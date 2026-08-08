"""Configuration-driven ingestion pipeline for CelesTrak orbital data.

CelesTrak (https://celestrak.org) publishes publicly available orbital
element data for tracked space objects. This module queries CelesTrak's
GP (General Perturbations) endpoint for OMM (Orbit Mean-Elements
Message) data for individual objects identified by their NORAD catalog
number, and persists it locally in one of two formats selected by
``settings.ingest_format``:

- ``"csv"``: the compact OMM-CSV representation, written verbatim to
  the bronze landing zone (``settings.bronze_dir``).
- ``"sds"``: the JSON representation, converted to a binary SDS
  FlatBuffer using the `OMM` schema from the `spacedatastandards-org`
  package and written to ``settings.sds_dir``.

A "CelesTrak Compliance Shield" wraps every fetch to keep the pipeline
from being IP-banned by CelesTrak:

- **Local cache verification**: if a landing-zone file for the
  requested object already exists and is under `_CACHE_TTL` old, the
  HTTP request is skipped entirely.
- **Fail-fast error gates**: only an HTTP 200 response is accepted. Any
  other status (301, 403, 404, 50x, or otherwise) raises
  `CelesTrakFatalError` immediately, with no automatic retry — CelesTrak
  treats repeated hits against a blocking response as abuse.

HTTP fetching and file-writing are shared between both formats so the
two ingestion paths stay DRY.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import flatbuffers
import requests
from OMM.OMM import OMMT
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from sat_tracker.config import settings

logger = logging.getLogger(__name__)

_CACHE_TTL = timedelta(hours=2)


class CelesTrakFatalError(RuntimeError):
    """Raised when CelesTrak returns a status that must not be retried."""


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


def _get_celestrak(params: dict[str, int | str]) -> requests.Response:
    """Query the CelesTrak GP endpoint, enforcing the fail-fast status gate.

    Args:
        params: Query parameters to send, e.g. ``{"CATNR": 25544,
            "FORMAT": "CSV"}``.

    Returns:
        The HTTP 200 response.

    Raises:
        CelesTrakFatalError: If CelesTrak responds with any status other
            than 200 (e.g. 301, 403, 404, or a 5xx). Never retried, to
            avoid tripping CelesTrak's abuse detection.
    """
    retry = Retry(total=0)
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))

    response = session.get(settings.celestrak_url, params=params, timeout=10)

    if response.status_code != 200:
        raise CelesTrakFatalError(
            f"CelesTrak returned fatal status {response.status_code} for "
            f"{response.url}; aborting the pipeline instead of retrying, "
            "to avoid triggering an IP ban."
        )
    return response


def _write_with_metadata(directory: Path, stem: str, suffix: str, data: bytes) -> Path:
    """Write `data` under a collision-free, auditable filename with a metadata sidecar.

    The filename is `<stem>_<ingested_at>_<ingestion_id><suffix>`, so
    repeated ingestions of the same `stem` (NORAD ID or group) never
    overwrite each other. Alongside the raw payload, writes a
    `<filename>.meta.json` sidecar containing the same UTC `ingested_at`
    timestamp and `ingestion_id` (UUID) in structured form, keeping the
    bronze raw payload itself untouched while making the ingestion
    fully auditable.

    Args:
        directory: Destination directory, created (including parents)
            if it does not already exist.
        stem: The NORAD ID or group name the file is being written for.
        suffix: File extension to use, e.g. `".csv"` or `".sds"`.
        data: Raw bytes to write.

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
    }
    metadata_path = path.with_name(path.name + ".meta.json")
    metadata_path.write_text(json.dumps(metadata, indent=2))

    return path


def _build_omm_flatbuffer(record: dict) -> bytes:
    """Encode a single CelesTrak GP JSON record as an `OMM` FlatBuffer.

    Args:
        record: One object's fields from a CelesTrak `FORMAT=json` GP
            response (e.g. `OBJECT_NAME`, `NORAD_CAT_ID`, `MEAN_MOTION`,
            ...), whose keys already match the `OMM` schema's field
            names.

    Returns:
        The FlatBuffer-encoded `OMM` record as raw bytes.
    """
    omm = OMMT()
    for field, value in record.items():
        if value is not None and hasattr(omm, field):
            setattr(omm, field, value)

    builder = flatbuffers.Builder(1024)
    builder.Finish(omm.Pack(builder))
    return bytes(builder.Output())


