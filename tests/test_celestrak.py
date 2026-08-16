"""Tests for the CelesTrak ingestion pipeline."""

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from sat_tracker.config import Settings
from sat_tracker.ingest.celestrak_client import (
    CelesTrakFatalError,
    CelesTrakVolumeBudgetExceeded,
    _is_cache_fresh,
    fetch_omm_csv,
    fetch_omm_csv_group,
    fetch_omm_sds,
    fetch_omm_sds_group,
    fetch_satcat,
    ingest,
    read_omm_sds,
)

ISS_CSV_RESPONSE = (
    "OBJECT_NAME,OBJECT_ID,EPOCH,MEAN_MOTION,ECCENTRICITY,INCLINATION,"
    "RA_OF_ASC_NODE,ARG_OF_PERICENTER,MEAN_ANOMALY,EPHEMERIS_TYPE,"
    "CLASSIFICATION_TYPE,NORAD_CAT_ID,ELEMENT_SET_NO,REV_AT_EPOCH,BSTAR,"
    "MEAN_MOTION_DOT,MEAN_MOTION_DDOT\r\n"
    "ISS (ZARYA),1998-067A,2021-07-18T21:59:56.518368,15.48815520,0.0003456,"
    "51.6423,194.0154,92.8797,333.0031,0,U,25544,999,29323,0,1.449e-05,0\r\n"
)

ISS_JSON_RESPONSE = [
    {
        "OBJECT_NAME": "ISS (ZARYA)",
        "OBJECT_ID": "1998-067A",
        "EPOCH": "2021-07-18T21:59:56.518368",
        "MEAN_MOTION": 15.48815520,
        "ECCENTRICITY": 0.0003456,
        "INCLINATION": 51.6423,
        "RA_OF_ASC_NODE": 194.0154,
        "ARG_OF_PERICENTER": 92.8797,
        "MEAN_ANOMALY": 333.0031,
        "EPHEMERIS_TYPE": 0,
        "CLASSIFICATION_TYPE": "U",
        "NORAD_CAT_ID": 25544,
        "ELEMENT_SET_NO": 999,
        "REV_AT_EPOCH": 29323,
        "BSTAR": 0.0,
        "MEAN_MOTION_DOT": 0.00001449,
        "MEAN_MOTION_DDOT": 0.0,
    }
]


def _assert_landing_filename(path: Path, directory: Path, stem: str, suffix: str) -> None:
    """Assert `path` is `<stem>_<...><suffix>` inside `directory` (the collision-free naming scheme)."""
    assert path.parent == directory
    assert path.name.startswith(f"{stem}_")
    assert path.name.endswith(suffix)


def _make_stale(path: Path, *, hours: int = 3) -> None:
    """Backdate `path`'s mtime so the cache-freshness check treats it as expired."""
    stale_timestamp = (datetime.now(UTC) - timedelta(hours=hours)).timestamp()
    os.utime(path, (stale_timestamp, stale_timestamp))


def test_fetch_omm_csv_writes_bronze_file(isolated_settings, mock_celestrak_response) -> None:
    """`fetch_omm_csv` should land the raw OMM-CSV response in the bronze zone."""
    mock_get = mock_celestrak_response(text=ISS_CSV_RESPONSE)

    path = fetch_omm_csv(norad_id=25544)

    mock_get.assert_called_once()
    assert mock_get.call_args.kwargs["params"] == {"CATNR": 25544, "FORMAT": "CSV"}
    _assert_landing_filename(path, isolated_settings.bronze_dir, "25544", ".csv")
    assert path.read_bytes() == ISS_CSV_RESPONSE.encode("utf-8")


@pytest.mark.usefixtures("isolated_settings")
def test_fetch_omm_csv_raises_when_not_found(mock_celestrak_response) -> None:
    """`fetch_omm_csv` should raise `ValueError` when CelesTrak reports no data."""
    mock_celestrak_response(text="No GP data found")

    with pytest.raises(ValueError):
        fetch_omm_csv(norad_id=99999999)


