"""Convert raw CelesTrak CSV landings into a partitioned Parquet dataset.

The bronze landing zone holds CelesTrak's response byte-for-byte, which
is the right thing for auditability but a poor thing to query. This
module reads a landed `.csv` plus its `.meta.json` audit sidecar and
writes the same rows into a Parquet dataset partitioned by
``ingest_date=YYYY-MM-DD/hour=HH``, carrying the lineage columns
alongside the payload.

Every source column is kept as a string. Bronze's contract is fidelity:
a value CelesTrak sends that does not parse must survive the trip and
fail loudly in a silver-layer test, rather than being coerced or
dropped here. dbt does the casting downstream.

The destination is `settings.parquet_root`, resolved through
`pyarrow.fs`. A plain path writes locally; an ``s3://bucket/prefix``
URI writes to S3 with no other change anywhere in the pipeline.
"""

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.fs as pa_fs
import pyarrow.parquet as pq

from sat_tracker.config import settings

logger = logging.getLogger(__name__)

_PARTITION_COLUMNS = ["ingest_date", "hour"]

# Lineage columns prepended to every row, sourced from the .meta.json
# sidecar rather than the payload itself.
_LINEAGE_COLUMNS = ["ingest_ts", "ingestion_id", "source", "source_file", "target"]


class MissingSidecarError(RuntimeError):
    """Raised when a landed CSV has no readable `.meta.json` sidecar.

    Without the sidecar there is no ingestion timestamp or ID, so the
    rows cannot be partitioned or traced back to a fetch. Loading them
    anyway would put unattributable data in the warehouse.
    """


def _resolve_filesystem(root: str) -> tuple[pa_fs.FileSystem, str]:
    """Resolve a dataset root into a filesystem and a path within it.

    Args:
        root: Either a plain filesystem path (e.g. ``data/bronze_parquet``)
            or a URI (e.g. ``s3://my-bucket/bronze``).

    Returns:
        A ``(filesystem, path)`` pair suitable for
        `pyarrow.parquet.write_to_dataset`.
    """
    if "://" in root:
        return pa_fs.FileSystem.from_uri(root)
    return pa_fs.LocalFileSystem(), str(Path(root).resolve())


def _read_sidecar(csv_path: Path) -> dict:
    """Read the audit sidecar belonging to a landed CSV.

    Args:
        csv_path: Path of the landed `.csv` file.

    Returns:
        The parsed sidecar contents.

    Raises:
        MissingSidecarError: If the sidecar is absent or malformed.
    """
    sidecar = csv_path.with_name(csv_path.name + ".meta.json")
    if not sidecar.exists():
        raise MissingSidecarError(f"No metadata sidecar found for {csv_path}")
    try:
        return json.loads(sidecar.read_text())
    except json.JSONDecodeError as error:
        raise MissingSidecarError(f"Malformed metadata sidecar {sidecar}") from error


def _read_csv_as_strings(csv_path: Path) -> pa.Table:
    """Read a CelesTrak OMM-CSV file with every column typed as string.

    Args:
        csv_path: Path of the landed `.csv` file.

    Returns:
        The payload as an Arrow table with lowercased column names and
        all-string columns.
    """
    with csv_path.open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))

    table = pa_csv.read_csv(
        csv_path,
        convert_options=pa_csv.ConvertOptions(
            column_types=dict.fromkeys(header, pa.string()),
            # CelesTrak leaves genuinely absent values empty; keep them
            # as NULL rather than letting Arrow guess a sentinel.
            strings_can_be_null=True,
        ),
    )
    return table.rename_columns([name.lower() for name in header])


def write_bronze_parquet(csv_path: Path) -> str:
    """Write one landed CSV into the partitioned bronze Parquet dataset.

    Args:
        csv_path: Path of a `.csv` file in the bronze landing zone, as
            returned by `sat_tracker.ingest.celestrak_client.fetch_omm_csv`
            and friends.

    Returns:
        The dataset root the rows were written under.

    Raises:
        MissingSidecarError: If `csv_path` has no readable audit sidecar.
    """
    metadata = _read_sidecar(csv_path)
    ingest_ts = datetime.fromisoformat(metadata["ingested_at"])

    table = _read_csv_as_strings(csv_path)
    row_count = table.num_rows

    lineage = {
        "ingest_ts": pa.array([ingest_ts] * row_count, type=pa.timestamp("us", tz="UTC")),
        "ingestion_id": pa.array([metadata["ingestion_id"]] * row_count, type=pa.string()),
        "source": pa.array([metadata["source"]] * row_count, type=pa.string()),
        "source_file": pa.array([metadata["source_file"]] * row_count, type=pa.string()),
        "target": pa.array([metadata["target"]] * row_count, type=pa.string()),
        # Partition keys are stored as strings so the on-disk layout is
        # exactly ingest_date=YYYY-MM-DD/hour=HH.
        "ingest_date": pa.array([f"{ingest_ts:%Y-%m-%d}"] * row_count, type=pa.string()),
        "hour": pa.array([f"{ingest_ts:%H}"] * row_count, type=pa.string()),
    }

    for name, column in reversed(list(lineage.items())):
        table = table.add_column(0, name, column)
    # add_column(0, ...) in reverse order leaves lineage first, in
    # declaration order, with the payload columns following.

    filesystem, root_path = _resolve_filesystem(settings.parquet_root)
    pq.write_to_dataset(
        table,
        root_path=root_path,
        filesystem=filesystem,
        partition_cols=_PARTITION_COLUMNS,
        # One file per ingestion, named for the ingestion that produced
        # it, so a re-run overwrites its own output instead of silently
        # doubling the partition.
        basename_template=f"{metadata['ingestion_id']}-{{i}}.parquet",
        existing_data_behavior="overwrite_or_ignore",
    )

    logger.info(
        "Wrote %d rows to %s (ingest_date=%s/hour=%s)",
        row_count,
        settings.parquet_root,
        f"{ingest_ts:%Y-%m-%d}",
        f"{ingest_ts:%H}",
    )
    return settings.parquet_root
