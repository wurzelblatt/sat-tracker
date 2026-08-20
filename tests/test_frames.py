"""Tests for the TEME to WGS84 coordinate transformation.

These are written before the implementation, so they all fail with
`NotImplementedError` until `frames.py` is filled in. That is the point:
every bug this transformation can have produces a plausible-looking
position somewhere real, so "it looks about right" is not a signal. Each
test below turns one such bug into a hard failure.

The oracle has three independent layers:

1. GMST is checked against `sgp4.propagation.gstime`, a separate
   implementation of the same IAU 1982 polynomial that ships with the
   propagator this project already depends on.
2. Geodetic conversion is checked against analytically known points —
   the equator, the poles, a known altitude — which are exact by
   construction rather than by reference data.
3. A round trip through `_geodetic_to_ecef` (the easy, closed-form
   direction, implemented here as the reference) must recover the input
   over arbitrary points, which catches errors the fixed points miss.
"""

import math

import pytest
from sgp4.propagation import gstime

from sat_tracker.propagate.frames import (
    J2000_JD,
    WGS84_A_KM,
    WGS84_B_KM,
    ecef_to_geodetic,
    geodetic_to_ecef,
    gmst_radians,
    teme_to_ecef,
    teme_to_geodetic,
)

# Was a private copy here, serving as the oracle for `ecef_to_geodetic`.
# It is now production code — the observer's position needs it — so the
# round-trip tests below exercise the real function rather than a
# lookalike that could drift from it.
_geodetic_to_ecef = geodetic_to_ecef


# ── GMST ─────────────────────────────────────────────────────────────


def test_gmst_at_j2000_matches_the_published_value() -> None:
    """GMST at J2000.0 is a standard constant: 280.46061837 degrees.

    Anchoring on a published value catches a wrong constant in the
    polynomial, which a self-consistency check never would.
    """
    assert math.degrees(gmst_radians(J2000_JD)) == pytest.approx(280.46061837, abs=1e-6)


@pytest.mark.parametrize(
    "jd",
    [
        2451545.0,  # J2000.0
        2440587.5,  # Unix epoch, 1970-01-01
        2461173.5,  # 2026-04-01
        2461267.0,  # 2026-07-03 12:00
        2469807.5,  # far future, exercises the cubic term
    ],
)
def test_gmst_matches_the_sgp4_reference_implementation(jd: float) -> None:
    """Agreement with an independent implementation of the same expression.

    `sgp4.propagation.gstime` is not this code and was not written for
    this project, so agreement across a century of dates is strong
    evidence the polynomial is transcribed correctly — including the
    quadratic and cubic terms, which the J2000 anchor cannot exercise
    because T is zero there.
    """
    assert gmst_radians(jd) == pytest.approx(gstime(jd), abs=1e-9)


@pytest.mark.parametrize("jd", [2451545.0, 2451545.3, 2461173.5, 2440587.5])
def test_gmst_is_normalised_to_one_turn(jd: float) -> None:
    """The result must be an angle in [0, 2*pi), never negative.

    A `math.fmod` in place of Python's `%` would return a negative value
    for dates before J2000, which then propagates into a longitude
    offset by a full turn.
    """
    result = gmst_radians(jd)

    assert 0.0 <= result < 2 * math.pi


def test_gmst_advances_by_a_sidereal_day_not_a_solar_day() -> None:
    """One solar day of rotation is 360.9856 degrees of sidereal time.

    The Earth turns slightly more than a full circle relative to the
    stars each day, because it has also moved along its orbit. If this
    came out as exactly 360 degrees, the polynomial's linear term would
    be wrong and positions would drift by ~1 degree per day.
    """
    advance = math.degrees(gmst_radians(J2000_JD + 1) - gmst_radians(J2000_JD)) % 360

    assert advance == pytest.approx(0.9856, abs=1e-3)


# ── TEME to ECEF ─────────────────────────────────────────────────────


