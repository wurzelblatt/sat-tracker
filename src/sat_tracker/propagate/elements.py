"""Turn warehouse rows into SGP4 propagations and geodetic positions.

This is the bridge between `gold.fact_propagatable_elset` and
`frames.teme_to_geodetic`. It does three things, each a separate
function so each can be tested without the others:

    read rows  ->  build SatrecArray  ->  propagate and convert

Writing the results to `gold.position_snapshot` is deliberately NOT
here; that belongs to the CLI, so this module stays a pure computation
that a test or a notebook can call without a database write.

── Why the field names are shouted ──────────────────────────────────
`sgp4.omm.initialize` reads a dict keyed by the OMM standard's own
names — ``NORAD_CAT_ID``, ``MEAN_MOTION`` and so on — and it expects
their values as STRINGS, since it applies its own `int()`, `float()`
and `strptime()` conversions. Our warehouse columns are lowercase and
already typed, so `_row_to_omm_fields` translates back. That looks
redundant but it is the honest boundary: bronze stored strings, silver
typed them for testing, and SGP4 wants strings again.

── Error codes are not exceptional ──────────────────────────────────
`SatrecArray.sgp4` returns an error code per satellite per time rather
than raising. A non-zero code means SGP4 declined to produce a position
— a decayed orbit, an eccentricity out of range, a convergence failure.
Those satellites are dropped rather than written, because the position
array still contains numbers for them and those numbers are meaningless.
The count is logged: a sudden jump is a genuine data-quality signal.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import psycopg
from sgp4.api import Satrec, SatrecArray, jday
from sgp4.omm import initialize

from sat_tracker.config import settings
from sat_tracker.propagate.frames import teme_to_geodetic

logger = logging.getLogger(__name__)

# Warehouse column -> OMM field name. The values SGP4 wants are exactly
# the columns gold.fact_propagatable_elset carries, renamed.
_COLUMN_TO_OMM_FIELD = {
    "norad_cat_id": "NORAD_CAT_ID",
    "object_id": "OBJECT_ID",
    "classification_type": "CLASSIFICATION_TYPE",
    "epoch": "EPOCH",
    "mean_motion": "MEAN_MOTION",
    "eccentricity": "ECCENTRICITY",
    "inclination": "INCLINATION",
    "ra_of_asc_node": "RA_OF_ASC_NODE",
    "arg_of_pericenter": "ARG_OF_PERICENTER",
    "mean_anomaly": "MEAN_ANOMALY",
    "mean_motion_dot": "MEAN_MOTION_DOT",
    "mean_motion_ddot": "MEAN_MOTION_DDOT",
    "bstar": "BSTAR",
    "ephemeris_type": "EPHEMERIS_TYPE",
    "element_set_no": "ELEMENT_SET_NO",
    "rev_at_epoch": "REV_AT_EPOCH",
}

# The exact format sgp4.omm.initialize parses EPOCH with. Microseconds
# are not optional: its strptime pattern ends in %f.
_OMM_EPOCH_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"

_SELECT_PROPAGATABLE = f"""
    SELECT norad_cat_id, object_name, {", ".join(
        c for c in _COLUMN_TO_OMM_FIELD if c != "norad_cat_id"
    )}
    FROM gold.fact_propagatable_elset
    ORDER BY norad_cat_id
