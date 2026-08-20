"""Tests for topocentric look angles: where a satellite appears from the ground.

Written before the implementation, like `test_frames.py`, and for the same
reason: every bug this function can have produces a bearing that points
somewhere real. A reversed argument order, a rotated position instead of a
range vector, an unnormalised azimuth — none of them produce an obviously
broken number, so "looks about right" is not evidence.

The assertions are geometric facts rather than reference data. They hold
for any correct implementation and need no external source.

Geometry used throughout:

    azimuth   clockwise from north — N 0, E 90, S 180, W 270
    elevation from the horizon — 0 at the horizon, 90 straight up,
              negative below (over the horizon, on the far side of Earth)
    range     straight-line distance, not distance over the ground
"""

import math

import pytest

from sat_tracker.propagate.frames import (
    WGS84_A_KM,
    ecef_to_look_angles,
    geodetic_to_ecef,
)

# Somewhere unremarkable and mid-latitude, so nothing degenerates.
BERLIN = (52.52, 13.40, 0.0)



def _look(
    observer: tuple[float, float, float], satellite: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Look angles from one geodetic point to another.

    Args:
        observer: ``(latitude_deg, longitude_deg, altitude_km)``.
        satellite: ``(latitude_deg, longitude_deg, altitude_km)``.

    Returns:
        ``(azimuth_deg, elevation_deg, range_km)``.
    """
    return ecef_to_look_angles(
        geodetic_to_ecef(*observer),
        geodetic_to_ecef(*satellite),
        observer[0],
        observer[1],
    )


# ── Elevation ────────────────────────────────────────────────────────


def test_a_satellite_directly_overhead_is_at_ninety_degrees() -> None:
    """The simplest case, and the one that catches a swapped E/N/U row."""
    _, elevation, _ = _look(BERLIN, (52.52, 13.40, 400.0))

    assert elevation == pytest.approx(90.0, abs=1e-6)


def test_a_satellite_overhead_is_exactly_its_altitude_away() -> None:
    """Straight up means range equals altitude, with no horizontal component.

    Catches a range computed from the satellite's own position vector
    rather than from the difference — that would return roughly an Earth
    radius instead of 400 km.
    """
    _, _, range_km = _look(BERLIN, (52.52, 13.40, 400.0))

    assert range_km == pytest.approx(400.0, abs=1e-6)


def test_the_antipode_is_far_below_the_horizon() -> None:
    """An object on the other side of the planet cannot be seen.

    Elevation must be strongly negative, and the range must exceed an
    Earth diameter.
    """
    azimuth, elevation, range_km = _look(BERLIN, (-52.52, 13.40 - 180.0, 400.0))

    assert elevation < -80.0
    assert range_km > 2 * WGS84_A_KM
    assert 0.0 <= azimuth < 360.0


def test_a_distant_satellite_sits_below_the_horizon() -> None:
    """Ninety degrees of arc away is over the horizon, not on it.

    The Earth curves away, so a satellite a quarter of the world distant
    is below the local horizontal even at 400 km up.
    """
    _, elevation, _ = _look(BERLIN, (52.52, 13.40 + 90.0, 400.0))

    assert elevation < 0.0


def test_elevation_rises_as_a_satellite_approaches_overhead() -> None:
    """Monotonic in angular separation, which a sign error would break."""
    elevations = [
        _look(BERLIN, (52.52, 13.40 + offset, 500.0))[1]
        for offset in (20.0, 10.0, 5.0, 0.0)
    ]

    assert elevations == sorted(elevations)


# ── Azimuth ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("description", "satellite", "expected_azimuth"),
    [
        ("due north", (57.52, 13.40, 400.0), 0.0),
        ("due east", (52.52, 21.40, 400.0), 90.0),
        ("due south", (47.52, 13.40, 400.0), 180.0),
        ("due west", (52.52, 5.40, 400.0), 270.0),
    ],
)
def test_azimuth_is_clockwise_from_north(
    description: str, satellite: tuple[float, float, float], expected_azimuth: float
) -> None:
    """The cardinal directions, which pin down both order and sign.

    `atan2(E, N)` takes East first, because azimuth is measured clockwise
    from north. Reversing the arguments reflects every bearing about 45
    degrees — still a valid-looking compass reading, and wrong.

    Tolerance is loose: a satellite due east along a *meridian* is not
    exactly due east along a *great circle* at this latitude, and the
    difference grows away from the equator.
    """
    azimuth, _, _ = _look(BERLIN, satellite)

    assert azimuth == pytest.approx(expected_azimuth, abs=6.0)


def test_azimuth_is_normalised_to_a_full_circle() -> None:
    """Due west must report 270, never -90.

    `atan2` returns (-180, 180], so anything west of north comes back
    negative unless it is normalised. A compass bearing has no negatives.
    """
    azimuth, _, _ = _look(BERLIN, (52.52, 5.40, 400.0))

    assert 0.0 <= azimuth < 360.0
    assert azimuth > 180.0


@pytest.mark.parametrize("longitude_offset", [-170.0, -90.0, -20.0, 20.0, 90.0, 170.0])
def test_azimuth_always_lands_inside_the_circle(longitude_offset: float) -> None:
    """No direction, from anywhere, may fall outside [0, 360)."""
    azimuth, _, _ = _look(BERLIN, (52.52, 13.40 + longitude_offset, 800.0))

    assert 0.0 <= azimuth < 360.0


# ── The range vector, which is the whole point ───────────────────────


def test_the_observer_position_actually_matters() -> None:
    """Two observers must not see the same satellite in the same place.

    This is the test that catches rotating the satellite's own position
    instead of the range vector. That mistake gives the direction from
    the centre of the Earth, which barely depends on where the observer
    stands — so these two would agree, and they must not.
    """
    satellite = (52.52, 13.40, 400.0)

    overhead = _look(BERLIN, satellite)
    far_away = _look((52.52, 3.40, 0.0), satellite)

    assert abs(overhead[1] - far_away[1]) > 10.0
    assert abs(overhead[2] - far_away[2]) > 100.0


def test_range_grows_with_horizontal_separation() -> None:
    """Further away is further away, at constant altitude."""
    ranges = [
        _look(BERLIN, (52.52, 13.40 + offset, 500.0))[2]
        for offset in (0.0, 5.0, 10.0, 20.0)
    ]

    assert ranges == sorted(ranges)


def test_range_matches_the_straight_line_distance() -> None:
    """Cross-check against the plain Euclidean distance between the two points.

    Independent of the rotation entirely: the rotation changes which
    direction the vector points, never how long it is.
    """
    observer, satellite = BERLIN, (48.0, 2.0, 700.0)

    expected = math.dist(geodetic_to_ecef(*observer), geodetic_to_ecef(*satellite))

    assert _look(observer, satellite)[2] == pytest.approx(expected, abs=1e-9)


# ── Awkward observers ────────────────────────────────────────────────


def test_an_observer_at_the_north_pole_sees_everything_to_the_south() -> None:
    """From the pole every direction is south, so azimuth is unconstrained.

    Elevation still has to be right, and the pole is where a latitude
    term in the rotation can quietly divide by zero.
    """
    _, elevation, range_km = _look((90.0, 0.0, 0.0), (90.0, 0.0, 600.0))

    assert elevation == pytest.approx(90.0, abs=1e-6)
    assert range_km == pytest.approx(600.0, abs=1e-6)


def test_an_observer_on_the_equator_works() -> None:
    """Where sin(latitude) is zero, which is the other degenerate row."""
    _, elevation, range_km = _look((0.0, 0.0, 0.0), (0.0, 0.0, 550.0))

    assert elevation == pytest.approx(90.0, abs=1e-6)
    assert range_km == pytest.approx(550.0, abs=1e-6)


def test_an_observer_across_the_antimeridian_is_handled() -> None:
    """Longitudes near 180 must not wrap into a wrong bearing."""
    azimuth, elevation, _ = _look((0.0, 179.0, 0.0), (0.0, -179.0, 400.0))

    assert 0.0 <= azimuth < 360.0
    assert elevation > 0.0
    # Two degrees east of the observer, so roughly due east.
    assert azimuth == pytest.approx(90.0, abs=10.0)


def test_an_observer_at_altitude_is_closer_to_a_satellite_overhead() -> None:
    """Standing on a mountain shortens the range by the height climbed."""
    sea_level = _look((52.52, 13.40, 0.0), (52.52, 13.40, 400.0))[2]
    mountain = _look((52.52, 13.40, 4.0), (52.52, 13.40, 400.0))[2]

    assert sea_level - mountain == pytest.approx(4.0, abs=1e-6)