def test_teme_to_ecef_is_identity_at_zero_rotation() -> None:
    """With GMST zero the two frames coincide, so nothing should move."""
    position = (4000.0, -2000.0, 5000.0)

    assert teme_to_ecef(position, 0.0) == pytest.approx(position, abs=1e-12)


def test_teme_to_ecef_quarter_turn_pins_down_the_sign() -> None:
    """A quarter turn maps the TEME X axis onto the ECEF MINUS Y axis.

    This is the test that catches a reversed rotation. The frame turns
    east by 90 degrees, so a fixed inertial point ends up 90 degrees west
    of where it started in Earth-fixed coordinates. Flipping the sign
    would put it on +Y instead, which is a perfectly valid position on
    the opposite side of the planet.
    """
    result = teme_to_ecef((1.0, 0.0, 0.0), math.pi / 2)

    assert result == pytest.approx((0.0, -1.0, 0.0), abs=1e-12)


@pytest.mark.parametrize("gmst", [0.0, 0.7, math.pi / 2, math.pi, 5.5])
def test_teme_to_ecef_preserves_length(gmst: float) -> None:
    """A rotation cannot change the distance from the centre of the Earth.

    Catches a mistyped matrix entry that a single fixed case might slip
    past — for instance cos where sin belongs.
    """
    position = (3000.0, 4000.0, 5000.0)
    expected = math.dist((0, 0, 0), position)

    assert math.dist((0, 0, 0), teme_to_ecef(position, gmst)) == pytest.approx(
        expected, rel=1e-12
    )


@pytest.mark.parametrize("gmst", [0.0, 1.3, math.pi])
def test_teme_to_ecef_leaves_z_alone(gmst: float) -> None:
    """Rotation is about the Z axis, so Z is untouched by construction."""
    assert teme_to_ecef((1000.0, 2000.0, 3000.0), gmst)[2] == pytest.approx(3000.0)


# ── ECEF to geodetic ─────────────────────────────────────────────────


def test_ecef_to_geodetic_at_the_equator_on_the_prime_meridian() -> None:
    """The simplest possible case: a point at (a, 0, 0) is 0N 0E, on the surface."""
    latitude, longitude, altitude = ecef_to_geodetic((WGS84_A_KM, 0.0, 0.0))

    assert latitude == pytest.approx(0.0, abs=1e-9)
    assert longitude == pytest.approx(0.0, abs=1e-9)
    assert altitude == pytest.approx(0.0, abs=1e-9)


def test_ecef_to_geodetic_at_ninety_east() -> None:
    """A point on the +Y axis is at 90 degrees EAST, not west.

    `atan2` takes Y first. Swapping the arguments sends this to 0E and
    puts the whole ground track in the wrong place while still looking
    like a real trajectory.
    """
    latitude, longitude, altitude = ecef_to_geodetic((0.0, WGS84_A_KM, 0.0))

    assert latitude == pytest.approx(0.0, abs=1e-9)
    assert longitude == pytest.approx(90.0, abs=1e-9)
    assert altitude == pytest.approx(0.0, abs=1e-9)


def test_ecef_to_geodetic_at_the_north_pole() -> None:
    """The pole is the degenerate case for the altitude formula.

    `alt = p / cos(lat) - N` divides by zero here, since p is 0 and
    cos(90 degrees) is 0. An implementation that does not handle it will
    raise or return a NaN.
    """
    latitude, _, altitude = ecef_to_geodetic((0.0, 0.0, WGS84_B_KM))

    assert latitude == pytest.approx(90.0, abs=1e-9)
    assert altitude == pytest.approx(0.0, abs=1e-6)


def test_ecef_to_geodetic_reports_altitude_above_the_ellipsoid() -> None:
    """400 km above the equator should read as 400 km, not as a radius."""
    _, _, altitude = ecef_to_geodetic((WGS84_A_KM + 400.0, 0.0, 0.0))

    assert altitude == pytest.approx(400.0, abs=1e-9)


def test_ecef_to_geodetic_handles_the_southern_hemisphere() -> None:
    """Negative Z must give negative latitude, not its absolute value."""
    latitude, _, _ = ecef_to_geodetic(_geodetic_to_ecef(-33.9, 18.4, 0.0))

    assert latitude == pytest.approx(-33.9, abs=1e-9)