"""


@dataclass(frozen=True)
class Elset:
    """One satellite's current element set, ready for SGP4.

    Attributes:
        norad_cat_id: NORAD catalog number, kept typed because it is the
            key every downstream row is written under.
        object_name: Human-readable name, carried for logging and for
            readable output; SGP4 never sees it.
        epoch: The instant the element set describes, as a timezone-aware
            datetime. Kept alongside `omm_fields` because the age of the
            element set is what expresses confidence in the result.
        omm_fields: The same element set in the string-keyed, string-valued
            form `sgp4.omm.initialize` consumes.
    """

    norad_cat_id: int
    object_name: str | None
    epoch: datetime
    omm_fields: dict[str, str]


@dataclass(frozen=True)
class Position:
    """One satellite's propagated position, shaped for `gold.position_snapshot`.

    Field names match the table's columns one for one, apart from
    `geo_point`, which Postgres generates from latitude and longitude.

    Attributes:
        norad_cat_id: NORAD catalog number.
        snapshot_ts: The instant propagated TO.
        epoch: The element set epoch propagated FROM.
        epoch_age_hours: `snapshot_ts - epoch` in hours. SIGNED: negative
            when the element set epoch lies in the future, which is normal
            for highly eccentric orbits.
        latitude_deg: WGS84 geodetic latitude.
        longitude_deg: WGS84 longitude.
        altitude_km: Height above the WGS84 ellipsoid.
        position_x_km: TEME position, X.
        position_y_km: TEME position, Y.
        position_z_km: TEME position, Z.
        velocity_x_km_s: TEME velocity, X.
        velocity_y_km_s: TEME velocity, Y.
        velocity_z_km_s: TEME velocity, Z.
    """

    norad_cat_id: int
    snapshot_ts: datetime
    epoch: datetime
    epoch_age_hours: float
    latitude_deg: float
    longitude_deg: float
    altitude_km: float
    position_x_km: float
    position_y_km: float
    position_z_km: float
    velocity_x_km_s: float
    velocity_y_km_s: float
    velocity_z_km_s: float


def _row_to_omm_fields(row: dict) -> dict[str, str]:
    """Rename and stringify a warehouse row into OMM fields.

    Args:
        row: One `gold.fact_propagatable_elset` row, keyed by column name.

    Returns:
        The same element set keyed by OMM field name, with every value a
        string, as `sgp4.omm.initialize` requires.
    """
    fields = {}
    for column, omm_field in _COLUMN_TO_OMM_FIELD.items():
        value = row[column]
        # EPOCH is parsed by strptime with an explicit format, so it
        # cannot go through str(): a datetime's default repr uses a space
        # separator and drops microseconds when they are zero.
        if column == "epoch":
            fields[omm_field] = value.strftime(_OMM_EPOCH_FORMAT)
        else:
            fields[omm_field] = str(value)
    return fields


def load_propagatable_elsets(limit: int | None = None) -> list[Elset]:
    """Read every propagatable element set from the warehouse.

    Reads `gold.fact_propagatable_elset`, which has already excluded
    objects that have decayed or do not orbit Earth.

    Args:
        limit: If given, read only this many satellites. Intended for
            quick manual runs, not for production.

    Returns:
        One `Elset` per satellite, ordered by NORAD catalog number so a
        run is reproducible.
    """
    query = _SELECT_PROPAGATABLE + (f"    LIMIT {int(limit)}" if limit else "")

    with (
        psycopg.connect(settings.postgres_dsn) as connection,
        connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
    ):
        rows = cursor.execute(query).fetchall()

    elsets = [
        Elset(
            norad_cat_id=row["norad_cat_id"],
            object_name=row["object_name"],
            epoch=row["epoch"],
            omm_fields=_row_to_omm_fields(row),
        )
        for row in rows
    ]

    logger.info("Loaded %d propagatable element sets", len(elsets))
    return elsets


def build_satrec_array(elsets: list[Elset]) -> SatrecArray:
    """Initialise one SGP4 propagator per element set, vectorised.

    `SatrecArray` exists so that all satellites are propagated in one
    call into C, rather than once per satellite from Python. At ~16,000
    objects that is the difference between milliseconds and seconds.

    Args:
        elsets: Element sets from `load_propagatable_elsets`.

    Returns:
        A `SatrecArray` in the same order as `elsets`, so results can be
        zipped back against them positionally.
    """
    satrecs = []
    for elset in elsets:
        satrec = Satrec()
        # Uses WGS72 gravity constants by default, which is correct:
        # WGS72 is part of the SGP4 theory itself. The WGS84 ellipsoid
        # enters later, in frames.ecef_to_geodetic, for a different job.
        initialize(satrec, elset.omm_fields)
        satrecs.append(satrec)

    return SatrecArray(satrecs)


def propagate(elsets: list[Elset], when: datetime) -> list[Position]:
    """Propagate every element set to one instant and convert to geodetic.

    Args:
        elsets: Element sets from `load_propagatable_elsets`.
        when: The instant to propagate to. Must be timezone-aware; it is
            converted to UTC, since SGP4 has no concept of a timezone and
            a naive datetime would silently be read as UTC regardless.

    Returns:
        One `Position` per satellite SGP4 produced a valid answer for.
        Satellites whose propagation returned a non-zero error code are
        omitted, so this may be shorter than `elsets`.

    Raises:
        ValueError: If `when` is timezone-naive.
    """
    if when.tzinfo is None:
        raise ValueError(
            "`when` must be timezone-aware; a naive datetime would be read "
            "as UTC without saying so."
        )
    when = when.astimezone(UTC)

    if not elsets:
        return []

    satrec_array = build_satrec_array(elsets)

    # jday splits the Julian date into a whole number and a fraction so
    # that the fractional part keeps full float precision, rather than
    # being crushed against a ~2.46 million integer part.
    julian_day, julian_fraction = jday(
        when.year,
        when.month,
        when.day,
        when.hour,
        when.minute,
        when.second + when.microsecond / 1_000_000,
    )

    # Must be numpy arrays, not lists: SatrecArray.sgp4 calls .astype()
    # on both before handing them to the C extension.
    # Shapes returned: errors (nsat, 1), positions and velocities (nsat, 1, 3).
    errors, positions, velocities = satrec_array.sgp4(
        np.array([julian_day]), np.array([julian_fraction])
    )

    # frames.gmst_radians takes a single Julian date. Recombining costs
    # about 0.1 ms of precision at this magnitude, which is four orders
    # below the UT1 approximation the module already accepts.
    jd_total = julian_day + julian_fraction

    results: list[Position] = []
    failed = 0

    for elset, error, position, velocity in zip(
        elsets, errors, positions, velocities, strict=True
    ):
        if error[0] != 0:
            failed += 1
            continue

        x, y, z = (float(component) for component in position[0])
        vx, vy, vz = (float(component) for component in velocity[0])
        latitude, longitude, altitude = teme_to_geodetic((x, y, z), jd_total)

        results.append(
            Position(
                norad_cat_id=elset.norad_cat_id,
                snapshot_ts=when,
                epoch=elset.epoch,
                # Signed on purpose: a future epoch gives a negative age.
                epoch_age_hours=(when - elset.epoch).total_seconds() / 3600.0,
                latitude_deg=float(latitude),
                longitude_deg=float(longitude),
                altitude_km=float(altitude),
                position_x_km=x,
                position_y_km=y,
                position_z_km=z,
                velocity_x_km_s=vx,
                velocity_y_km_s=vy,
                velocity_z_km_s=vz,
            )
        )

    if failed:
        logger.warning(
            "SGP4 declined %d of %d satellites; they are omitted from the snapshot",
            failed,
            len(elsets),
        )
    logger.info("Propagated %d satellites to %s", len(results), when.isoformat())

    return results
