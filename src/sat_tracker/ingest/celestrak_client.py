"""Configuration-driven ingestion pipeline for CelesTrak orbital data.

CelesTrak (https://celestrak.org) publishes publicly available orbital
element data for tracked space objects. This module queries CelesTrak's
GP (General Perturbations) endpoint for OMM (Orbit Mean-Elements
Message) data, either for a single object identified by its NORAD
catalog number or for an entire published group (e.g. ``"starlink"``),
and persists it locally in one of two formats selected by
``settings.ingest_format``:

- ``"csv"``: the compact OMM-CSV representation, written verbatim to
  the bronze landing zone (``settings.bronze_dir``).
- ``"sds"``: the JSON representation, converted to binary SDS
  FlatBuffers using the `OMM` schema from the `spacedatastandards-org`
  package and written to ``settings.sds_dir``.

A "CelesTrak Compliance Shield" wraps every fetch to keep the pipeline
from being IP-banned by CelesTrak:

- **Local cache verification**: if a landing-zone file for the
  requested target already exists and is under `_CACHE_TTL` old, the
  HTTP request is skipped entirely.
- **Conditional requests**: when a stale cached file carries an ``ETag``
  from a previous fetch, it is replayed as ``If-None-Match``. A ``304
  Not Modified`` response costs almost no bandwidth and refreshes the
  cache window without re-downloading the payload.
- **Daily volume budget**: CelesTrak firewall-blocks IP addresses that
  download more than 100 MB in a day, so downloaded bytes are tallied
  in a small on-disk ledger and fetching halts at
  ``settings.daily_volume_budget_bytes``.
- **Identifying User-Agent**: every request carries
  ``settings.user_agent``; CelesTrak's usage policy expects machine
  clients to be identifiable and contactable.
- **Fail-fast error gates**: only ``200`` and ``304`` responses are
  accepted. Any other status (301, 403, 404, 50x, or otherwise) raises
  `CelesTrakFatalError` immediately, with no automatic retry — CelesTrak
  treats repeated hits against a blocking response as abuse.

HTTP fetching and file-writing are shared between both formats so the
two ingestion paths stay DRY.
"""

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import flatbuffers
import requests
from flatbuffers import util as flatbuffers_util
from OMM.OMM import OMM as OMMReader
from OMM.OMM import OMMT
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from sat_tracker.config import settings

logger = logging.getLogger(__name__)

_CACHE_TTL = timedelta(hours=2)
_VOLUME_LEDGER_FILENAME = "download_volume.json"
_SOURCE_NAME = "celestrak"

# FlatBuffers size prefixes are 4-byte unsigned offsets preceding each record.
_SIZE_PREFIX_BYTES = 4


class CelesTrakFatalError(RuntimeError):
    """Raised when ingestion must halt and must not be retried."""


class CelesTrakVolumeBudgetExceeded(CelesTrakFatalError):
    """Raised when the configured daily download budget is already spent.

    Subclasses `CelesTrakFatalError` because the required response is the
    same: stop, do not retry. CelesTrak firewall-blocks IPs that exceed
    100 MB of downloads per day.
    """


def _is_cache_fresh(path: Path, *, ttl: timedelta = _CACHE_TTL) -> bool:
    """Check whether `path` exists and was last written within `ttl`.

    Args:
        path: The landing-zone file to check.
        ttl: Maximum age for the file to be considered fresh.

    Returns:
        True if `path` exists and its modification time is within
        `ttl` of now.
    """
    if not path.exists():
        return False
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return datetime.now(UTC) - modified_at < ttl


