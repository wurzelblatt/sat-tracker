"""Tests for loading the bronze Parquet dataset into Postgres.

These exercise a real Postgres instance (the one in `docker-compose.yml`)
rather than a mock, because the behaviour under test is precisely the
part a mock cannot check: that `COPY` and the `ON CONFLICT` primary-key
clause actually make repeated loads idempotent.

The whole module skips when Postgres is unreachable, so `uv run pytest`
still passes on a machine with no containers running.
"""

import json
from pathlib import Path

import psycopg
import pytest

from sat_tracker.config import settings
from sat_tracker.storage.parquet_writer import write_bronze_parquet
from sat_tracker.storage.postgres_loader import load_bronze_to_postgres

# Distinctive enough that cleanup can never touch real ingested rows.
TEST_SOURCE_FILE = "pytest-fixture_20260807T201737187277Z_pytest-id.csv"


def _postgres_available() -> bool:
    """Check whether the configured Postgres instance accepts connections."""
    try:
        with psycopg.connect(settings.postgres_dsn, connect_timeout=2):
            return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_available(),
    reason="Postgres is not reachable; run `docker compose up -d` to exercise these tests.",
)


def _delete_test_rows() -> None:
    """Remove only the rows this module's fixture inserts."""
    with psycopg.connect(settings.postgres_dsn) as connection:
        connection.execute(
            "DELETE FROM bronze.raw_gp WHERE source_file = %s", (TEST_SOURCE_FILE,)
        )
        connection.commit()


def _count_test_rows() -> int:
    """Count the rows belonging to this module's fixture."""
    with psycopg.connect(settings.postgres_dsn) as connection:
        result = connection.execute(
            "SELECT count(*) FROM bronze.raw_gp WHERE source_file = %s", (TEST_SOURCE_FILE,)
        ).fetchone()
    assert result is not None
    return result[0]


@pytest.fixture
def loaded_parquet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample_csv_payload: str,
    sample_ingested_at: str,
) -> str:
    """Build a throwaway Parquet dataset and guarantee warehouse cleanup."""
    parquet_root = str(tmp_path / "parquet")
    monkeypatch.setattr(settings, "parquet_root", parquet_root)

    csv_path = tmp_path / TEST_SOURCE_FILE
    csv_path.write_text(sample_csv_payload)
    csv_path.with_name(csv_path.name + ".meta.json").write_text(
        json.dumps(
            {
                "ingested_at": sample_ingested_at,
                "ingestion_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "source": "celestrak",
                "source_file": TEST_SOURCE_FILE,
                "target": "pytest-fixture",
            }
        )
    )
    write_bronze_parquet(csv_path)

    _delete_test_rows()
    yield parquet_root
    _delete_test_rows()


def test_loads_rows_into_bronze(loaded_parquet: str) -> None:
    """Parquet rows should arrive in `bronze.raw_gp` and be queryable."""
    inserted = load_bronze_to_postgres(source_file=TEST_SOURCE_FILE)

    assert inserted == 2
    assert _count_test_rows() == 2


def test_reloading_is_idempotent(loaded_parquet: str) -> None:
    """Re-running a load must not duplicate rows.

    This is the property that lets Airflow retry a failed task without
    corrupting the warehouse.
    """
    load_bronze_to_postgres(source_file=TEST_SOURCE_FILE)
    second_run = load_bronze_to_postgres(source_file=TEST_SOURCE_FILE)

    assert second_run == 0
    assert _count_test_rows() == 2


def test_lineage_survives_the_load(loaded_parquet: str) -> None:
    """Lineage columns must reach the warehouse, not just the Parquet file."""
    load_bronze_to_postgres(source_file=TEST_SOURCE_FILE)

    with psycopg.connect(settings.postgres_dsn) as connection:
        row = connection.execute(
            "SELECT source, target, ingestion_id::text, norad_cat_id "
            "FROM bronze.raw_gp WHERE source_file = %s ORDER BY norad_cat_id LIMIT 1",
            (TEST_SOURCE_FILE,),
        ).fetchone()

    assert row == (
        "celestrak",
        "pytest-fixture",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "100001",
    )


def test_unknown_source_file_loads_nothing(loaded_parquet: str) -> None:
    """Filtering on a source file that isn't in the dataset is a safe no-op."""
    assert load_bronze_to_postgres(source_file="does-not-exist.csv") == 0
