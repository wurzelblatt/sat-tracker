"""Tests for turning warehouse rows into propagated positions.

Most of these need no database. The ISS element set below is a real
one, so `propagate` can be exercised end to end against a satellite
whose orbit is well known — an altitude of roughly 420 km and an
inclination of 51.6 degrees bound the latitude it can reach.

The database-backed tests skip when Postgres is unreachable, matching
`test_postgres_loader.py`, so `uv run pytest` still passes with no
containers running.
"""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from sat_tracker.config import settings
from sat_tracker.propagate.elements import (
    Elset,
    _row_to_omm_fields,
    build_satrec_array,
    load_propagatable_elsets,
    propagate,
)

# A genuine ISS element set. Inclination 51.64 degrees caps the latitude
# it can ever reach, which is what makes the sanity assertions below
# meaningful rather than tautological.
ISS_ROW = {
    "norad_cat_id": 25544,
    "object_name": "ISS (ZARYA)",
    "object_id": "1998-067A",
    "classification_type": "U",
    "epoch": datetime(2021, 7, 18, 21, 59, 56, 518368, tzinfo=UTC),
    "mean_motion": 15.48815520,
    "eccentricity": 0.0003456,
    "inclination": 51.6423,
    "ra_of_asc_node": 194.0154,
    "arg_of_pericenter": 92.8797,
    "mean_anomaly": 333.0031,
    "mean_motion_dot": 0.00001449,
    "mean_motion_ddot": 0.0,
    "bstar": 0.0,
    "ephemeris_type": 0,
    "element_set_no": 999,
    "rev_at_epoch": 29323,
}


def _iss_elset() -> Elset:
    """Build an `Elset` from the sample row."""
    return Elset(
        norad_cat_id=ISS_ROW["norad_cat_id"],
        object_name=ISS_ROW["object_name"],
        epoch=ISS_ROW["epoch"],
        omm_fields=_row_to_omm_fields(ISS_ROW),
    )


# ── Field mapping ────────────────────────────────────────────────────


def test_row_maps_to_omm_field_names() -> None:
    """SGP4 reads the OMM standard's names, not our column names."""
    fields = _row_to_omm_fields(ISS_ROW)

    assert fields["NORAD_CAT_ID"] == "25544"
    assert fields["MEAN_MOTION"] == "15.4881552"
    assert "norad_cat_id" not in fields


def test_every_value_is_a_string() -> None:
    """`initialize` applies its own int()/float()/strptime() conversions.

    Passing a typed value works for the numeric fields by accident, but
    not for EPOCH, so the whole dict is stringified for consistency
    rather than relying on which fields happen to tolerate it.
    """
    assert all(isinstance(v, str) for v in _row_to_omm_fields(ISS_ROW).values())


def test_epoch_keeps_the_format_sgp4_parses() -> None:
    """EPOCH must match `%Y-%m-%dT%H:%M:%S.%f` exactly.

    `str(datetime)` produces a space separator and omits microseconds
    when they are zero, either of which makes `initialize` raise.
    """
    epoch = _row_to_omm_fields(ISS_ROW)["EPOCH"]

    assert epoch == "2021-07-18T21:59:56.518368"
    # Naive on purpose: this asserts the FORMAT is what sgp4 parses, and
    # sgp4's own strptime pattern carries no timezone either.
    assert datetime.strptime(epoch, "%Y-%m-%dT%H:%M:%S.%f")  # noqa: DTZ007


def test_epoch_with_whole_seconds_still_has_microseconds() -> None:
    """A satellite whose epoch lands on an exact second must not lose `.000000`."""
    row = dict(ISS_ROW, epoch=datetime(2026, 8, 16, 12, 0, 0, 0, tzinfo=UTC))

    assert _row_to_omm_fields(row)["EPOCH"] == "2026-08-16T12:00:00.000000"


# ── Propagation ──────────────────────────────────────────────────────


def test_results_come_back_in_input_order() -> None:
    """`propagate` zips results against the input positionally.

    If `SatrecArray` ever reordered its inputs, every position would be
    written under the wrong satellite — a silent corruption that no
    range check could catch, since all the values stay plausible.
    """
    other_row = dict(ISS_ROW, norad_cat_id=99999)
    elsets = [
        _iss_elset(),
        Elset(99999, "OTHER", ISS_ROW["epoch"], _row_to_omm_fields(other_row)),
    ]

    positions = propagate(elsets, ISS_ROW["epoch"])

    assert [p.norad_cat_id for p in positions] == [25544, 99999]


def test_propagate_puts_the_iss_in_a_plausible_place() -> None:
    """A real element set must yield a position consistent with the real orbit.

    Inclination 51.64 degrees means the ISS cannot reach beyond that
    latitude, and its altitude sits near 420 km. Either bound would be
    violated by a frame error, a unit error or a swapped axis.
    """
    position = propagate([_iss_elset()], ISS_ROW["epoch"])[0]

    assert abs(position.latitude_deg) <= 51.7
    assert -180.0 < position.longitude_deg <= 180.0
    assert 350.0 < position.altitude_km < 450.0


