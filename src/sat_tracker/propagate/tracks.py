"""Trace a satellite's path over one full revolution.

`elements.propagate` answers "where is everything now". This module
answers "where does one object go" — sampling a single satellite across
its own orbital period so the result can be drawn as a line rather than
a dot.

── Why one satellite at a time ──────────────────────────────────────
`SatrecArray.sgp4` propagates many satellites at one instant, or many
satellites across a shared list of instants. It cannot give each
satellite its own time grid, and each satellite needs exactly that: a
LEO object completes a revolution in about 90 minutes and a
geostationary one takes about 1,436, so a shared window would either
truncate the slow orbits or oversample the fast ones into millions of
redundant points.

`Satrec.sgp4_array` is the right primitive instead — one satellite,
many times, still a single call into C. At the scale this is used for
(a couple of dozen selected objects) the per-satellite loop is
immaterial: 25 tracks of 90 samples is 2,250 propagations, a few
milliseconds.

That scale is deliberate. Tracing the whole catalogue would be 1.5
million points, which no browser will draw interactively and which
would render as noise rather than information. Tracks are for the
handful of objects someone has picked out.

── Where the period comes from ──────────────────────────────────────
`mean_motion` is revolutions per day, so the period is
``1440 / mean_motion`` minutes. That is taken from the element set
rather than from `dim_object.period_minutes`: SATCAT's value is a
rounded catalogue summary, while the element set's is the number SGP4
is actually propagating with.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
from sgp4.api import Satrec, jday
from sgp4.omm import initialize

from sat_tracker.propagate.elements import Elset
from sat_tracker.propagate.frames import teme_to_geodetic

logger = logging.getLogger(__name__)

MINUTES_PER_DAY = 1440.0

DEFAULT_SAMPLES = 90
"""Points per revolution. Enough that a LEO ground track reads as a smooth
curve — roughly one sample per minute — without inflating the vertex count."""


@dataclass(frozen=True)
class Track:
    """One satellite's path over a single revolution.

    Attributes:
        norad_cat_id: NORAD catalog number.
        object_name: Human-readable name, for labelling.
        period_minutes: The revolution this track covers, derived from
            the element set's mean motion.
        points: ``(latitude_deg, longitude_deg, altitude_km)`` in time
            order. May be shorter than the requested sample count if
            SGP4 declined some instants.
    """

    norad_cat_id: int
    object_name: str | None
    period_minutes: float
    points: list[tuple[float, float, float]]


def period_minutes(elset: Elset) -> float:
    """Derive the orbital period from an element set's mean motion.

    Args:
        elset: The element set to read `MEAN_MOTION` from.

    Returns:
        The period in minutes.

    Raises:
        ValueError: If mean motion is zero or negative, which would make
            the period undefined or nonsensical. SGP4 would reject such
            an element set anyway, but failing here names the reason.
    """
    mean_motion = float(elset.omm_fields["MEAN_MOTION"])
    if mean_motion <= 0:
        raise ValueError(
            f"Satellite {elset.norad_cat_id} has mean motion {mean_motion}; "
            "a period cannot be derived from it."
        )
    return MINUTES_PER_DAY / mean_motion


def orbit_track(
    elset: Elset,
    when: datetime,
    samples: int = DEFAULT_SAMPLES,
    *,
    ground_track: bool = True,
) -> Track:
    """Trace one satellite across one revolution, starting at `when`.

    ── Ground track or orbit path ───────────────────────────────────
    These are the same motion in two frames, and only one of them
    closes.

    A **ground track** converts every sample using its own GMST, so the
    Earth turns underneath the satellite as it goes. Over one 93-minute
    ISS revolution the planet rotates about 29 degrees, and the trace
    ends that far west of where it began. It never closes, and that
    westward march is exactly what a ground track is: the line of places
    the satellite passed over.

    An **orbit path** freezes GMST at `when`, so every sample is rotated
    by the same angle. What arrives is the orbital ellipse itself,
    rigidly positioned against the Earth as it is at that instant — a
    closed curve. It is the right thing to draw at altitude on a globe,
    where a drifting spiral would look like a bug rather than like
    physics.

    Args:
        elset: The element set to propagate.
        when: Timezone-aware instant the revolution starts from.
        samples: Points to compute across the period.
        ground_track: Convert each sample at its own instant, giving the
            trace over the ground. Set False for the closed orbit, which
            also includes the endpoint so the curve joins back onto
            itself rather than stopping one step short.

    Returns:
        A `Track`. Instants SGP4 declined are omitted rather than
        written as meaningless coordinates, so the point list may be
        shorter than `samples`.

    Raises:
        ValueError: If `when` is timezone-naive, or `samples` is below 2,
            or the element set has no usable mean motion.
    """
    if when.tzinfo is None:
        raise ValueError("`when` must be timezone-aware.")
    if samples < 2:
        raise ValueError("A track needs at least 2 points to be a line.")

    when = when.astimezone(UTC)
    period = period_minutes(elset)

    satrec = Satrec()
    initialize(satrec, elset.omm_fields)

    julian_day, julian_fraction = jday(
        when.year,
        when.month,
        when.day,
        when.hour,
        when.minute,
        when.second + when.microsecond / 1_000_000,
    )

    # Offsets in days across one period. Added to the fraction rather
    # than the day so the whole-number part stays exact.
    #
    # A ground track excludes the endpoint: after a full period the
    # satellite is back at its starting point in the orbit, and drawing
    # it would add a vertex on top of the first. A closed orbit path
    # wants precisely that duplicate, because it is what joins the curve.
    offsets = np.linspace(
        0.0,
        period / MINUTES_PER_DAY,
        samples,
        endpoint=not ground_track,
    )
    fractions = julian_fraction + offsets
    days = np.full(samples, julian_day, dtype=float)

    errors, positions, _ = satrec.sgp4_array(days, fractions)

    start_jd = julian_day + julian_fraction

    points: list[tuple[float, float, float]] = []
    for error, position, offset in zip(errors, positions, offsets, strict=True):
        if error != 0:
            continue
        # The whole difference between the two frames is which Julian
        # date the rotation is taken at.
        jd = start_jd + float(offset) if ground_track else start_jd
        latitude, longitude, altitude = teme_to_geodetic(
            tuple(float(component) for component in position), jd
        )
        points.append((float(latitude), float(longitude), float(altitude)))

    declined = samples - len(points)
    if declined:
        logger.warning(
            "SGP4 declined %d of %d instants for satellite %d",
            declined,
            samples,
            elset.norad_cat_id,
        )

    return Track(
        norad_cat_id=elset.norad_cat_id,
        object_name=elset.object_name,
        period_minutes=period,
        points=points,
    )


def orbit_tracks(
    elsets: list[Elset],
    when: datetime,
    samples: int = DEFAULT_SAMPLES,
    *,
    ground_track: bool = True,
) -> list[Track]:
    """Trace several satellites, each across its own revolution.

    Args:
        elsets: Element sets to trace.
        when: Timezone-aware instant every revolution starts from.
        samples: Points per track.
        ground_track: See `orbit_track`. True traces the ground; False
            traces the closed orbit.

    Returns:
        One `Track` per element set that produced at least two points.
        A satellite SGP4 refused outright is dropped rather than
        returned as a degenerate line.
    """
    tracks = []
    for elset in elsets:
        try:
            track = orbit_track(elset, when, samples, ground_track=ground_track)
        except ValueError:
            logger.warning(
                "Skipping satellite %d: no usable period", elset.norad_cat_id
            )
            continue
        if len(track.points) >= 2:
            tracks.append(track)

    logger.info("Traced %d of %d requested orbits", len(tracks), len(elsets))
    return tracks
