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
        celestrak_satcat_url: URL of CelesTrak's full SATCAT dump — the
            static catalogue of every tracked object, including decayed
            ones. This is a plain file rather than the ``records.php``
            query endpoint, because the gold ``dim_object`` dimension
            must stay complete: an object that decays between a SATCAT
            pull and a GP pull would otherwise become an unresolvable
            key in the fact table.
        satcat_cache_ttl_hours: Cache window for the SATCAT dump.
            CelesTrak rebuilds the file about once a day, so the GP
            feed's 2-hour window would only re-download identical bytes.
        ingest_format: Which representation the CelesTrak ingestion
            pipeline should fetch and persist: ``"csv"`` for the compact
            OMM-CSV format written to the bronze landing zone, or
            ``"sds"`` for the JSON representation converted to a binary
            SDS FlatBuffer.
        bronze_dir: Local directory the GP/OMM CSV bronze landing zone
            is written to.
        satcat_dir: Local directory the SATCAT CSV landings are written
            to. Separate from `bronze_dir` so that a bulk load can tell
            the two feeds apart by location: landing filenames carry
            only a stem and an ingestion ID, not the schema, so a shared
            directory would leave nothing to dispatch on.
        sds_dir: Local directory binary ``.sds`` FlatBuffer files are
            written to.
        state_dir: Local directory for small pipeline state files, such
            as the daily download-volume ledger.
        user_agent: Value sent as the HTTP ``User-Agent`` header on every
            CelesTrak request. CelesTrak's usage policy expects machine
            clients to identify themselves with a contactable agent
            string; the default ``python-requests/x.y`` is exactly the
            kind of anonymous agent the policy targets.
        daily_volume_budget_bytes: Maximum number of bytes the pipeline
            will download from CelesTrak within a single UTC day.
            CelesTrak firewall-blocks IP addresses exceeding 100 MB/day,
            so this defaults to 80 MB to leave headroom for re-runs.
        parquet_root: Root of the partitioned Parquet bronze dataset
            for GP/OMM element sets. A plain path writes locally; an
            ``s3://bucket/prefix`` URI writes to S3 with no other code
            change, which is what keeps the S3 migration off the
            critical path.
        satcat_parquet_root: Root of the partitioned Parquet bronze
            dataset for SATCAT records. Kept separate from
            `parquet_root` because the two feeds have different
            schemas, and a Parquet dataset root must hold one schema.
        postgres_dsn: libpq connection string for the warehouse holding
            the bronze/silver/gold schemas. Defaults to the local
            ``docker-compose.yml`` instance, which publishes on 5433 to
            avoid clashing with a system Postgres on 5432.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SAT_TRACKER_",
        extra="ignore",
    )

    app_name: str = "sat-tracker"
    debug: bool = False
    celestrak_url: str = "https://celestrak.org/NORAD/elements/gp.php"
    celestrak_satcat_url: str = "https://celestrak.org/pub/satcat.csv"
    satcat_cache_ttl_hours: int = 24
    ingest_format: Literal["csv", "sds"] = "csv"
    bronze_dir: Path = Path("data/bronze")
    satcat_dir: Path = Path("data/bronze_satcat")
    sds_dir: Path = Path("data/sds")
    state_dir: Path = Path("data/state")
    user_agent: str = "sat-tracker/0.1.0 (+https://github.com/wurzelblatt/sat-tracker)"
    daily_volume_budget_bytes: int = 80 * 1024 * 1024
    parquet_root: str = "data/bronze_parquet"
    satcat_parquet_root: str = "data/bronze_satcat_parquet"
    postgres_dsn: str = "postgresql://sat_tracker:sat_tracker@localhost:5433/sat_tracker"


settings = Settings()
"""Module-level singleton settings instance, safe to import and reuse."""