def test_propagate_returns_a_teme_state_vector() -> None:
    """Position and velocity must both be present, and physically sane.

    A LEO satellite orbits at roughly 7.7 km/s, and its distance from the
    centre of the Earth is the ellipsoid radius plus the altitude.
    """
    p = propagate([_iss_elset()], ISS_ROW["epoch"])[0]

    radius = (p.position_x_km**2 + p.position_y_km**2 + p.position_z_km**2) ** 0.5
    speed = (p.velocity_x_km_s**2 + p.velocity_y_km_s**2 + p.velocity_z_km_s**2) ** 0.5

    assert 6700.0 < radius < 6900.0
    assert 7.0 < speed < 8.5


def test_epoch_age_is_zero_at_epoch() -> None:
    """Propagating to the element set's own epoch is zero separation."""
    position = propagate([_iss_elset()], ISS_ROW["epoch"])[0]

    assert position.epoch_age_hours == pytest.approx(0.0, abs=1e-9)


def test_epoch_age_is_negative_for_a_future_epoch() -> None:
    """Signed, not absolute.

    XMM-Newton, Chandra and Cluster II-FM7 publish epochs up to two days
    ahead, which is normal for highly eccentric orbits. Taking the
    absolute value here would report them as stale rather than early.
    """
    when = ISS_ROW["epoch"] - timedelta(hours=6)

    assert propagate([_iss_elset()], when)[0].epoch_age_hours == pytest.approx(-6.0)


def test_propagate_moves_the_satellite_over_time() -> None:
    """Half an orbit later the ISS should be most of a planet away.

    Guards against a propagation that silently ignores the requested
    time and returns the position at epoch every call.
    """
    at_epoch = propagate([_iss_elset()], ISS_ROW["epoch"])[0]
    later = propagate([_iss_elset()], ISS_ROW["epoch"] + timedelta(minutes=46))[0]

    separation = (
        (later.position_x_km - at_epoch.position_x_km) ** 2
        + (later.position_y_km - at_epoch.position_y_km) ** 2
        + (later.position_z_km - at_epoch.position_z_km) ** 2
    ) ** 0.5

    assert separation > 10_000.0


def test_propagate_rejects_a_naive_datetime() -> None:
    """A naive datetime would be read as UTC without saying so."""
    with pytest.raises(ValueError, match="timezone-aware"):
        # Naive is the whole point of this test.
        propagate([_iss_elset()], datetime(2026, 8, 16, 12, 0, 0))  # noqa: DTZ001


def test_propagate_handles_an_empty_input() -> None:
    """No satellites is a valid state, not an error."""
    assert propagate([], datetime.now(UTC)) == []


def test_propagate_drops_satellites_sgp4_declines() -> None:
    """A non-zero error code means the position array holds meaningless numbers.

    Propagating an element set decades past its epoch lets atmospheric
    drag drive the orbit into nonsense, which SGP4 reports as an error
    code rather than raising. Those rows must not reach the warehouse.

    The BSTAR override matters: the ISS sample carries BSTAR = 0, which
    means no drag at all, and a drag-free orbit propagates cleanly for a
    century. Only a realistic drag term actually decays.
    """
    dragged = dict(ISS_ROW, bstar=0.0001)
    elset = Elset(25544, "ISS", ISS_ROW["epoch"], _row_to_omm_fields(dragged))

    assert propagate([elset], ISS_ROW["epoch"] + timedelta(days=365 * 40)) == []


# ── Warehouse-backed ─────────────────────────────────────────────────


def _postgres_available() -> bool:
    """Check whether the configured Postgres instance accepts connections."""
    try:
        with psycopg.connect(settings.postgres_dsn, connect_timeout=2):
            return True
    except psycopg.Error:
        return False


needs_postgres = pytest.mark.skipif(
    not _postgres_available(),
    reason="Postgres is not reachable; run `docker compose up -d` to exercise these tests.",
)


@needs_postgres
def test_load_propagatable_elsets_reads_the_warehouse() -> None:
    """Every loaded row must be usable by SGP4 without further massaging."""
    elsets = load_propagatable_elsets(limit=5)

    assert len(elsets) == 5
    assert all(isinstance(e.norad_cat_id, int) for e in elsets)
    assert all(e.epoch.tzinfo is not None for e in elsets)
    build_satrec_array(elsets)


@needs_postgres
def test_loaded_elsets_propagate_to_the_surface_of_the_earth() -> None:
    """Real warehouse data must produce positions above the ground, not below it.

    A frame or unit error anywhere in the chain shows up here as a
    negative altitude, which is the cheapest possible integration check.
    """
    positions = propagate(load_propagatable_elsets(limit=200), datetime.now(UTC))

    assert positions
    assert all(p.altitude_km > 0 for p in positions)
    assert all(-90.0 <= p.latitude_deg <= 90.0 for p in positions)