def test_fetch_omm_sds_writes_flatbuffer_file(isolated_settings, mock_celestrak_response) -> None:
    """`fetch_omm_sds` should encode the JSON response as an OMM FlatBuffer file."""
    mock_get = mock_celestrak_response(json_body=ISS_JSON_RESPONSE)

    path = fetch_omm_sds(norad_id=25544)

    mock_get.assert_called_once()
    assert mock_get.call_args.kwargs["params"] == {"CATNR": 25544, "FORMAT": "json"}
    _assert_landing_filename(path, isolated_settings.sds_dir, "25544", ".sds")

    records = read_omm_sds(path)
    assert len(records) == 1
    omm = records[0]
    assert omm.NORAD_CAT_ID() == 25544
    assert omm.OBJECT_NAME() == b"ISS (ZARYA)"
    assert omm.OBJECT_ID() == b"1998-067A"
    assert omm.MEAN_MOTION() == pytest.approx(15.48815520)
    assert omm.ECCENTRICITY() == pytest.approx(0.0003456)


@pytest.mark.usefixtures("isolated_settings")
def test_fetch_omm_sds_raises_on_empty_response(mock_celestrak_response) -> None:
    """`fetch_omm_sds` should raise `ValueError` when CelesTrak returns no records."""
    mock_celestrak_response(json_body=[])

    with pytest.raises(ValueError):
        fetch_omm_sds(norad_id=99999999)


def test_fetch_omm_sds_group_writes_flatbuffer_file(isolated_settings, mock_celestrak_response) -> None:
    """`fetch_omm_sds_group` should query by GROUP and land the FlatBuffer in the sds zone."""
    mock_get = mock_celestrak_response(json_body=ISS_JSON_RESPONSE)

    path = fetch_omm_sds_group("starlink")

    mock_get.assert_called_once()
    assert mock_get.call_args.kwargs["params"] == {"GROUP": "starlink", "FORMAT": "json"}
    _assert_landing_filename(path, isolated_settings.sds_dir, "starlink", ".sds")

    records = read_omm_sds(path)
    assert len(records) == 1
    assert records[0].NORAD_CAT_ID() == 25544


@pytest.mark.usefixtures("isolated_settings")
def test_fetch_omm_sds_group_encodes_every_record(mock_celestrak_response) -> None:
    """Every satellite in a group must be encoded, not just the first.

    Regression test: the group flow previously wrote only `records[0]`,
    silently discarding the rest of the constellation.
    """
    group_response = [
        {**ISS_JSON_RESPONSE[0], "NORAD_CAT_ID": 40000 + index, "OBJECT_NAME": f"STARLINK-{index}"}
        for index in range(5)
    ]
    mock_celestrak_response(json_body=group_response)

    path = fetch_omm_sds_group("starlink")

    records = read_omm_sds(path)
    assert len(records) == 5
    assert [record.NORAD_CAT_ID() for record in records] == [40000 + i for i in range(5)]
    assert records[4].OBJECT_NAME() == b"STARLINK-4"


def test_ingest_dispatches_to_csv_by_default(isolated_settings, mock_celestrak_response) -> None:
    """`ingest` should use the CSV flow when `ingest_format` is `"csv"` (the default)."""
    mock_celestrak_response(text=ISS_CSV_RESPONSE)

    path = ingest(norad_id=25544)

    _assert_landing_filename(path, isolated_settings.bronze_dir, "25544", ".csv")


