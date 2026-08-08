"""Shared pytest fixtures for the sat-tracker test suite."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from sat_tracker.config import Settings
from sat_tracker.ingest import celestrak_client


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
    `tmp_path` instead of the real `data/bronze` / `data/sds`
    directories, then restores the originals afterwards.
    """
    monkeypatch.setattr(celestrak_client.settings, "bronze_dir", tmp_path / "bronze")
    monkeypatch.setattr(celestrak_client.settings, "sds_dir", tmp_path / "sds")
    return celestrak_client.settings


@pytest.fixture
def mock_celestrak_response(monkeypatch: pytest.MonkeyPatch):
    """Factory fixture that mocks the CelesTrak HTTP GET call.

    Returns a callable accepting `text=` (for CSV responses), `json_body=`
    (for JSON/SDS responses), and `status_code=` (default 200, for
    exercising the fail-fast error gate). Calling it patches
    `requests.Session.get` (used by `celestrak_client._get_celestrak`)
    to return a fake response with that content, and returns the mock
    so callers can assert on how it was invoked.
    """

    def _install(
        *, text: str | None = None, json_body: list | None = None, status_code: int = 200
    ) -> Mock:
        response = Mock()
        response.status_code = status_code
        response.url = "https://celestrak.org/NORAD/elements/gp.php"
        response.raise_for_status.return_value = None
        if text is not None:
            response.text = text
            response.content = text.encode("utf-8")
        if json_body is not None:
            response.json.return_value = json_body

        mock_get = Mock(return_value=response)
        monkeypatch.setattr(requests.Session, "get", mock_get)
        return mock_get

    return _install
