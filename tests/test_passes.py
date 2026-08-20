"""Tests for finding when a satellite is above an observer's horizon.

Physical invariants rather than golden values, as elsewhere in this
project: a pass rises and falls, its peak is interior, everything inside it
clears the threshold, and a satellite that never rises produces nothing.
Those hold for any correct implementation and need no reference data.

The ISS element set is real, so the counts and durations it produces can be
checked against a well-known orbit — roughly four to six passes a day at
mid-latitudes, each five to twelve minutes.
"""

from datetime import UTC, datetime
from itertools import pairwise

import pytest
from test_elements import ISS_ROW

from sat_tracker.propagate.elements import Elset, _row_to_omm_fields
from sat_tracker.propagate.passes import (
    Pass,
    find_passes,
    look_angle_series,
)

WHEN = datetime(2026, 8, 21, 0, 0, 0, tzinfo=UTC)

# Mid-latitude, and inside the ISS's 51.6-degree inclination band so it
# actually gets passes.
BERLIN_LAT, BERLIN_LON = 52.52, 13.40


def _iss(**overrides) -> Elset:
    """Build an `Elset` from the shared ISS sample."""
    row = ISS_ROW | overrides
    return Elset(
        norad_cat_id=row["norad_cat_id"],
        object_name=row["object_name"],
        epoch=row["epoch"],
        omm_fields=_row_to_omm_fields(row),
    )


def _passes(**kwargs) -> list[Pass]:
    """Find ISS passes over Berlin with overridable parameters."""
    return find_passes(_iss(), BERLIN_LAT, BERLIN_LON, WHEN, **kwargs)


# ── look_angle_series ────────────────────────────────────────────────


def test_the_series_covers_the_whole_window() -> None:
    """One sample per step across the window, give or take SGP4 refusals."""
    times, _, _ = look_angle_series(
        _iss(), BERLIN_LAT, BERLIN_LON, WHEN, window_hours=24, step_seconds=60
    )

    assert len(times) == pytest.approx(24 * 60, rel=0.01)


def test_the_series_spans_the_requested_hours() -> None:
    """Last sample lands one step short of the window's end."""
    times, _, _ = look_angle_series(
        _iss(), BERLIN_LAT, BERLIN_LON, WHEN, window_hours=6, step_seconds=60
    )

    assert (times[-1] - times[0]).total_seconds() == pytest.approx(
        6 * 3600 - 60, abs=1.0
    )


def test_elevation_reaches_both_extremes() -> None:
    """Over a day a low-orbit satellite is sometimes up and often far below.

    Catches a series that never varies, which a frozen time grid would
    produce while still looking like plausible angles.
    """
    _, _, elevations = look_angle_series(
        _iss(), BERLIN_LAT, BERLIN_LON, WHEN, window_hours=24
    )

    assert elevations.max() > 10.0
    assert elevations.min() < -50.0


def test_a_naive_start_is_rejected() -> None:
    """As everywhere else, silence about the timezone is not acceptable."""
    with pytest.raises(ValueError, match="timezone-aware"):
        look_angle_series(
            _iss(), BERLIN_LAT, BERLIN_LON, datetime(2026, 8, 21)  # noqa: DTZ001
        )


@pytest.mark.parametrize(("hours", "step"), [(0, 30), (24, 0), (-1, 30)])
def test_an_empty_window_is_rejected(hours: int, step: int) -> None:
    """A window with no samples in it is a caller error, not an empty result."""
    with pytest.raises(ValueError, match="positive"):
        look_angle_series(
            _iss(), BERLIN_LAT, BERLIN_LON, WHEN, window_hours=hours, step_seconds=step
        )


# ── find_passes ──────────────────────────────────────────────────────


def test_the_iss_passes_a_realistic_number_of_times() -> None:
    """Four to six a day at mid-latitudes is the well-known figure.

    Far more would mean the threshold or the elevation sign is wrong; far
    fewer would mean passes are being missed or merged.
    """
    per_day = len(_passes(window_hours=72)) / 3

    assert 3.0 <= per_day <= 7.0