def _find_latest_landing_file(directory: Path, stem: str, suffix: str) -> Path | None:
    """Find the most recently written landing-zone file for `stem`, if any.

    Since each ingestion writes a uniquely named file (`stem` plus an
    ingestion timestamp and ID, to avoid collisions between runs), the
    cache check has to search for the newest matching file rather than
    a single fixed path.

    Args:
        directory: Landing-zone directory to search (e.g.
            `settings.bronze_dir`).
        stem: The NORAD ID or group name the file was written for.
        suffix: File extension to match, e.g. `".csv"` or `".sds"`.

    Returns:
        The most recently modified `stem_*suffix` file in `directory`,
        or None if none exists.
    """
    candidates = directory.glob(f"{stem}_*{suffix}")
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def _sidecar_path(path: Path) -> Path:
    """Return the `.meta.json` sidecar path belonging to a landing file."""
    return path.with_name(path.name + ".meta.json")


def _read_sidecar(path: Path) -> dict:
    """Read the audit metadata sidecar for a landing file.

    Args:
        path: The landing-zone file whose sidecar should be read.

    Returns:
        The parsed sidecar contents, or an empty dict if the sidecar is
        missing or malformed. A malformed sidecar is never fatal: the
        worst consequence is a missed conditional request.
    """
    sidecar = _sidecar_path(path)
    if not sidecar.exists():
        return {}
    try:
        return json.loads(sidecar.read_text())
    except json.JSONDecodeError:
        logger.warning("Ignoring malformed metadata sidecar %s", sidecar)
        return {}


def _volume_ledger_path() -> Path:
    """Return the path of the daily download-volume ledger."""
    return settings.state_dir / _VOLUME_LEDGER_FILENAME


def _read_volume_ledger() -> tuple[date, int]:
    """Read today's downloaded byte total from the on-disk ledger.

    The ledger resets at UTC midnight: a ledger written on an earlier
    date is reported as zero bytes used rather than being carried over.

    Returns:
        A ``(utc_date, bytes_downloaded)`` tuple for the current UTC day.
    """
    today = datetime.now(UTC).date()
    path = _volume_ledger_path()
    if not path.exists():
        return today, 0
    try:
        ledger = json.loads(path.read_text())
        if date.fromisoformat(ledger["date"]) != today:
            return today, 0
        return today, int(ledger["bytes"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("Ignoring malformed volume ledger %s", path)
        return today, 0


def _check_volume_budget() -> None:
    """Halt ingestion if today's download budget is already exhausted.

    Raises:
        CelesTrakVolumeBudgetExceeded: If the bytes already downloaded
            today meet or exceed `settings.daily_volume_budget_bytes`.
    """
    _, used = _read_volume_ledger()
    budget = settings.daily_volume_budget_bytes
    if used >= budget:
        raise CelesTrakVolumeBudgetExceeded(
            f"Daily CelesTrak download budget exhausted: {used} of {budget} bytes "
            "already downloaded today. Halting instead of risking the 100 MB/day "
            "firewall block. The budget resets at UTC midnight."
        )


def _record_downloaded_bytes(count: int) -> None:
    """Add `count` bytes to today's running download total.

    Args:
        count: Number of payload bytes just downloaded from CelesTrak.
    """
    today, used = _read_volume_ledger()
    path = _volume_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": today.isoformat(), "bytes": used + count}, indent=2))


def _get_celestrak(
    params: dict[str, int | str], *, etag: str | None = None
) -> requests.Response:
    """Query the CelesTrak GP endpoint under the full compliance shield.

    Checks the daily volume budget, sends an identifying User-Agent and
    an optional conditional-request header, enforces the fail-fast status
    gate, and tallies downloaded bytes.

    Args:
        params: Query parameters to send, e.g. ``{"CATNR": 25544,
            "FORMAT": "CSV"}``.
        etag: ``ETag`` from a previous fetch of the same target, replayed
            as ``If-None-Match`` so CelesTrak can answer ``304 Not
            Modified`` instead of resending an unchanged payload.

    Returns:
        The HTTP response, guaranteed to be either ``200`` or ``304``.

    Raises:
        CelesTrakVolumeBudgetExceeded: If today's download budget is
            already spent.
        CelesTrakFatalError: If CelesTrak responds with any status other
            than 200 or 304 (e.g. 301, 403, 404, or a 5xx). Never
            retried, to avoid tripping CelesTrak's abuse detection.
    """
    _check_volume_budget()

    retry = Retry(total=0)
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))

    headers = {"User-Agent": settings.user_agent}
    if etag:
        headers["If-None-Match"] = etag

    response = session.get(
        settings.celestrak_url, params=params, headers=headers, timeout=10
    )

    if response.status_code not in (200, 304):
        raise CelesTrakFatalError(
            f"CelesTrak returned fatal status {response.status_code} for "
            f"{response.url}; aborting the pipeline instead of retrying, "
            "to avoid triggering an IP ban."
        )

    if response.status_code == 200:
        _record_downloaded_bytes(len(response.content))

    return response


