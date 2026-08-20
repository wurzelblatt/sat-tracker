"""When a satellite is above a given observer's horizon.

`elements` answers "where is everything now". `tracks` answers "where does
one object go". This answers "when can I see it from here" — which is a
search rather than a transformation.

── The method ───────────────────────────────────────────────────────
Sample the satellite's elevation across the window, then find the
contiguous runs where it exceeds a threshold. Each run is a pass, and its
first, last and highest samples give the numbers an observer wants.

That is deliberately not an analytic solution. Root-finding on elevation
would be more elegant and considerably more fragile: SGP4 has no closed
form to differentiate, passes come in bunches, and a solver that lands on
the wrong root produces a plausible time for a pass that never happens.
Dense sampling cannot be subtly wrong, only coarse.

── Why sampling is affordable ───────────────────────────────────────
`Satrec.sgp4_array` propagates one satellite across many instants in a
single call into C. Three days at 30-second steps is 8,640 samples and a
few milliseconds; the frame conversion afterwards is the slower half, and
still well under a second.

── Step size is the only real trade ─────────────────────────────────
It bounds how precisely a pass edge can be located, since the true
crossing lies somewhere inside the last step. Thirty seconds gives 10-24
samples across a typical 5-12 minute low-orbit pass — enough to find the
peak, coarse at the edges. Ten sharpens both and costs three times as
much, which is still nothing.

Geostationary objects need far fewer samples than that, since they barely
move relative to the ground; low orbits need the most.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sgp4.api import Satrec, jday
from sgp4.omm import initialize

from sat_tracker.propagate.elements import Elset
from sat_tracker.propagate.frames import (
    ecef_to_look_angles,
    geodetic_to_ecef,
    gmst_radians,
    teme_to_ecef,
)

logger = logging.getLogger(__name__)

DEFAULT_STEP_SECONDS = 30
"""Sampling interval. See the module docstring for what it costs and buys."""

DEFAULT_WINDOW_HOURS = 72
"""How far ahead to look. Three days is about the limit worth predicting.

SGP4 drifts 1-3 km per day from epoch, and with element sets typically a
day old already, the far end of this window carries several kilometres of
along-track error — roughly a second of timing. Useful; not to be quoted
to the second.
"""

DEFAULT_MINIMUM_ELEVATION = 10.0
"""Degrees above the horizon to count as a pass.

