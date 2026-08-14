"""Tests for the bronze CSV to Parquet conversion."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.dataset as ds
import pytest

from sat_tracker.config import settings
from sat_tracker.storage.parquet_writer import MissingSidecarError, write_bronze_parquet


@pytest.fixture
def landed_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample_csv_payload: str,
    sample_ingested_at: str,
) -> Path:
    """Write a landed CSV plus sidecar, with the Parquet root redirected to tmp_path."""
    monkeypatch.setattr(settings, "parquet_root", str(tmp_path / "parquet"))

    csv_path = tmp_path / "bronze" / "starlink_20260807T201737187277Z_test-id.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(sample_csv_payload)
    csv_path.with_name(csv_path.name + ".meta.json").write_text(
        json.dumps(
            {
                "ingested_at": sample_ingested_at,
                "ingestion_id": "35874ee5-b1e6-4c03-a706-954a9428b32f",
                "source": "celestrak",
                "source_file": csv_path.name,
                "target": "starlink",
            }
        )
    )
    return csv_path


def _read_dataset(root: str) -> ds.Dataset:
    return ds.dataset(root, format="parquet", partitioning="hive")


def test_writes_hive_partitioned_dataset(landed_csv: Path) -> None:
    """Rows should land under `ingest_date=.../hour=...` taken from the sidecar."""
    root = write_bronze_parquet(landed_csv)

    partition = Path(root) / "ingest_date=2026-08-07" / "hour=20"
    assert partition.is_dir()
    assert list(partition.glob("*.parquet"))


def test_preserves_every_row_and_column(landed_csv: Path) -> None:
    """The payload should survive conversion intact, with lowercased column names."""
    root = write_bronze_parquet(landed_csv)
    table = _read_dataset(root).to_table()

    assert table.num_rows == 2
    assert {"object_name", "norad_cat_id", "mean_motion"} <= set(table.column_names)
    assert table.column("object_name").to_pylist() == ["STARLINK-1", "STARLINK-2"]


def test_source_columns_stay_strings(landed_csv: Path) -> None:
    """Bronze keeps CelesTrak's values as text so nothing is coerced or lost.

    Six-digit NORAD IDs and exponent-notation BSTAR values are exactly
    the kind of thing type inference would mangle.
    """
    root = write_bronze_parquet(landed_csv)
    table = _read_dataset(root).to_table()

    assert table.column("norad_cat_id").to_pylist() == ["100001", "100002"]
    assert table.column("mean_motion_dot").to_pylist() == ["1.4e-05", "1.5e-05"]


def test_attaches_lineage_columns(landed_csv: Path, sample_ingested_at: str) -> None:
    """Every row should carry the ingestion lineage from the sidecar."""
    root = write_bronze_parquet(landed_csv)
    table = _read_dataset(root).to_table()

    assert table.column("source").to_pylist() == ["celestrak"] * 2
    assert table.column("source_file").to_pylist() == [landed_csv.name] * 2
    assert table.column("target").to_pylist() == ["starlink"] * 2
    expected_ts = datetime.fromisoformat(sample_ingested_at)
    assert table.column("ingest_ts").to_pylist() == [expected_ts] * 2


def test_rewriting_same_ingestion_does_not_duplicate_rows(landed_csv: Path) -> None:
    """Re-converting the same landing must overwrite its file, not append a second one."""
    root = write_bronze_parquet(landed_csv)
    write_bronze_parquet(landed_csv)

    assert _read_dataset(root).to_table().num_rows == 2


def test_missing_sidecar_is_rejected(landed_csv: Path) -> None:
    """A CSV with no sidecar has no traceable lineage and must not be converted."""
    landed_csv.with_name(landed_csv.name + ".meta.json").unlink()

    with pytest.raises(MissingSidecarError):
        write_bronze_parquet(landed_csv)


def test_malformed_sidecar_is_rejected(landed_csv: Path) -> None:
    """A corrupt sidecar must fail loudly rather than load unattributable rows."""
    landed_csv.with_name(landed_csv.name + ".meta.json").write_text("{not json")

    with pytest.raises(MissingSidecarError):
        write_bronze_parquet(landed_csv)


def test_partition_reflects_ingestion_time_not_wall_clock(landed_csv: Path) -> None:
    """Partitioning must use the sidecar's ingest time, so re-runs are stable."""
    root = write_bronze_parquet(landed_csv)

    today = f"{datetime.now(UTC):%Y-%m-%d}"
    assert (Path(root) / "ingest_date=2026-08-07").is_dir()
    if today != "2026-08-07":
        assert not (Path(root) / f"ingest_date={today}").exists()