def _write_with_metadata(
    directory: Path, stem: str, suffix: str, data: bytes, *, etag: str | None = None
) -> Path:
    """Write `data` under a collision-free, auditable filename with a metadata sidecar.

    The filename is `<stem>_<ingested_at>_<ingestion_id><suffix>`, so
    repeated ingestions of the same `stem` (NORAD ID or group) never
    overwrite each other. Alongside the raw payload, writes a
    `<filename>.meta.json` sidecar carrying the ingestion audit fields
    and lineage columns, keeping the bronze raw payload itself untouched
    while making the ingestion fully auditable.

    Args:
        directory: Destination directory, created (including parents)
            if it does not already exist.
        stem: The NORAD ID or group name the file is being written for.
        suffix: File extension to use, e.g. `".csv"` or `".sds"`.
        data: Raw bytes to write.
        etag: ``ETag`` CelesTrak returned for this payload, recorded so
            the next fetch of the same target can issue a conditional
            request.

    Returns:
        The path the data was written to.
    """
    ingested_at = datetime.now(UTC)
    ingestion_id = uuid4()
    filename = f"{stem}_{ingested_at:%Y%m%dT%H%M%S%fZ}_{ingestion_id}{suffix}"

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(data)

    metadata = {
        "ingested_at": ingested_at.isoformat(),
        "ingestion_id": str(ingestion_id),
        "source": _SOURCE_NAME,
        "source_file": filename,
        "target": stem,
        "bytes": len(data),
    }
    if etag is not None:
        metadata["etag"] = etag

    _sidecar_path(path).write_text(json.dumps(metadata, indent=2))

    return path


def _build_omm_flatbuffers(records: list[dict]) -> bytes:
    """Encode CelesTrak GP JSON records as a size-prefixed `OMM` FlatBuffer stream.

    The `OMM` schema describes a single satellite, so a group of records
    is stored as a stream of individually size-prefixed FlatBuffers
    concatenated into one file. `read_omm_sds` iterates such a stream;
    a single-satellite fetch produces a stream of length one, so every
    `.sds` file this module writes has the same structure.

    Args:
        records: Objects' fields from a CelesTrak `FORMAT=json` GP
            response (e.g. `OBJECT_NAME`, `NORAD_CAT_ID`,
            `MEAN_MOTION`, ...), whose keys already match the `OMM`
            schema's field names.

    Returns:
        The concatenated size-prefixed FlatBuffer stream as raw bytes.
    """
    stream = bytearray()
    for record in records:
        omm = OMMT()
        for field, value in record.items():
            if value is not None and hasattr(omm, field):
                setattr(omm, field, value)

        builder = flatbuffers.Builder(1024)
        builder.FinishSizePrefixed(omm.Pack(builder))
        stream += builder.Output()

    return bytes(stream)