Zero is the true geometric horizon. Ten is the more honest floor for a
real sky, where buildings, trees and haze eat the first few degrees.
"""

SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class Pass:
    """One appearance of a satellite above an observer's horizon.

    Attributes:
        norad_cat_id: NORAD catalog number.
        object_name: Human-readable name, for labelling.
        start_utc: When the satellite first exceeded the threshold.
        start_azimuth_deg: Bearing at that moment — where to look first.
        peak_utc: When it was highest.
        peak_azimuth_deg: Bearing at the highest point.
        peak_elevation_deg: The highest elevation reached. This is what
            decides whether a pass is worth watching: a 15-degree pass
            skims the horizon, an 80-degree one goes nearly overhead.
        end_utc: When it dropped back below the threshold.
        end_azimuth_deg: Bearing as it set.
        duration_minutes: How long it was up.
        truncated: True when the pass was still in progress at one end of
            the search window, so its real start or end lies outside and
            the reported time is the window's edge rather than the
            crossing.
    """

    norad_cat_id: int
    object_name: str | None
    start_utc: datetime
    start_azimuth_deg: float
    peak_utc: datetime
    peak_azimuth_deg: float
    peak_elevation_deg: float
    end_utc: datetime
    end_azimuth_deg: float
    duration_minutes: float
    truncated: bool


def look_angle_series(
    elset: Elset,
    observer_latitude: float,
    observer_longitude: float,
    start: datetime,
    *,
    observer_altitude_km: float = 0.0,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    step_seconds: int = DEFAULT_STEP_SECONDS,
) -> tuple[list[datetime], np.ndarray, np.ndarray]:
    """Compute look angles for one satellite across a window.

    Args:
        elset: The element set to propagate.
        observer_latitude: Observer geodetic latitude in degrees.
        observer_longitude: Observer longitude in degrees.
        start: Timezone-aware instant the window begins.
        observer_altitude_km: Observer height above the ellipsoid.
        window_hours: How far ahead to look.
        step_seconds: Sampling interval.

    Returns:
        ``(times, azimuths, elevations)``, one entry per sample. Instants
        SGP4 declined are omitted, so the series may be shorter than the
        window implies — and may have gaps, which the pass search treats
        as breaks rather than pretending continuity across them.

    Raises:
        ValueError: If `start` is timezone-naive, or the sampling
            parameters describe an empty window.
    """
    if start.tzinfo is None:
        raise ValueError("`start` must be timezone-aware.")
    if window_hours <= 0 or step_seconds <= 0:
        raise ValueError("`window_hours` and `step_seconds` must both be positive.")

    start = start.astimezone(UTC)
    samples = int(window_hours * 3600 / step_seconds)

    satrec = Satrec()
    initialize(satrec, elset.omm_fields)

    julian_day, julian_fraction = jday(
        start.year,
        start.month,
        start.day,
        start.hour,
        start.minute,
        start.second + start.microsecond / 1_000_000,
    )

    # Offsets in days. Added to the fraction rather than the day so the
    # whole-number part stays exact across a three-day window.
    offsets = np.arange(samples) * (step_seconds / SECONDS_PER_DAY)
    errors, positions, _ = satrec.sgp4_array(
        np.full(samples, julian_day, dtype=float), julian_fraction + offsets
    )

    observer_ecef = geodetic_to_ecef(
        observer_latitude, observer_longitude, observer_altitude_km
    )
    start_jd = julian_day + julian_fraction

    times: list[datetime] = []
    azimuths: list[float] = []
    elevations: list[float] = []

    for error, position, offset in zip(errors, positions, offsets, strict=True):
        if error != 0:
            continue
        # Each sample is rotated at its own instant: the Earth turns under
        # the satellite as the window advances, which is precisely what
        # makes a pass begin and end.
        jd = start_jd + float(offset)
        satellite_ecef = teme_to_ecef(
            tuple(float(component) for component in position), gmst_radians(jd)
        )
        azimuth, elevation, _ = ecef_to_look_angles(
            observer_ecef, satellite_ecef, observer_latitude, observer_longitude
        )
        times.append(start + timedelta(days=float(offset)))
        azimuths.append(azimuth)
        elevations.append(elevation)

    declined = samples - len(times)
    if declined:
        logger.warning(
            "SGP4 declined %d of %d instants for satellite %d",
            declined,
            samples,
            elset.norad_cat_id,
        )

    return times, np.array(azimuths), np.array(elevations)


def find_passes(
    elset: Elset,
    observer_latitude: float,
    observer_longitude: float,
    start: datetime,
    *,
    observer_altitude_km: float = 0.0,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    step_seconds: int = DEFAULT_STEP_SECONDS,
    minimum_elevation: float = DEFAULT_MINIMUM_ELEVATION,
) -> list[Pass]:
    """Find every pass of one satellite over an observer within a window.

    Args:
        elset: The element set to propagate.
        observer_latitude: Observer geodetic latitude in degrees.
        observer_longitude: Observer longitude in degrees.
        start: Timezone-aware instant the window begins.
        observer_altitude_km: Observer height above the ellipsoid.
        window_hours: How far ahead to look.
        step_seconds: Sampling interval.
        minimum_elevation: Degrees above the horizon that count.

    Returns:
        One `Pass` per contiguous run above the threshold, in time order.
        A pass needs at least two samples: a single sample above the
        threshold locates nothing, since both its edges are unknown.
    """
    times, azimuths, elevations = look_angle_series(
        elset,
        observer_latitude,
        observer_longitude,
        start,
        observer_altitude_km=observer_altitude_km,
        window_hours=window_hours,
        step_seconds=step_seconds,
    )

    if len(times) < 2:
        return []

    above = elevations >= minimum_elevation
    passes: list[Pass] = []

    # Walk the boolean series and cut it into runs of True.
    run_start: int | None = None
    for index, is_above in enumerate(above):
        if is_above and run_start is None:
            run_start = index
        elif not is_above and run_start is not None:
            passes.append(
                _build_pass(elset, times, azimuths, elevations, run_start, index - 1)
            )
            run_start = None

    # A run still open at the end of the series was cut off by the window.
    if run_start is not None:
        passes.append(
            _build_pass(elset, times, azimuths, elevations, run_start, len(above) - 1)
        )

    # A run that began at the very first sample was already in progress
    # when the window opened, so its true start lies before it.
    passes = [p for p in passes if p.duration_minutes > 0]

    logger.info(
        "Found %d passes of satellite %d over %.1f hours",
        len(passes),
        elset.norad_cat_id,
        window_hours,
    )
    return passes


def _build_pass(
    elset: Elset,
    times: list[datetime],
    azimuths: np.ndarray,
    elevations: np.ndarray,
    first: int,
    last: int,
) -> Pass:
    """Summarise one contiguous run of samples above the threshold.

    Args:
        elset: The satellite the run belongs to.
        times: Sample instants.
        azimuths: Sample azimuths in degrees.
        elevations: Sample elevations in degrees.
        first: Index of the first sample above the threshold.
        last: Index of the last.

    Returns:
        The corresponding `Pass`.
    """
    peak = first + int(np.argmax(elevations[first : last + 1]))

    return Pass(
        norad_cat_id=elset.norad_cat_id,
        object_name=elset.object_name,
        start_utc=times[first],
        start_azimuth_deg=float(azimuths[first]),
        peak_utc=times[peak],
        peak_azimuth_deg=float(azimuths[peak]),
        peak_elevation_deg=float(elevations[peak]),
        end_utc=times[last],
        end_azimuth_deg=float(azimuths[last]),
        duration_minutes=(times[last] - times[first]).total_seconds() / 60.0,
        # Touching either end of the series means the crossing happened
        # outside the window, so the reported time is the window edge.
        truncated=first == 0 or last == len(times) - 1,
    )
