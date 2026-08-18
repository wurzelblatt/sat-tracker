"""Persist propagated positions into `gold.position_snapshot`.

`elements.propagate` returns positions in memory; the Streamlit map
queries Postgres. This module is the step between them, and it is the
only place in the project that writes to a gold table.

── Why one transaction ──────────────────────────────────────────────
The table holds exactly one snapshot, so a run replaces it wholesale:
``TRUNCATE`` then ``COPY``. Both statements run inside a single
transaction, which matters more than it looks.

``TRUNCATE`` takes an ACCESS EXCLUSIVE lock, so a reader arriving
mid-write blocks until commit and then sees the complete new snapshot.
Run as two separate statements, that same reader could land in the gap
between them and render an empty map.

PostgreSQL also makes ``TRUNCATE`` transactional, which is what allows
this at all — if the ``COPY`` fails halfway, the previous snapshot is
still there, untouched. (MySQL forces an implicit commit and would
genuinely leave the table empty.)

``TRUNCATE`` rather than ``DELETE FROM`` because ``DELETE`` only marks
rows dead and leaves the space for VACUUM to reclaim. Replacing 16,000
rows many times a day would steadily bloat both the table and its GIST
index.
"""

import logging
from dataclasses import astuple, fields

import psycopg

from sat_tracker.config import settings
from sat_tracker.propagate.elements import Position

logger = logging.getLogger(__name__)

_TABLE = "gold.position_snapshot"

# Derived from the dataclass rather than written out by hand, so a field
# added to Position cannot silently stop being written.
#
# geo_point is absent by construction, which is required rather than
# incidental: it is GENERATED ALWAYS ... STORED, and Postgres rejects any
# COPY that supplies a value for a generated column.
_COLUMNS = tuple(field.name for field in fields(Position))


def write_position_snapshot(positions: list[Position], table: str = _TABLE) -> int:
    """Replace the stored snapshot with `positions`, atomically.

    Args:
        positions: Positions from `sat_tracker.propagate.elements.propagate`.
            An empty list is treated as "nothing to say" rather than
            "everything is gone": the existing snapshot is left in place
            and nothing is truncated, because an empty write is far more
            likely to be an upstream failure than a real report that no
            satellite exists.
        table: Destination table. Defaults to `gold.position_snapshot`.

            This exists because the function's contract is "replace
            everything", which cannot be scoped the way
            `postgres_loader` scopes its damage with a `source_file`
            filter. Without it, testing this function against a
            development warehouse necessarily destroys whatever snapshot
            was there — which it did, once. Tests target a scratch table
            instead. It is also the seam a blue/green write would need.

    Returns:
        The number of rows written. Zero when `positions` is empty, in
        which case the table is untouched.
    """
    if not positions:
        logger.warning(
            "No positions to write; leaving the existing snapshot in place "
            "rather than truncating it"
        )
        return 0

    columns = ", ".join(_COLUMNS)

    with (
        psycopg.connect(settings.postgres_dsn) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(f"TRUNCATE {table}")

        with cursor.copy(f"COPY {table} ({columns}) FROM STDIN") as copy:
            for position in positions:
                copy.write_row(astuple(position))

        connection.commit()

    logger.info(
        "Wrote %d positions to %s at %s",
        len(positions),
        table,
        positions[0].snapshot_ts.isoformat(),
    )
    return len(positions)
