"""Shared pytest fixtures for the sat-tracker test suite."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from sat_tracker.config import Settings
from sat_tracker.ingest import celestrak_client

SAMPLE_CSV_PAYLOAD = (
    "OBJECT_NAME,OBJECT_ID,EPOCH,MEAN_MOTION,ECCENTRICITY,INCLINATION,"
    "RA_OF_ASC_NODE,ARG_OF_PERICENTER,MEAN_ANOMALY,EPHEMERIS_TYPE,"
    "CLASSIFICATION_TYPE,NORAD_CAT_ID,ELEMENT_SET_NO,REV_AT_EPOCH,BSTAR,"
    "MEAN_MOTION_DOT,MEAN_MOTION_DDOT\r\n"
    "STARLINK-1,2020-001A,2026-08-07T07:03:44.978976,15.72953539,0.0001,"
    "53.0,100.0,90.0,270.0,0,U,100001,999,1000,0.0001,1.4e-05,0\r\n"
    "STARLINK-2,2020-001B,2026-08-07T06:54:35.676288,15.72952004,0.0002,"
    "53.1,101.0,91.0,271.0,0,U,100002,999,1001,0.0002,1.5e-05,0\r\n"
)
"""A two-row CelesTrak OMM-CSV response, with the six-digit NORAD IDs and
exponent-notation values that make bronze's string-fidelity rule matter."""

SAMPLE_INGESTED_AT = "2026-08-07T20:17:37.187277+00:00"
"""Fixed ingestion timestamp, so partition assertions never depend on the clock."""


@pytest.fixture
def settings() -> Settings:
    """Provide a fresh `Settings` instance for tests.

    Returns:
        A `Settings` instance built from defaults, isolated from any
        `.env` file overrides that might affect the developer's shell.
    """
    return Settings(_env_file=None)


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Point the shared settings singleton at throwaway landing-zone directories.

    Mutates `sat_tracker.config.settings` (the same object imported by
    `sat_tracker.ingest.celestrak_client`) so ingestion tests write into
    `tmp_path` instead of the real `data/bronze` / `data/sds` /
    `data/state` directories, then restores the originals afterwards.

    Isolating `state_dir` matters as much as the landing zones: it holds
    the daily download-volume ledger, so without redirecting it the
    suite would accumulate against the developer's real budget and
    tests would leak state into one another.
    """
    monkeypatch.setattr(celestrak_client.settings, "bronze_dir", tmp_path / "bronze")
    monkeypatch.setattr(celestrak_client.settings, "sds_dir", tmp_path / "sds")
    monkeypatch.setattr(celestrak_client.settings, "state_dir", tmp_path / "state")
    return celestrak_client.settings


@pytest.fixture
def sample_csv_payload() -> str:
    """Provide the shared two-row OMM-CSV sample."""
    return SAMPLE_CSV_PAYLOAD


@pytest.fixture
def sample_ingested_at() -> str:
    """Provide the shared fixed ingestion timestamp."""
    return SAMPLE_INGESTED_AT


@pytest.fixture
def mock_celestrak_response(monkeypatch: pytest.MonkeyPatch):
    """Factory fixture that mocks the CelesTrak HTTP GET call.

    Returns a callable accepting `text=` (for CSV responses), `json_body=`
    (for JSON/SDS responses), `status_code=` (default 200, for exercising
    the fail-fast error gate) and `etag=` (recorded in the response
    headers so conditional-request behaviour can be tested). Calling it
    patches `requests.Session.get` (used by
    `celestrak_client._get_celestrak`) to return a fake response with
    that content, and returns the mock so callers can assert on how it
    was invoked.
    """

    def _install(
        *,
        text: str | None = None,
        json_body: list | None = None,
        status_code: int = 200,
        etag: str | None = None,
    ) -> Mock:
        response = Mock()
        response.status_code = status_code
        response.url = "https://celestrak.org/NORAD/elements/gp.php"
        response.raise_for_status.return_value = None
        response.headers = {"ETag": etag} if etag is not None else {}

        # `content` is always set: the volume ledger measures every
        # 200 response with len(response.content), regardless of format.
        if text is not None:
            response.text = text
            response.content = text.encode("utf-8")
        if json_body is not None:
            response.json.return_value = json_body
            if text is None:
                response.content = json.dumps(json_body).encode("utf-8")
        if text is None and json_body is None:
            response.content = b""

        mock_get = Mock(return_value=response)
        monkeypatch.setattr(requests.Session, "get", mock_get)
        return mock_get

    return _install
