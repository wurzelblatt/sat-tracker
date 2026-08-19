"""Tests for tracing a satellite's path over one revolution.

The interesting assertions here are physical invariants rather than
golden values: an orbit closes, a satellite cannot exceed its
inclination, a near-circular orbit holds its altitude. Those hold for
any correct implementation and need no reference data, which makes them
better evidence than a stored expected output.

The ISS element set below is real, so the numbers it produces can be
checked against a well-known orbit.
"""

import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from test_elements import ISS_ROW

from sat_tracker.propagate.elements import Elset, _row_to_omm_fields
from sat_tracker.propagate.tracks import (
    DEFAULT_SAMPLES,
    Track,
    orbit_track,
    orbit_tracks,
    period_minutes,
)

WHEN = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

# The ISS orbits at 51.64 degrees inclination, which bounds the
# GEOCENTRIC latitude it can reach. These tests report GEODETIC
# latitude, which runs up to ~0.19 degrees higher at mid-latitudes
# because the ellipsoid normal does not point at the centre of the
# Earth. 51.9 leaves room for that without letting a real error through.
ISS_MAX_LATITUDE = 51.9


def _iss_elset(**overrides) -> Elset:
    """Build an `Elset` from the shared ISS sample."""
    row = ISS_ROW | overrides
    return Elset(
        norad_cat_id=row["norad_cat_id"],
        object_name=row["object_name"],
        epoch=row["epoch"],
        omm_fields=_row_to_omm_fields(row),
    )


# ── period_minutes ───────────────────────────────────────────────────


def test_period_comes_from_mean_motion() -> None:
    """1440 minutes divided by revolutions per day.

    The ISS completes about 15.49 revolutions a day, so a little under
    93 minutes each.
    """
    assert period_minutes(_iss_elset()) == pytest.approx(92.97, abs=0.05)


def test_a_geostationary_period_is_about_a_day() -> None:
    """One revolution per day is the definition of geostationary."""
    assert period_minutes(_iss_elset(mean_motion=1.0027)) == pytest.approx(
        1436.0, abs=1.0
    )


def test_zero_mean_motion_is_rejected() -> None:
    """A period cannot be derived from it, and the error should say so."""
    with pytest.raises(ValueError, match="mean motion"):
        period_minutes(_iss_elset(mean_motion=0.0))


# ── orbit_track ──────────────────────────────────────────────────────


def test_a_track_has_the_requested_number_of_points() -> None:
    """One sample per step, with the endpoint excluded."""
    assert len(orbit_track(_iss_elset(), WHEN).points) == DEFAULT_SAMPLES


def test_a_track_covers_exactly_one_revolution() -> None:
    """The window is the satellite's own period, not a fixed span."""
    assert orbit_track(_iss_elset(), WHEN).period_minutes == pytest.approx(
        92.97, abs=0.05
    )


def test_the_orbit_closes_in_latitude_and_altitude() -> None:
    """After one period the satellite is back where it started.

    This is the strongest self-validating check available: it needs no
    reference data, and it exercises the period calculation, the time
    stepping, the propagation and the frame conversion at once. If any
    of them is wrong the track will not close.

    Longitude is deliberately excluded — the Earth turns roughly 23
    degrees during a 93-minute orbit, so the ground track drifts west
    each revolution rather than repeating. That westward march is the
    reason ground tracks are sinusoids that never quite overlay.
    """
    points = orbit_track(_iss_elset(), WHEN, samples=360).points
    # Compare against one step before a full period, since the endpoint
    # is excluded from the sample grid.
    first, last = points[0], points[-1]
    step = 360 / len(points)

    assert last[0] == pytest.approx(first[0], abs=2.0 * step)
    assert last[2] == pytest.approx(first[2], abs=5.0)


def test_a_track_respects_the_orbit_inclination() -> None:
    """A satellite cannot reach a latitude above its inclination.

    The orbital plane passes through the centre of the Earth, so the
    ground track is bounded by the inclination. This is the same
    physical law that validated the full snapshot.
    """
    latitudes = [point[0] for point in orbit_track(_iss_elset(), WHEN).points]

    assert max(abs(latitude) for latitude in latitudes) <= ISS_MAX_LATITUDE


def test_a_track_reaches_both_hemispheres() -> None:
    """A 51-degree orbit crosses the equator twice per revolution.

    Guards against a track that is bounded correctly but never moves —
    which the inclination test alone would not catch.
    """
    latitudes = [point[0] for point in orbit_track(_iss_elset(), WHEN).points]

    assert min(latitudes) < -40.0
    assert max(latitudes) > 40.0


def test_a_near_circular_orbit_holds_its_altitude() -> None:
    """The ISS orbit is nearly circular, so altitude barely varies.

    A large swing would mean the conversion is losing the ellipsoid, or
    that eccentricity is being misread.
    """
    altitudes = [point[2] for point in orbit_track(_iss_elset(), WHEN).points]

    assert 380.0 < min(altitudes) < max(altitudes) < 470.0
    assert max(altitudes) - min(altitudes) < 60.0


def test_a_track_moves_monotonically_through_time() -> None:
    """Successive points must be neighbours, not scattered.

    At ~1 sample per minute the ISS travels about 4 degrees of arc, so
    consecutive latitudes cannot jump far. Catches an unsorted or
    mis-broadcast time grid.
    """
    points = orbit_track(_iss_elset(), WHEN).points
    steps = [abs(b[0] - a[0]) for a, b in pairwise(points)]

    assert max(steps) < 15.0


