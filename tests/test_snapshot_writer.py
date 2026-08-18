"""Tests for writing propagated positions into `gold.position_snapshot`.

These need a real Postgres with PostGIS, because the behaviour under test
is precisely what a mock cannot check: that `TRUNCATE` and `COPY` inside
one transaction replace the snapshot wholesale, and that the generated
`geo_point` column derives the geography the map will query.

The whole module skips when Postgres is unreachable, so `uv run pytest`
still passes on a machine with no containers running.
"""

from datetime import UTC, datetime

import psycopg
import pytest

from sat_tracker.config import settings
from sat_tracker.propagate.elements import Position
from sat_tracker.storage.snapshot_writer import _COLUMNS, write_position_snapshot


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

SNAPSHOT_TS = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
EPOCH = datetime(2026, 8, 16, 6, 0, 0, tzinfo=UTC)

TEST_TABLE = "gold.position_snapshot_pytest"
"""Scratch table these tests write to instead of the real snapshot.

`write_position_snapshot` truncates its whole destination — that is its
contract — so it cannot be scoped the way `test_postgres_loader.py` scopes
its writes with a distinctive `source_file`. Pointing the tests at a
throwaway table is the only way the suite can exercise the real TRUNCATE
and COPY without destroying whatever snapshot the developer had.

Built with `INCLUDING ALL` so it carries the generated `geo_point` column
and the indexes; without those, the geography and proximity tests below
would pass against a table that is not shaped like the real one.
"""


def _position(norad_cat_id: int, latitude: float, longitude: float) -> Position:
    """Build a position with a recognisable latitude and longitude."""
    return Position(
        norad_cat_id=norad_cat_id,
        snapshot_ts=SNAPSHOT_TS,
        epoch=EPOCH,
        epoch_age_hours=6.0,
        latitude_deg=latitude,
        longitude_deg=longitude,
        altitude_km=420.0,
        position_x_km=4000.0,
        position_y_km=3000.0,
        position_z_km=4000.0,
        velocity_x_km_s=-1.5,
        velocity_y_km_s=6.9,
        velocity_z_km_s=3.2,
    )


def _count() -> int:
    """Count the rows currently in the scratch table."""
    with psycopg.connect(settings.postgres_dsn) as connection:
        result = connection.execute(f"SELECT count(*) FROM {TEST_TABLE}")
        row = result.fetchone()
    assert row is not None
    return row[0]


@pytest.fixture(autouse=True)
def scratch_table():
    """Create a throwaway clone of the snapshot table, and drop it afterwards.

    `INCLUDING ALL` copies the generated `geo_point` expression and the
    GIST index, so these tests exercise the real DDL rather than a
    simplified stand-in. The real `gold.position_snapshot` is never
    touched.
    """
    with psycopg.connect(settings.postgres_dsn) as connection:
        connection.execute(f"DROP TABLE IF EXISTS {TEST_TABLE}")
        connection.execute(
            f"CREATE TABLE {TEST_TABLE} "
            "(LIKE gold.position_snapshot INCLUDING ALL)"
        )
        connection.commit()
    yield
    with psycopg.connect(settings.postgres_dsn) as connection:
        connection.execute(f"DROP TABLE IF EXISTS {TEST_TABLE}")
        connection.commit()


def test_the_real_snapshot_table_is_never_touched() -> None:
    """The suite must not destroy a developer's snapshot.

    It did once: an autouse fixture truncated gold.position_snapshot
    before and after every test, so running `pytest` silently wiped a
    freshly propagated snapshot. This test pins the fix.
    """
    with psycopg.connect(settings.postgres_dsn) as connection:
        before = connection.execute(
            "SELECT count(*) FROM gold.position_snapshot"
        ).fetchone()

    write_position_snapshot([_position(1, 10.0, 20.0)], table=TEST_TABLE)

    with psycopg.connect(settings.postgres_dsn) as connection:
        after = connection.execute(
            "SELECT count(*) FROM gold.position_snapshot"
        ).fetchone()

    assert before == after


def test_writes_every_position() -> None:
    """Each position becomes one row."""
    written = write_position_snapshot([_position(1, 10.0, 20.0), _position(2, -5.0, 30.0)], table=TEST_TABLE)

    assert written == 2
    assert _count() == 2


def test_a_second_run_replaces_rather_than_appends() -> None:
    """The table holds exactly one snapshot, so a rerun must not accumulate."""
    write_position_snapshot([_position(1, 10.0, 20.0), _position(2, -5.0, 30.0)], table=TEST_TABLE)
    write_position_snapshot([_position(3, 1.0, 2.0)], table=TEST_TABLE)

    assert _count() == 1


def test_an_empty_write_leaves_the_previous_snapshot_alone() -> None:
    """An empty result is far more likely an upstream failure than real news.

    Truncating on empty would blank the map whenever propagation failed,
    which is exactly when you would want the last known positions.
    """
    write_position_snapshot([_position(1, 10.0, 20.0)], table=TEST_TABLE)

    assert write_position_snapshot([], table=TEST_TABLE) == 0
    assert _count() == 1


def test_geo_point_is_generated_from_latitude_and_longitude() -> None:
    """The single test that proves the ST_MakePoint argument order.

    `ST_MakePoint` takes X then Y — longitude then latitude — and getting
    it backwards produces perfectly valid geometry that puts the satellite
    in the wrong hemisphere. Nothing else in the suite can catch that,
    because the DDL is what does the conversion.
    """
    write_position_snapshot([_position(25544, 51.5, -0.13)], table=TEST_TABLE)

    with psycopg.connect(settings.postgres_dsn) as connection:
        row = connection.execute(
            "SELECT ST_Y(geo_point::geometry), ST_X(geo_point::geometry) "
            f"FROM {TEST_TABLE} WHERE norad_cat_id = 25544"
        ).fetchone()

    assert row is not None
    assert row[0] == pytest.approx(51.5)  # ST_Y is latitude
    assert row[1] == pytest.approx(-0.13)  # ST_X is longitude


def test_the_spatial_index_answers_a_proximity_query() -> None:
    """A written row must be findable by the query the map will actually ask."""
    write_position_snapshot(
        [_position(1, 52.52, 13.40), _position(2, -33.9, 151.2)], table=TEST_TABLE
    )

    with psycopg.connect(settings.postgres_dsn) as connection:
        row = connection.execute(
            f"SELECT norad_cat_id FROM {TEST_TABLE} "
            "WHERE ST_DWithin(geo_point, "
            "ST_SetSRID(ST_MakePoint(13.40, 52.52), 4326)::geography, 50000)"
        ).fetchall()

    assert [r[0] for r in row] == [1]


def test_written_columns_match_the_table() -> None:
    """The column list is derived from Position, so it must match the DDL.

    COPY streams values positionally. A field added to Position without a
    matching column would fail loudly here rather than at the next
    propagation run.
    """
    with psycopg.connect(settings.postgres_dsn) as connection:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'gold' AND table_name = 'position_snapshot' "
            "AND is_generated = 'NEVER' ORDER BY ordinal_position"
        ).fetchall()

    assert list(_COLUMNS) == [r[0] for r in rows]