def read_omm_sds(path: Path) -> list[OMMReader]:
    """Decode every `OMM` record from a size-prefixed `.sds` FlatBuffer stream.

    Args:
        path: A `.sds` file written by this module.

    Returns:
        One `OMM` reader per satellite encoded in the file, in the order
        CelesTrak returned them.
    """
    buffer = path.read_bytes()
    records: list[OMMReader] = []

    offset = 0
    while offset < len(buffer):
        size = flatbuffers_util.GetSizePrefix(buffer, offset)
        records.append(OMMReader.GetRootAs(buffer, offset + _SIZE_PREFIX_BYTES))
        offset += _SIZE_PREFIX_BYTES + size

    return records


def _fetch_and_cache(
    params: dict[str, int | str],
    *,
    directory: Path,
    stem: str,
    suffix: str,
    not_found_label: str,
) -> Path:
    """Run the shared cache-check, conditional-fetch and write flow.

    Both the CSV and SDS paths differ only in how a ``200`` payload is
    turned into bytes, so everything up to that point — cache
    freshness, `ETag` replay, `304` handling, and the empty-response
    check — lives here.

    Args:
        params: Query parameters for the CelesTrak GP request.
        directory: Landing zone to read the cache from and write to.
        stem: The NORAD ID or group name keying the filename and cache
            lookup.
        suffix: File extension, e.g. `".csv"` or `".sds"`.
        not_found_label: Human-readable description of what was
            requested, used in the `ValueError` message if CelesTrak has
            no matching data.

    Returns:
        The path of the cached or newly written file.

    Raises:
        CelesTrakVolumeBudgetExceeded: If today's download budget is spent.
        CelesTrakFatalError: If CelesTrak responds with a fatal status.
        ValueError: If CelesTrak returns no data for the request.
    """
    cached = _find_latest_landing_file(directory, stem, suffix)
    if cached is not None and _is_cache_fresh(cached):
        logger.info("Using cached local data (under 2 hours old)")
        return cached

    etag = _read_sidecar(cached).get("etag") if cached is not None else None
    response = _get_celestrak(params, etag=etag)

    if response.status_code == 304 and cached is not None:
        logger.info("CelesTrak reports data unchanged (HTTP 304); reusing cached file")
        # Reset the 2h TTL window: the cached bytes are confirmed current.
        cached.touch()
        return cached

    if suffix == ".csv":
        if not response.content or response.text.strip().startswith("No GP data found"):
            raise ValueError(f"No OMM data found for {not_found_label}")
        data = response.content
    else:
        records = response.json()
        if not records:
            raise ValueError(f"No OMM data found for {not_found_label}")
        data = _build_omm_flatbuffers(records)

    return _write_with_metadata(
        directory, stem, suffix, data, etag=response.headers.get("ETag")
    )


def fetch_omm_csv(norad_id: int) -> Path:
    """Fetch compact OMM-CSV data for a single satellite and land it in the bronze zone.

    Skips the HTTP request and returns the existing file if a cached
    copy under `_CACHE_TTL` old is already present.

    Args:
        norad_id: The NORAD catalog number (also known as SATCAT
            number) identifying the space object to fetch data for.

    Returns:
        The path of the `.csv` file, under `settings.bronze_dir`.

    Raises:
        CelesTrakFatalError: If CelesTrak responds with a fatal status,
            or today's download budget is spent.
        ValueError: If CelesTrak returns no data for `norad_id`.
    """
    return _fetch_and_cache(
        {"CATNR": norad_id, "FORMAT": "CSV"},
        directory=settings.bronze_dir,
        stem=str(norad_id),
        suffix=".csv",
        not_found_label=f"NORAD catalog number {norad_id}",
    )


