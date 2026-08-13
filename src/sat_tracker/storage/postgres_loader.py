"""Load the bronze Parquet dataset into `bronze.raw_gp` in Postgres.

Rows go in via ``COPY`` into an unlogged staging table, then move across
with a single ``INSERT ... ON CONFLICT DO NOTHING``. That combination is
both fast and idempotent: `bronze.raw_gp`'s primary key is
``(source, source_file, norad_cat_id)``, so re-running a load for a file
that is already present is a no-op rather than a duplicate.

``COPY`` is used in preference to an ORM bulk insert because the row
counts here (~30,000 per full-catalogue fetch) are exactly where
row-by-row inserts start to hurt and ``COPY`` does not, and because it
keeps SQLAlchemy out of the dependency tree entirely.
"""

import logging

import psycopg
import pyarrow.dataset as ds
import pyarrow.fs as pa_fs

from sat_tracker.config import settings
from sat_tracker.storage.parquet_writer import _resolve_filesystem

logger = logging.getLogger(__name__)

_TARGET_TABLE = "bronze.raw_gp"

# Column order used for both the COPY stream and the INSERT. Must match
# sql/init/02_bronze_raw_gp.sql.
_COLUMNS = [
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
]


def _read_dataset_rows(source_file: str | None = None) -> list[tuple]:
    """Read the bronze Parquet dataset into row tuples in `_COLUMNS` order.

    Args:
        source_file: If given, only rows whose ``source_file`` matches
            are returned, so a single ingestion can be loaded without
            rescanning the whole dataset.

    Returns:
        One tuple per row, with values ordered to match `_COLUMNS`.
    """
    filesystem, root_path = _resolve_filesystem(settings.parquet_root)

    if filesystem.get_file_info(root_path).type == pa_fs.FileType.NotFound:
        return []

    dataset = ds.dataset(
        root_path,
        filesystem=filesystem,
        format="parquet",
        partitioning="hive",
    )

    table = dataset.to_table(
        columns=_COLUMNS,
        filter=ds.field("source_file") == source_file if source_file else None,
    )
    return list(zip(*[column.to_pylist() for column in table.columns], strict=True))


def load_bronze_to_postgres(source_file: str | None = None) -> int:
    """Load bronze Parquet rows into `bronze.raw_gp`, skipping ones already present.

    Args:
        source_file: If given, load only the rows produced by that
            landing file. Defaults to loading the entire dataset.

    Returns:
        The number of rows actually inserted — zero when everything in
        the dataset had already been loaded.
    """
    rows = _read_dataset_rows(source_file)
    if not rows:
        logger.info("No bronze Parquet rows found to load")
        return 0

    columns = ", ".join(_COLUMNS)

    with psycopg.connect(settings.postgres_dsn) as connection, connection.cursor() as cursor:
        # Unlogged and temporary: this staging table exists only to give
        # COPY somewhere to land before the conflict-aware insert.
        cursor.execute(
            f"CREATE TEMP TABLE staging_raw_gp (LIKE {_TARGET_TABLE} INCLUDING DEFAULTS) "
            "ON COMMIT DROP"
        )

        with cursor.copy(f"COPY staging_raw_gp ({columns}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)

        cursor.execute(
            f"INSERT INTO {_TARGET_TABLE} ({columns}) "
            f"SELECT {columns} FROM staging_raw_gp "
            "ON CONFLICT (source, source_file, norad_cat_id) DO NOTHING"
        )
        inserted = cursor.rowcount
        connection.commit()

    logger.info("Loaded %d new rows into %s (%d read)", inserted, _TARGET_TABLE, len(rows))
    return inserted
