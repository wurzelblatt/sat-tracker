"""Descriptors for the bronze datasets the storage layer can carry.

The storage layer was built to carry exactly one thing: CelesTrak GP/OMM
rows, into ``bronze.raw_gp``, under a single Parquet root. A second feed
cannot simply reuse that path. Its columns differ, so writing it beneath
the same dataset root would break the GP dataset's schema the next time
the root is read as a whole — and its primary key differs, so the
conflict clause that makes loads idempotent has to differ with it.

Table, column list, conflict key and dataset root therefore vary
together, which makes them one thing rather than four parameters. Each
dataset is described once here and passed to `write_bronze_parquet` and
`load_bronze_to_postgres`.

``parquet_root_setting`` holds the *name* of a `Settings` field rather
than its value. The root is resolved through the settings singleton at
call time, so pointing `SAT_TRACKER_PARQUET_ROOT` at an ``s3://`` URI —
or a test redirecting the root into a `tmp_path` — still takes effect.
Capturing the value at import time would silently ignore both.
"""

from dataclasses import dataclass
from pathlib import Path

from sat_tracker.config import settings

# Column order for both the COPY stream and the INSERT. Must match
# sql/init/02_bronze_raw_gp.sql.
_GP_COLUMNS = (
    "ingest_ts",
    "ingestion_id",
    "source",
    "source_file",
    "target",
    "object_name",
    "object_id",
    "epoch",
    "mean_motion",
    "eccentricity",
    "inclination",
    "ra_of_asc_node",
    "arg_of_pericenter",
    "mean_anomaly",
    "ephemeris_type",
    "classification_type",
    "norad_cat_id",
    "element_set_no",
    "rev_at_epoch",
    "bstar",
    "mean_motion_dot",
    "mean_motion_ddot",
)


# Column order for both the COPY stream and the INSERT. Must match
# sql/init/03_bronze_raw_satcat.sql.
_SATCAT_COLUMNS = (
    "ingest_ts",
    "ingestion_id",
    "source",
    "source_file",
    "target",
    "object_name",
    "object_id",
    "norad_cat_id",
    "object_type",
    "ops_status_code",
    "owner",
    "launch_date",
    "launch_site",
    "decay_date",
    "period",
    "inclination",
    "apogee",
    "perigee",
    "rcs",
    "data_status_code",
    "orbit_center",
    "orbit_type",
)


@dataclass(frozen=True)
class BronzeDataset:
    """One bronze feed, from landed CSV through Parquet to Postgres.

    Attributes:
        name: Short identifier, e.g. ``"gp"``. Also names the temporary
            staging table used during a load, so it must be a valid SQL
            identifier fragment.
        table: Fully qualified destination table, e.g. ``"bronze.raw_gp"``.
        columns: Column order used for both the ``COPY`` stream and the
            ``INSERT``. Must match the table's DDL.
        conflict_key: Columns forming the table's primary key, used in
            ``ON CONFLICT (...) DO NOTHING`` so a repeated load is a
            no-op rather than a duplicate.
        parquet_root_setting: Name of the `Settings` field holding this
            dataset's Parquet root. Resolved lazily via `parquet_root`.
        landing_dir_setting: Name of the `Settings` field holding the
            directory this feed's raw CSV landings are written to.
            Resolved lazily via `landing_dir`.
    """

    name: str
    table: str
    columns: tuple[str, ...]
    conflict_key: tuple[str, ...]
    parquet_root_setting: str
    landing_dir_setting: str

    @property
    def parquet_root(self) -> str:
        """Resolve this dataset's Parquet root from the settings singleton.

        Returns:
            The configured root, either a plain path or a URI such as
            ``s3://bucket/prefix``.
        """
        return getattr(settings, self.parquet_root_setting)

    @property
    def landing_dir(self) -> Path:
        """Resolve this dataset's CSV landing zone from the settings singleton.

        Returns:
            The configured landing directory. Not guaranteed to exist:
            it is created on first write.
        """
        return getattr(settings, self.landing_dir_setting)


GP = BronzeDataset(
    name="gp",
    table="bronze.raw_gp",
    columns=_GP_COLUMNS,
    conflict_key=("source", "source_file", "norad_cat_id"),
    parquet_root_setting="parquet_root",
    landing_dir_setting="bronze_dir",
)
"""CelesTrak GP/OMM element sets — the feed the pipeline was built around."""

SATCAT = BronzeDataset(
    name="satcat",
    table="bronze.raw_satcat",
    columns=_SATCAT_COLUMNS,
    conflict_key=("source", "source_file", "norad_cat_id"),
    parquet_root_setting="satcat_parquet_root",
    landing_dir_setting="satcat_dir",
)
"""CelesTrak's full satellite catalogue — the descriptive feed behind `gold.dim_object`."""

ALL_DATASETS = (GP, SATCAT)
"""Every bronze feed the storage layer knows how to carry."""