def fetch_omm_csv_group(group: str) -> Path:
    """Fetch compact OMM-CSV data for an entire CelesTrak GP group and land it in the bronze zone.

    Useful for bulk constellations (e.g. `"starlink"`, `"oneweb"`,
    `"gps-ops"`) where querying one NORAD ID at a time would be
    impractical. See https://celestrak.org/NORAD/elements/ for the full
    list of group names CelesTrak publishes.

    Skips the HTTP request and returns the existing file if a cached
    copy under `_CACHE_TTL` old is already present.

    Args:
        group: The CelesTrak GP group name, e.g. `"starlink"`.

    Returns:
        The path of the `.csv` file, under `settings.bronze_dir`.

    Raises:
        CelesTrakFatalError: If CelesTrak responds with a fatal status,
            or today's download budget is spent.
        ValueError: If CelesTrak returns no data for `group`.
    """
    return _fetch_and_cache(
        {"GROUP": group, "FORMAT": "CSV"},
        directory=settings.bronze_dir,
        stem=group,
        suffix=".csv",
        not_found_label=f"CelesTrak group '{group}'",
    )


def fetch_omm_sds(norad_id: int) -> Path:
    """Fetch OMM JSON data for a single satellite and write it as a binary SDS FlatBuffer.

    Skips the HTTP request and returns the existing file if a cached
    copy under `_CACHE_TTL` old is already present.

    Args:
        norad_id: The NORAD catalog number (also known as SATCAT
            number) identifying the space object to fetch data for.

    Returns:
        The path of the `.sds` file, under `settings.sds_dir`. Read it
        back with `read_omm_sds`.

    Raises:
        CelesTrakFatalError: If CelesTrak responds with a fatal status,
            or today's download budget is spent.
        ValueError: If CelesTrak returns no data for `norad_id`.
    """
    return _fetch_and_cache(
        {"CATNR": norad_id, "FORMAT": "json"},
        directory=settings.sds_dir,
        stem=str(norad_id),
        suffix=".sds",
        not_found_label=f"NORAD catalog number {norad_id}",
    )


def fetch_omm_sds_group(group: str) -> Path:
    """Fetch OMM JSON data for an entire CelesTrak GP group as binary SDS FlatBuffers.

    Every object CelesTrak returns for the group is encoded; the records
    are stored as a size-prefixed FlatBuffer stream in a single file,
    since the `OMM` schema itself describes one satellite. Read the file
    back with `read_omm_sds`.

    Skips the HTTP request and returns the existing file if a cached
    copy under `_CACHE_TTL` old is already present.

    Args:
        group: The CelesTrak GP group name, e.g. `"starlink"`.

    Returns:
        The path of the `.sds` file, under `settings.sds_dir`.

    Raises:
        CelesTrakFatalError: If CelesTrak responds with a fatal status,
            or today's download budget is spent.
        ValueError: If CelesTrak returns no data for `group`.
    """
    return _fetch_and_cache(
        {"GROUP": group, "FORMAT": "json"},
        directory=settings.sds_dir,
        stem=group,
        suffix=".sds",
        not_found_label=f"CelesTrak group '{group}'",
    )


def ingest(*, norad_id: int | None = None, group: str | None = None) -> Path:
    """Ingest OMM data for one satellite or one group using the configured format.

    Dispatches to the CSV or SDS flow based on `settings.ingest_format`.

    Args:
        norad_id: The NORAD catalog number identifying a single space
            object. Mutually exclusive with `group`.
        group: A CelesTrak GP group name, e.g. `"starlink"`. Mutually
            exclusive with `norad_id`.

    Returns:
        The path of the written (or cached) file.

    Raises:
        ValueError: If neither or both of `norad_id` and `group` are
            given, or if CelesTrak returns no data for the target.
        CelesTrakFatalError: If CelesTrak responds with a fatal status,
            or today's download budget is spent.
    """
    if (norad_id is None) == (group is None):
        raise ValueError("Provide exactly one of `norad_id` or `group`.")

    if norad_id is not None:
        if settings.ingest_format == "csv":
            return fetch_omm_csv(norad_id)
        return fetch_omm_sds(norad_id)

    assert group is not None  # narrowed by the mutual-exclusion check above
    if settings.ingest_format == "csv":
        return fetch_omm_csv_group(group)
    return fetch_omm_sds_group(group)
