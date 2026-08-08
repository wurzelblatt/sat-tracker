"""Tests for the CelesTrak ingestion pipeline."""

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from OMM.OMM import OMM as OMMReader

from sat_tracker.ingest.celestrak_client import (
    CelesTrakFatalError,
    fetch_omm_csv,
    fetch_omm_csv_group,
    fetch_omm_sds,
    fetch_omm_sds_group,
    ingest,
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

    omm = OMMReader.GetRootAs(path.read_bytes())
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

    omm = OMMReader.GetRootAs(path.read_bytes())
    assert omm.NORAD_CAT_ID() == 25544


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
    stale_timestamp = (datetime.now(UTC) - timedelta(hours=3)).timestamp()
    os.utime(cached_path, (stale_timestamp, stale_timestamp))
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