def test_geodetic_latitude_is_not_geocentric_latitude() -> None:
    """The single most important test here.

    Geodetic latitude is measured from the ellipsoid NORMAL, which does
    not point at the centre of the Earth. Geocentric latitude —
    `atan2(z, p)` — is the obvious-looking answer and is wrong by up to
    0.19 degrees, about 21 km on the ground, peaking near 45 degrees.

    That error is far too small to look wrong on a world map and far too
    large to be acceptable. This test forces the distinction.
    """
    x, y, z = _geodetic_to_ecef(45.0, 0.0, 0.0)
    geocentric = math.degrees(math.atan2(z, math.hypot(x, y)))

    latitude, _, _ = ecef_to_geodetic((x, y, z))

    assert latitude == pytest.approx(45.0, abs=1e-9)
    # And confirm the two really do differ, so this test cannot pass by
    # accident on an ellipsoid that has been flattened to a sphere.
    assert abs(geocentric - 45.0) > 0.15


@pytest.mark.parametrize(
    ("latitude", "longitude", "altitude"),
    [
        (0.0, 0.0, 0.0),
        (51.5, -0.13, 0.0),  # London, sea level
        (-33.9, 151.2, 0.4),  # Sydney
        (89.9, 179.9, 800.0),  # near the pole, near the date line
        (-89.9, -179.9, 35786.0),  # near the other pole, at geostationary height
        (45.0, 90.0, 400.0),  # ISS altitude, where the geodetic gap peaks
        (23.5, -120.0, 20200.0),  # GPS altitude
        (0.0, 180.0, 1.0),  # exactly on the date line
    ],
)
def test_geodetic_round_trip(latitude: float, longitude: float, altitude: float) -> None:
    """geodetic -> ECEF -> geodetic must return the input.

    The fixed points above are all on axes or on the surface, where
    several wrong implementations happen to agree with the right one.
    These are not, and they span the altitudes this project actually
    propagates: LEO, GPS, and geostationary.
    """
    recovered = ecef_to_geodetic(_geodetic_to_ecef(latitude, longitude, altitude))

    # 1e-6 degrees is about 11 cm on the ground. The binding limit is
    # Bowring's closed form, which is exact on the ellipsoid surface but
    # drifts with altitude — measured at 1.2 mm at 400 km and 2 cm at GPS
    # height. That is roughly five orders of magnitude below SGP4's own
    # 1-3 km/day error, so a tighter bound here would be testing the
    # derivation of the formula rather than this implementation of it.
    assert recovered[0] == pytest.approx(latitude, abs=1e-6)
    assert recovered[1] == pytest.approx(longitude, abs=1e-6)
    # 1e-4 km is 10 cm, bounded by the same Bowring drift: 1.3 mm at
    # 400 km, 3.6 cm at GPS height. Note the fixed-point tests above hold
    # this function to exact agreement on the ellipsoid surface, where
    # the closed form has no error to hide behind — so loosening here
    # does not loosen the suite's grip on the algebra.
    assert recovered[2] == pytest.approx(altitude, abs=1e-4)


# ── The composed transformation ──────────────────────────────────────


def test_teme_to_geodetic_matches_its_parts() -> None:
    """The convenience wrapper must not diverge from the steps it composes."""
    position = (4000.0, 3000.0, 4000.0)
    jd = 2461173.5

    expected = ecef_to_geodetic(teme_to_ecef(position, gmst_radians(jd)))

    assert teme_to_geodetic(position, jd) == pytest.approx(expected, abs=1e-12)


def test_teme_to_geodetic_produces_coordinates_in_range() -> None:
    """Whatever comes out must be a point a map can draw."""
    latitude, longitude, altitude = teme_to_geodetic((4000.0, 3000.0, 4000.0), 2461173.5)

    assert -90.0 <= latitude <= 90.0
    assert -180.0 < longitude <= 180.0
    assert altitude > 0.0
