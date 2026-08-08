"""Application configuration for the sat-tracker project.

Defines a single, type-safe source of truth for runtime settings using
`pydantic-settings`. Values can be overridden via environment variables
(prefixed with ``SAT_TRACKER_``) or a local ``.env`` file, so nothing in
this module is a hardcoded credential or environment-specific value in
the sense the project rules forbid.
"""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Type-safe runtime settings for the sat-tracker application.

    Attributes:
        app_name: Human-readable name of the application, used in logging
            and diagnostics.
        debug: Whether the application should run in debug mode (more
            verbose logging, less strict error handling, etc.).
        celestrak_url: Base URL of the CelesTrak GP (General
            Perturbations) data endpoint used to fetch orbital element
            data such as OMM (Orbit Mean-Elements Message) records.
        ingest_format: Which representation the CelesTrak ingestion
            pipeline should fetch and persist: ``"csv"`` for the compact
            OMM-CSV format written to the bronze landing zone, or
            ``"sds"`` for the JSON representation converted to a binary
            SDS FlatBuffer.
        bronze_dir: Local directory the CSV bronze landing zone is
            written to.
        sds_dir: Local directory binary ``.sds`` FlatBuffer files are
            written to.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SAT_TRACKER_",
        extra="ignore",
    )

    app_name: str = "sat-tracker"
    debug: bool = False
    celestrak_url: str = "https://celestrak.org/NORAD/elements/gp.php"
    ingest_format: Literal["csv", "sds"] = "csv"
    bronze_dir: Path = Path("data/bronze")
    sds_dir: Path = Path("data/sds")


settings = Settings()
"""Module-level singleton settings instance, safe to import and reuse."""