def test_ingest_dispatches_to_sds_when_configured(
    isolated_settings, mock_celestrak_response, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ingest` should use the SDS flow when `ingest_format` is `"sds"`."""
    monkeypatch.setattr(isolated_settings, "ingest_format", "sds")
    mock_celestrak_response(json_body=ISS_JSON_RESPONSE)

    path = ingest(norad_id=25544)

    _assert_landing_filename(path, isolated_settings.sds_dir, "25544", ".sds")


def test_fetch_omm_csv_group_writes_bronze_file(isolated_settings, mock_celestrak_response) -> None:
    """`fetch_omm_csv_group` should query by GROUP and land the CSV in the bronze zone."""
    mock_get = mock_celestrak_response(text=ISS_CSV_RESPONSE)

    path = fetch_omm_csv_group("starlink")

    mock_get.assert_called_once()
    assert mock_get.call_args.kwargs["params"] == {"GROUP": "starlink", "FORMAT": "CSV"}
    _assert_landing_filename(path, isolated_settings.bronze_dir, "starlink", ".csv")
    assert path.read_bytes() == ISS_CSV_RESPONSE.encode("utf-8")


def test_fetch_omm_csv_uses_fresh_cache(
    isolated_settings, mock_celestrak_response, caplog: pytest.LogCaptureFixture
) -> None:
    """A landing-zone file under 2 hours old should be served instead of re-fetched."""
    cached_path = isolated_settings.bronze_dir / "25544_20260101T000000000000Z_cached-test.csv"
    cached_path.parent.mkdir(parents=True)
    cached_path.write_text("cached content")
    mock_get = mock_celestrak_response(text=ISS_CSV_RESPONSE)

    with caplog.at_level(logging.INFO):
        path = fetch_omm_csv(norad_id=25544)

    mock_get.assert_not_called()
    assert path == cached_path
    assert path.read_text() == "cached content"
    assert "Using cached local data (under 2 hours old)" in caplog.text


def test_fetch_omm_csv_refetches_stale_cache(isolated_settings, mock_celestrak_response) -> None:
    """A landing-zone file 2+ hours old should be re-downloaded, not served from cache."""
    cached_path = isolated_settings.bronze_dir / "25544_20260101T000000000000Z_stale-test.csv"
    cached_path.parent.mkdir(parents=True)
    cached_path.write_text("stale content")
    _make_stale(cached_path)
    mock_get = mock_celestrak_response(text=ISS_CSV_RESPONSE)

    path = fetch_omm_csv(norad_id=25544)

    mock_get.assert_called_once()
    assert path.read_bytes() == ISS_CSV_RESPONSE.encode("utf-8")


@pytest.mark.usefixtures("isolated_settings")
@pytest.mark.parametrize("status_code", [301, 403, 404, 500, 502, 503])
def test_fetch_omm_csv_fails_fast_on_error_status(
    mock_celestrak_response, status_code: int
) -> None:
    """A non-200 CelesTrak response should raise immediately, with no retry."""
    mock_get = mock_celestrak_response(text=ISS_CSV_RESPONSE, status_code=status_code)

    with pytest.raises(CelesTrakFatalError):
        fetch_omm_csv(norad_id=25544)

    mock_get.assert_called_once()


@pytest.mark.usefixtures("isolated_settings")
@pytest.mark.parametrize("status_code", [403, 404, 500])
def test_fetch_omm_sds_fails_fast_on_error_status(
    mock_celestrak_response, status_code: int
) -> None:
    """The SDS flow should also fail fast, without retrying, on error statuses."""
    mock_get = mock_celestrak_response(json_body=ISS_JSON_RESPONSE, status_code=status_code)

    with pytest.raises(CelesTrakFatalError):
        fetch_omm_sds(norad_id=25544)

    mock_get.assert_called_once()


@pytest.mark.usefixtures("isolated_settings")
def test_fetch_omm_csv_writes_auditable_metadata_sidecar(mock_celestrak_response) -> None:
    """Each written file should get a `.meta.json` sidecar with ingestion audit fields."""
    mock_celestrak_response(text=ISS_CSV_RESPONSE)

    path = fetch_omm_csv(norad_id=25544)
    metadata_path = path.with_name(path.name + ".meta.json")
    metadata = json.loads(metadata_path.read_text())

    assert UUID(metadata["ingestion_id"])
    assert datetime.fromisoformat(metadata["ingested_at"]).tzinfo is not None
    assert metadata["source"] == "celestrak"
    assert metadata["source_file"] == path.name
    assert metadata["target"] == "25544"
    assert metadata["bytes"] == len(ISS_CSV_RESPONSE.encode("utf-8"))


# --- Compliance shield: User-Agent, conditional requests, volume budget ---


@pytest.mark.usefixtures("isolated_settings")
def test_request_sends_identifying_user_agent(
    settings: Settings, mock_celestrak_response
) -> None:
    """Every request must identify the client, per CelesTrak's usage policy."""
    mock_get = mock_celestrak_response(text=ISS_CSV_RESPONSE)

    fetch_omm_csv(norad_id=25544)

    user_agent = mock_get.call_args.kwargs["headers"]["User-Agent"]
    assert user_agent == settings.user_agent
    assert "python-requests" not in user_agent


@pytest.mark.usefixtures("isolated_settings")
def test_etag_is_recorded_in_sidecar(mock_celestrak_response) -> None:
    """A returned `ETag` should be persisted so the next fetch can be conditional."""
    mock_celestrak_response(text=ISS_CSV_RESPONSE, etag='W/"abc123"')

    path = fetch_omm_csv(norad_id=25544)
    metadata = json.loads(path.with_name(path.name + ".meta.json").read_text())

    assert metadata["etag"] == 'W/"abc123"'


def test_stale_cache_replays_etag_as_conditional_request(
    isolated_settings, mock_celestrak_response
) -> None:
    """A stale cached file with an `ETag` must trigger an `If-None-Match` request."""
    cached_path = isolated_settings.bronze_dir / "25544_20260101T000000000000Z_etag-test.csv"
    cached_path.parent.mkdir(parents=True)
    cached_path.write_text("cached content")
    cached_path.with_name(cached_path.name + ".meta.json").write_text(
        json.dumps({"etag": 'W/"cached-etag"'})
    )
    _make_stale(cached_path)
    mock_get = mock_celestrak_response(text=ISS_CSV_RESPONSE)

    fetch_omm_csv(norad_id=25544)

    assert mock_get.call_args.kwargs["headers"]["If-None-Match"] == 'W/"cached-etag"'


def test_not_modified_response_reuses_cache_without_rewriting(
    isolated_settings, mock_celestrak_response, caplog: pytest.LogCaptureFixture
) -> None:
    """A `304 Not Modified` must reuse the cached bytes and refresh its TTL window."""
    cached_path = isolated_settings.bronze_dir / "25544_20260101T000000000000Z_304-test.csv"
    cached_path.parent.mkdir(parents=True)
    cached_path.write_text("cached content")
    _make_stale(cached_path)

    mock_celestrak_response(status_code=304)

    with caplog.at_level(logging.INFO):
        path = fetch_omm_csv(norad_id=25544)

    assert path == cached_path
    assert path.read_text() == "cached content"
    assert "HTTP 304" in caplog.text
    # The TTL window is reset, so an immediate re-fetch is served from cache.
    assert _is_cache_fresh(cached_path)


@pytest.mark.usefixtures("isolated_settings")
def test_download_volume_is_tallied_against_daily_budget(
    isolated_settings, mock_celestrak_response
) -> None:
    """Downloaded bytes should accumulate in the on-disk volume ledger."""
    mock_celestrak_response(text=ISS_CSV_RESPONSE)

    fetch_omm_csv(norad_id=25544)
    ledger = json.loads((isolated_settings.state_dir / "download_volume.json").read_text())

    assert ledger["bytes"] == len(ISS_CSV_RESPONSE.encode("utf-8"))
    assert ledger["date"] == datetime.now(UTC).date().isoformat()


def test_fetch_halts_when_daily_budget_is_exhausted(
    isolated_settings, mock_celestrak_response, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exceeding the daily byte budget must halt before any HTTP request is made."""
    monkeypatch.setattr(isolated_settings, "daily_volume_budget_bytes", 100)
    isolated_settings.state_dir.mkdir(parents=True)
    (isolated_settings.state_dir / "download_volume.json").write_text(
        json.dumps({"date": datetime.now(UTC).date().isoformat(), "bytes": 100})
    )
    mock_get = mock_celestrak_response(text=ISS_CSV_RESPONSE)

    with pytest.raises(CelesTrakVolumeBudgetExceeded):
        fetch_omm_csv(norad_id=25544)

    mock_get.assert_not_called()


def test_volume_ledger_resets_on_a_new_utc_day(
    isolated_settings, mock_celestrak_response, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Yesterday's spent budget must not block today's fetch."""
    monkeypatch.setattr(isolated_settings, "daily_volume_budget_bytes", 100)
    isolated_settings.state_dir.mkdir(parents=True)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    (isolated_settings.state_dir / "download_volume.json").write_text(
        json.dumps({"date": yesterday, "bytes": 999_999})
    )
    mock_get = mock_celestrak_response(text=ISS_CSV_RESPONSE)

    fetch_omm_csv(norad_id=25544)

    mock_get.assert_called_once()


# --- ingest() dispatch ---


@pytest.mark.usefixtures("isolated_settings")
def test_ingest_supports_groups(isolated_settings, mock_celestrak_response) -> None:
    """`ingest` should accept a group target, not only a NORAD ID."""
    mock_get = mock_celestrak_response(text=ISS_CSV_RESPONSE)

    path = ingest(group="starlink")

    assert mock_get.call_args.kwargs["params"] == {"GROUP": "starlink", "FORMAT": "CSV"}
    _assert_landing_filename(path, isolated_settings.bronze_dir, "starlink", ".csv")


@pytest.mark.parametrize(
    "kwargs", [{}, {"norad_id": 25544, "group": "starlink"}], ids=["neither", "both"]
)
def test_ingest_requires_exactly_one_target(kwargs: dict) -> None:
    """`ingest` must reject an ambiguous or empty target."""
    with pytest.raises(ValueError, match="exactly one"):
        ingest(**kwargs)


SATCAT_CSV_RESPONSE = (
    "OBJECT_NAME,OBJECT_ID,NORAD_CAT_ID,OBJECT_TYPE,OPS_STATUS_CODE,OWNER,"
    "LAUNCH_DATE,LAUNCH_SITE,DECAY_DATE,PERIOD,INCLINATION,APOGEE,PERIGEE,"
    "RCS,DATA_STATUS_CODE,ORBIT_CENTER,ORBIT_TYPE\r\n"
    "ISS (ZARYA),1998-067A,25544,PAY,+,ISS,1998-11-20,TTMTR,,92.9,51.64,"
    "422,415,399.05,,EA,ORB\r\n"
    "SL-1 R/B,1998-067B,25545,R/B,D,CIS,1998-11-20,TTMTR,1998-12-03,,,,,,,EA,ORB\r\n"
)
"""A two-row SATCAT sample: one payload in orbit, one decayed rocket body.

The decayed row is the point — it carries a DECAY_DATE and empty
PERIOD/INCLINATION/APOGEE/PERIGEE, which is exactly the shape that makes
every bronze SATCAT column TEXT rather than typed.
"""


def test_fetch_satcat_writes_its_own_landing_zone(
    isolated_settings, mock_celestrak_response
) -> None:
    """SATCAT must land under `satcat_dir`, separate from the GP landings.

    A bulk load tells the two feeds apart by directory, so a SATCAT file
    landing in `bronze_dir` would be converted with the GP schema.
    """
    mock_celestrak_response(text=SATCAT_CSV_RESPONSE)

    path = fetch_satcat()

    _assert_landing_filename(path, isolated_settings.satcat_dir, "satcat", ".csv")
    # Compared as bytes: bronze's contract is byte-for-byte fidelity, and
    # `read_text` would silently normalise CelesTrak's CRLF line endings.
    assert path.read_bytes() == SATCAT_CSV_RESPONSE.encode("utf-8")
    assert not isolated_settings.bronze_dir.exists()


def test_fetch_satcat_queries_the_satcat_url(
    isolated_settings, mock_celestrak_response
) -> None:
    """The dump is a static file on a different URL, requested with no query params."""
    mock_get = mock_celestrak_response(text=SATCAT_CSV_RESPONSE)

    fetch_satcat()

    mock_get.assert_called_once()
    assert mock_get.call_args.args[0] == isolated_settings.celestrak_satcat_url
    assert mock_get.call_args.kwargs["params"] == {}


def test_fetch_satcat_cache_outlives_the_gp_window(
    isolated_settings, mock_celestrak_response
) -> None:
    """A 3-hour-old SATCAT landing is still fresh, where a GP one would not be.

    This is the whole point of the separate TTL: CelesTrak rebuilds the
    dump about once a day, so re-requesting it on the GP feed's 2-hour
    cycle would spend 6.7 MB of the daily budget on identical bytes.
    """
    mock_get = mock_celestrak_response(text=SATCAT_CSV_RESPONSE)
    path = fetch_satcat()
    _make_stale(path, hours=3)

    assert fetch_satcat() == path
    mock_get.assert_called_once()


def test_fetch_satcat_refetches_past_its_own_ttl(
    isolated_settings, mock_celestrak_response
) -> None:
    """Once the 24-hour window lapses, a new request is made."""
    mock_celestrak_response(text=SATCAT_CSV_RESPONSE)
    first = fetch_satcat()
    _make_stale(first, hours=25)

    mock_get = mock_celestrak_response(text=SATCAT_CSV_RESPONSE)
    second = fetch_satcat()

    mock_get.assert_called_once()
    assert second != first


def test_fetch_satcat_is_tallied_against_the_daily_budget(
    isolated_settings, mock_celestrak_response
) -> None:
    """The SATCAT dump is ~6.7 MB, so it must count against the same ledger."""
    mock_celestrak_response(text=SATCAT_CSV_RESPONSE)

    fetch_satcat()

    ledger = json.loads((isolated_settings.state_dir / "download_volume.json").read_text())
    assert ledger["bytes"] == len(SATCAT_CSV_RESPONSE.encode("utf-8"))