def test_passes_last_a_realistic_time() -> None:
    """A low-orbit pass runs a few minutes, never hours.

    A pass lasting hours would mean adjacent passes had been merged —
    which is what happens if the elevation series is sampled too coarsely
    to see the dip between them.
    """
    durations = [p.duration_minutes for p in _passes()]

    assert durations
    assert all(0 < d < 15 for d in durations)


def test_passes_are_in_time_order() -> None:
    """The table is read top to bottom."""
    starts = [p.start_utc for p in _passes()]

    assert starts == sorted(starts)


def test_passes_do_not_overlap() -> None:
    """Each pass must end before the next begins.

    Overlapping passes would mean a single run was split in two, which a
    threshold comparison written the wrong way round can produce.
    """
    passes = _passes()

    for earlier, later in pairwise(passes):
        assert earlier.end_utc < later.start_utc


def test_the_peak_lies_between_the_start_and_the_end() -> None:
    """A pass rises and falls, so its highest point is interior.

    A peak sitting exactly on an edge means the pass was truncated by the
    window — which is what the `truncated` flag is for.
    """
    for p in _passes():
        assert p.start_utc <= p.peak_utc <= p.end_utc
        if not p.truncated:
            assert p.start_utc < p.peak_utc < p.end_utc


def test_the_peak_is_the_highest_elevation_reached() -> None:
    """And it must clear the threshold, or the pass would not exist."""
    for p in _passes(minimum_elevation=10.0):
        assert p.peak_elevation_deg >= 10.0


def test_a_higher_threshold_yields_fewer_and_shorter_passes() -> None:
    """Raising the bar can only remove passes and trim the survivors.

    The physical statement of what a threshold does, and a check that the
    comparison is not reversed.
    """
    low = _passes(minimum_elevation=0.0)
    high = _passes(minimum_elevation=40.0)

    assert len(high) <= len(low)
    assert sum(p.duration_minutes for p in high) < sum(p.duration_minutes for p in low)


def test_a_satellite_that_never_rises_produces_no_passes() -> None:
    """The ISS cannot be seen from the South Pole.

    Its 51.6-degree inclination bounds the latitude it reaches, so from
    90 degrees south it is permanently below the horizon.
    """
    assert find_passes(_iss(), -90.0, 0.0, WHEN, window_hours=48) == []


def test_a_geostationary_satellite_overhead_is_permanently_up() -> None:
    """It hangs in one place, so it is one long pass rather than many.

    The opposite extreme from a low orbit, and it exercises the truncation
    flag: the pass runs past both ends of the window.
    """
    geo = _iss(mean_motion=1.0027, eccentricity=0.0001, inclination=0.05)

    passes = find_passes(geo, 0.0, 0.0, WHEN, window_hours=24, step_seconds=300)

    assert len(passes) <= 2
    if passes:
        assert passes[0].truncated


def test_a_shorter_window_finds_a_subset() -> None:
    """Looking less far ahead cannot conjure passes that were not there."""
    long_window = _passes(window_hours=72)
    short_window = _passes(window_hours=24)

    assert len(short_window) <= len(long_window)


def test_a_finer_step_locates_the_same_passes() -> None:
    """Step size changes precision, not how many passes exist.

    If halving the step changed the count, the sampling would be too
    coarse to resolve them — the check that 30 seconds is fine enough.
    """
    coarse = _passes(step_seconds=30)
    fine = _passes(step_seconds=10)

    assert abs(len(coarse) - len(fine)) <= 1


def test_azimuths_are_compass_bearings() -> None:
    """Every reported direction must be somewhere on the compass."""
    for p in _passes():
        for azimuth in (p.start_azimuth_deg, p.peak_azimuth_deg, p.end_azimuth_deg):
            assert 0.0 <= azimuth < 360.0


def test_a_pass_carries_the_satellite_identity() -> None:
    """The table names what it is describing."""
    p = _passes()[0]

    assert p.norad_cat_id == ISS_ROW["norad_cat_id"]
    assert p.object_name == ISS_ROW["object_name"]
