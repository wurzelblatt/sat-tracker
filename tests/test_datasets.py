"""Tests for the bronze dataset descriptors."""

import re
from pathlib import Path

import pytest

from sat_tracker.config import settings
from sat_tracker.storage.datasets import ALL_DATASETS, GP, BronzeDataset

_SQL_INIT_DIR = Path(__file__).resolve().parents[1] / "sql" / "init"

# Matches `CREATE TABLE IF NOT EXISTS <table> ( ... );` non-greedily up to
# the first line that closes the definition.
_CREATE_TABLE = r"CREATE TABLE IF NOT EXISTS\s+{table}\s*\((.*?)\n\);"


def _columns_declared_in_ddl(table: str) -> list[str]:
    """Extract the column names a table's `CREATE TABLE` statement declares, in order.

    Args:
        table: Fully qualified table name, e.g. ``"bronze.raw_gp"``.

    Returns:
        The declared column names, lowercased, in declaration order.

    Raises:
        AssertionError: If no init script declares `table`.
    """
    pattern = re.compile(_CREATE_TABLE.format(table=re.escape(table)), re.DOTALL)
    for sql_file in sorted(_SQL_INIT_DIR.glob("*.sql")):
        match = pattern.search(sql_file.read_text())
        if match is None:
            continue
        columns = []
        for line in match.group(1).splitlines():
            statement = line.split("--")[0].strip()
            # Skip blanks, comment-only lines and table-level constraints.
            if not statement or statement.upper().startswith(("PRIMARY KEY", "CONSTRAINT")):
                continue
            columns.append(statement.split()[0].lower())
        return columns

    raise AssertionError(f"No init script in {_SQL_INIT_DIR} declares {table}")


def test_parquet_root_resolves_from_settings_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The root must follow the settings singleton, not a value frozen at import.

    Capturing it when the module is imported would break both the
    `s3://` override the S3 migration depends on and every test that
    redirects the root into a `tmp_path`.
    """
    monkeypatch.setattr(settings, "parquet_root", "s3://example-bucket/bronze")

    assert GP.parquet_root == "s3://example-bucket/bronze"


@pytest.mark.parametrize("dataset", ALL_DATASETS, ids=lambda d: d.name)
def test_conflict_key_is_a_subset_of_the_columns(dataset: BronzeDataset) -> None:
    """Every conflict-key column must also be loaded.

    A key naming a column the INSERT never supplies would still build a
    valid-looking ``ON CONFLICT`` clause, and would only fail against a
    real warehouse at load time.
    """
    assert set(dataset.conflict_key) <= set(dataset.columns)


@pytest.mark.parametrize("dataset", ALL_DATASETS, ids=lambda d: d.name)
@pytest.mark.parametrize("setting", ["parquet_root_setting", "landing_dir_setting"])
def test_declared_settings_exist(dataset: BronzeDataset, setting: str) -> None:
    """A dataset must name real `Settings` fields, or it cannot resolve its paths."""
    assert hasattr(settings, getattr(dataset, setting))


@pytest.mark.parametrize("setting", ["parquet_root_setting", "landing_dir_setting"])
def test_datasets_do_not_share_a_location(setting: str) -> None:
    """Each feed needs its own landing zone and its own dataset root.

    A shared Parquet root cannot be read as one dataset, since the
    schemas differ. A shared landing zone is worse, because it is
    silent: landing filenames carry only a stem and an ingestion ID, so
    a bulk load globbing the directory has no way to tell which schema
    a given CSV holds.
    """
    locations = [getattr(dataset, setting) for dataset in ALL_DATASETS]

    assert len(set(locations)) == len(locations)


def test_dataset_names_are_unique() -> None:
    """Names key the temp staging table, so a collision would cross feeds."""
    names = [dataset.name for dataset in ALL_DATASETS]

    assert len(set(names)) == len(names)


@pytest.mark.parametrize("dataset", ALL_DATASETS, ids=lambda d: d.name)
def test_columns_have_no_duplicates(dataset: BronzeDataset) -> None:
    """A repeated column name would build a COPY the row tuples cannot fill."""
    assert len(set(dataset.columns)) == len(dataset.columns)


@pytest.mark.parametrize("dataset", ALL_DATASETS, ids=lambda d: d.name)
def test_columns_match_the_table_ddl(dataset: BronzeDataset) -> None:
    """The descriptor's column list must match its `CREATE TABLE`, in order.

    `COPY` streams values positionally, so a descriptor that drifts from
    the DDL does not fail loudly — it silently writes each value into
    the neighbouring column. Comparing the two lists here catches that
    at test time instead of in the warehouse.
    """
    assert list(dataset.columns) == _columns_declared_in_ddl(dataset.table)