def test_a_naive_datetime_is_rejected() -> None:
    """As with `propagate`, silence about the timezone is not acceptable."""
    with pytest.raises(ValueError, match="timezone-aware"):
        orbit_track(_iss_elset(), datetime(2026, 8, 18, 12, 0, 0))  # noqa: DTZ001


def test_a_line_needs_at_least_two_points() -> None:
    """One sample is a dot, not a track."""
    with pytest.raises(ValueError, match="at least 2"):
        orbit_track(_iss_elset(), WHEN, samples=1)


def test_the_start_instant_shifts_the_track() -> None:
    """Tracing from a different time must trace a different arc."""
    first = orbit_track(_iss_elset(), WHEN).points[0]
    later = orbit_track(_iss_elset(), WHEN + timedelta(minutes=20)).points[0]

    assert abs(later[0] - first[0]) > 1.0


# ── orbit_tracks ─────────────────────────────────────────────────────


def test_several_satellites_are_traced_independently() -> None:
    """Each gets its own period rather than a shared window.

    A geostationary satellite takes 15 times as long to come round as
    the ISS; sampling both over one shared span would truncate the slow
    orbit and oversample the fast one.
    """
    tracks = orbit_tracks(
        [_iss_elset(), _iss_elset(norad_cat_id=99999, mean_motion=1.0027)], WHEN
    )

    assert len(tracks) == 2
    assert tracks[0].period_minutes < 100.0
    assert tracks[1].period_minutes > 1400.0


def test_tracing_nothing_is_not_an_error() -> None:
    """An empty selection is a valid state."""
    assert orbit_tracks([], WHEN) == []


def test_a_satellite_without_a_usable_period_is_skipped() -> None:
    """One bad element set must not lose the whole selection."""
    tracks = orbit_tracks([_iss_elset(mean_motion=0.0), _iss_elset()], WHEN)

    assert len(tracks) == 1
    assert isinstance(tracks[0], Track)


# ── Ground track versus orbit path ───────────────────────────────────


def _gap_degrees(points: list[tuple[float, float, float]]) -> float:
    """Angular distance between a track's first and last point."""
    first, last = points[0], points[-1]
    return math.dist((first[0], first[1]), (last[0], last[1]))


def test_a_ground_track_does_not_close() -> None:
    """It is not supposed to, and that is the point.

    Each sample is converted at its own instant, so the Earth turns
    beneath the satellite as it goes — about 29 degrees over one ISS
    revolution. The trace ends that far west of where it began, which is
    exactly what makes a ground track a westward-marching sinusoid.
    """
    gap = _gap_degrees(orbit_track(_iss_elset(), WHEN, ground_track=True).points)

    assert gap > 20.0


def test_an_orbit_path_closes() -> None:
    """Freezing GMST leaves the orbital ellipse itself, which is closed.

    Every sample is rotated by the same angle, so what arrives is the
    inertial ellipse rigidly positioned against the Earth as it is at
    that instant. Drawn at altitude on a globe, a drifting spiral would
    read as a bug rather than as physics.
    """
    gap = _gap_degrees(orbit_track(_iss_elset(), WHEN, ground_track=False).points)

    assert gap < 1.0


def test_an_orbit_path_includes_its_endpoint() -> None:
    """The duplicate vertex is what joins the curve back onto itself.

    A ground track excludes it, since the satellite is back at the same
    point in the orbit and the vertex would sit on top of the first.
    """
    closed = orbit_track(_iss_elset(), WHEN, samples=90, ground_track=False)
    ground = orbit_track(_iss_elset(), WHEN, samples=90, ground_track=True)

    assert len(closed.points) == len(ground.points) == 90
    # The closed path spans the full period; the ground track stops one
    # step short of it.
    assert _gap_degrees(closed.points) < _gap_degrees(ground.points)


def test_both_frames_agree_at_the_first_sample() -> None:
    """At zero elapsed time there is no rotation to differ over.

    If the two disagreed here, the frame switch would be doing something
    other than freezing the Earth.
    """
    ground = orbit_track(_iss_elset(), WHEN, ground_track=True).points[0]
    closed = orbit_track(_iss_elset(), WHEN, ground_track=False).points[0]

    assert closed == pytest.approx(ground, abs=1e-9)


def test_the_orbit_path_keeps_the_same_altitudes() -> None:
    """Only the rotation changes; the satellite's height does not.

    Altitude is measured from the ellipsoid and is independent of how
    far the Earth has spun, so the two frames must report it identically.
    """
    ground = [p[2] for p in orbit_track(_iss_elset(), WHEN, ground_track=True).points]
    closed = [p[2] for p in orbit_track(_iss_elset(), WHEN, ground_track=False).points]

    assert min(closed) == pytest.approx(min(ground), abs=1.0)
    assert max(closed) == pytest.approx(max(ground), abs=1.0)


def test_orbit_tracks_passes_the_frame_through() -> None:
    """The batch helper must not silently fall back to ground tracks."""
    closed = orbit_tracks([_iss_elset()], WHEN, ground_track=False)[0]

    assert _gap_degrees(closed.points) < 1.0