def _fetch_and_cache_csv(params: dict[str, int | str], stem: str, not_found_label: str) -> Path:
    """Shared cache-check + fetch + write flow for a bronze-zone CSV file.

    Args:
        params: Query parameters for the CelesTrak GP request (must
            include ``"FORMAT": "CSV"``).
        stem: The NORAD ID or group name to key the filename and cache
            lookup on, under `settings.bronze_dir`.
        not_found_label: Human-readable description of what was
            requested, used in the `ValueError` message if CelesTrak
            has no matching data.

    Returns:
        The path of the `.csv` file, under `settings.bronze_dir`.

    Raises:
        CelesTrakFatalError: If CelesTrak responds with a non-200 status.
        ValueError: If CelesTrak returns no data for the request.
    """
    cached = _find_latest_landing_file(settings.bronze_dir, stem, ".csv")
    if cached is not None and _is_cache_fresh(cached):
        logger.info("Using cached local data (under 2 hours old)")
        return cached

    response = _get_celestrak(params)
    if not response.content or response.text.strip().startswith("No GP data found"):
        raise ValueError(f"No OMM data found for {not_found_label}")

    return _write_with_metadata(settings.bronze_dir, stem, ".csv", response.content)


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
        CelesTrakFatalError: If CelesTrak responds with a non-200 status.
        ValueError: If CelesTrak returns no data for `norad_id`.
    """
    return _fetch_and_cache_csv(
        {"CATNR": norad_id, "FORMAT": "CSV"},
        str(norad_id),
        f"NORAD catalog number {norad_id}",
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
        CelesTrakFatalError: If CelesTrak responds with a non-200 status.
        ValueError: If CelesTrak returns no data for `group`.
    """
    return _fetch_and_cache_csv(
        {"GROUP": group, "FORMAT": "CSV"},
        group,
        f"CelesTrak group '{group}'",
    )


def _fetch_and_cache_sds(params: dict[str, int | str], stem: str, not_found_label: str) -> Path:
    """Shared cache-check + fetch + encode + write flow for an SDS FlatBuffer file.

    Args:
        params: Query parameters for the CelesTrak GP request (must
            include ``"FORMAT": "json"``).
        stem: The NORAD ID or group name to key the filename and cache
            lookup on, under `settings.sds_dir`.
        not_found_label: Human-readable description of what was
            requested, used in the `ValueError` message if CelesTrak
            has no matching data.

    Returns:
        The path of the `.sds` file, under `settings.sds_dir`.

    Raises:
        CelesTrakFatalError: If CelesTrak responds with a non-200 status.
        ValueError: If CelesTrak returns no data for the request.
    """
    cached = _find_latest_landing_file(settings.sds_dir, stem, ".sds")
    if cached is not None and _is_cache_fresh(cached):
        logger.info("Using cached local data (under 2 hours old)")
        return cached

    response = _get_celestrak(params)
    records = response.json()
    if not records:
        raise ValueError(f"No OMM data found for {not_found_label}")

    data = _build_omm_flatbuffer(records[0])
    return _write_with_metadata(settings.sds_dir, stem, ".sds", data)


def fetch_omm_sds(norad_id: int) -> Path:
    """Fetch OMM JSON data for a single satellite and write it as a binary SDS FlatBuffer.

    Skips the HTTP request and returns the existing file if a cached
    copy under `_CACHE_TTL` old is already present.

    Args:
        norad_id: The NORAD catalog number (also known as SATCAT
            number) identifying the space object to fetch data for.

    Returns:
        The path of the `.sds` file, under `settings.sds_dir`.

    Raises:
        CelesTrakFatalError: If CelesTrak responds with a non-200 status.
        ValueError: If CelesTrak returns no data for `norad_id`.
    """
    return _fetch_and_cache_sds(
        {"CATNR": norad_id, "FORMAT": "json"},
        str(norad_id),
        f"NORAD catalog number {norad_id}",
    )


def fetch_omm_sds_group(group: str) -> Path:
    """Fetch OMM JSON data for an entire CelesTrak GP group and write it as a binary SDS FlatBuffer.

    Note that CelesTrak's `FORMAT=json` group response is a list of one
    record per object, but the `OMM` FlatBuffer schema encodes a single
    record; only the first object in `group` is encoded. For bulk
    constellations, prefer `fetch_omm_csv_group` instead.

    Skips the HTTP request and returns the existing file if a cached
    copy under `_CACHE_TTL` old is already present.

    Args:
        group: The CelesTrak GP group name, e.g. `"starlink"`.

    Returns:
        The path of the `.sds` file, under `settings.sds_dir`.

    Raises:
        CelesTrakFatalError: If CelesTrak responds with a non-200 status.
        ValueError: If CelesTrak returns no data for `group`.
    """
    return _fetch_and_cache_sds(
        {"GROUP": group, "FORMAT": "json"},
        group,
        f"CelesTrak group '{group}'",
    )


def ingest(norad_id: int) -> Path:
    """Ingest OMM data for a satellite using the configured format.

    Dispatches to `fetch_omm_csv` or `fetch_omm_sds` based on
    `settings.ingest_format`.

    Args:
        norad_id: The NORAD catalog number (also known as SATCAT
            number) identifying the space object to fetch data for.

    Returns:
        The path of the written (or cached) file.

    Raises:
        CelesTrakFatalError: If CelesTrak responds with a non-200 status.
        ValueError: If CelesTrak returns no data for `norad_id`.
    """
    if settings.ingest_format == "csv":
        return fetch_omm_csv(norad_id)
    return fetch_omm_sds(norad_id)
