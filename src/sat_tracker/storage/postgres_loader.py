"""Load a bronze Parquet dataset into its Postgres table.

Rows go in via ``COPY`` into an unlogged staging table, then move across
with a single ``INSERT ... ON CONFLICT DO NOTHING``. That combination is
both fast and idempotent: each dataset declares the primary key of its
destination table, so re-running a load for a file that is already
present is a no-op rather than a duplicate.

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
from sat_tracker.storage.datasets import GP, BronzeDataset
from sat_tracker.storage.parquet_writer import _resolve_filesystem

logger = logging.getLogger(__name__)


def _read_dataset_rows(
    source_file: str | None = None, dataset: BronzeDataset = GP
) -> list[tuple]:
    """Read a bronze Parquet dataset into row tuples in the dataset's column order.

    Args:
        source_file: If given, only rows whose ``source_file`` matches
            are returned, so a single ingestion can be loaded without
            rescanning the whole dataset.
        dataset: Which bronze feed to read.

    Returns:
        One tuple per row, with values ordered to match
        ``dataset.columns``.
    """
    filesystem, root_path = _resolve_filesystem(dataset.parquet_root)

    if filesystem.get_file_info(root_path).type == pa_fs.FileType.NotFound:
        return []

    parquet_dataset = ds.dataset(
        root_path,
        filesystem=filesystem,
        format="parquet",
        partitioning="hive",
    )

    table = parquet_dataset.to_table(
        columns=list(dataset.columns),
        filter=ds.field("source_file") == source_file if source_file else None,
    )
    return list(zip(*[column.to_pylist() for column in table.columns], strict=True))


def load_bronze_to_postgres(
    source_file: str | None = None, dataset: BronzeDataset = GP
) -> int:
    """Load bronze Parquet rows into their table, skipping ones already present.

    Args:
        source_file: If given, load only the rows produced by that
            landing file. Defaults to loading the entire dataset.
        dataset: Which bronze feed to load. Defaults to the GP/OMM feed.

    Returns:
        The number of rows actually inserted — zero when everything in
        the dataset had already been loaded.
    """
    rows = _read_dataset_rows(source_file, dataset)
    if not rows:
        logger.info("No %s Parquet rows found to load", dataset.name)
        return 0

    columns = ", ".join(dataset.columns)
    conflict_key = ", ".join(dataset.conflict_key)
    staging_table = f"staging_{dataset.name}"

    with psycopg.connect(settings.postgres_dsn) as connection, connection.cursor() as cursor:
        # Unlogged and temporary: this staging table exists only to give
        # COPY somewhere to land before the conflict-aware insert.
        cursor.execute(
            f"CREATE TEMP TABLE {staging_table} (LIKE {dataset.table} INCLUDING DEFAULTS) "
            "ON COMMIT DROP"
        )

        with cursor.copy(f"COPY {staging_table} ({columns}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)

        cursor.execute(
            f"INSERT INTO {dataset.table} ({columns}) "
            f"SELECT {columns} FROM {staging_table} "
            f"ON CONFLICT ({conflict_key}) DO NOTHING"
        )
        inserted = cursor.rowcount
        connection.commit()

    logger.info("Loaded %d new rows into %s (%d read)", inserted, dataset.table, len(rows))
    return inserted
