"""Streamlit map of where every tracked object is, right now.

Positions are propagated **on demand** rather than read from
`gold.position_snapshot`. That follows the project's stance that "real
time" here means computing positions when someone asks, not streaming
them from CelesTrak: a full propagation of ~16,000 objects costs about a
third of a second, which is cheaper than keeping a table current.

`gold.position_snapshot` still earns its place — it is the PostGIS
artifact an orchestrator writes and the thing spatial queries run
against. The map simply does not need to go through it.

── What is cached, and why ──────────────────────────────────────────
Element sets and the object dimension change only when the pipeline
runs, so both are cached for the session. The propagation is cached on
the instant it was computed for, which means changing a filter reuses
the existing positions while pressing Refresh computes new ones. Without
that split, every checkbox click would re-propagate the catalogue.

── Colour ───────────────────────────────────────────────────────────
Points are coloured by orbital regime — identity, not magnitude — using
the first three slots of the categorical palette in their dark-surface
steps, validated for all-pairs use (any two colours can land next to
each other on a map, unlike bars in a stack). `unknown` is not a fourth
hue: it folds to neutral, because a fourth categorical slot does not
clear the all-pairs separation floors.

Staleness is deliberately NOT encoded as colour. Colour follows the
entity, and overloading it with a second meaning would make a stale LEO
satellite indistinguishable from a fresh MEO one. It appears as a count
and an optional filter instead.
"""

import json
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg
import pydeck as pdk
import streamlit as st
import streamlit.components.v1 as components

from sat_tracker.config import settings
from sat_tracker.propagate import elements, frames
from sat_tracker.propagate import tracks as tracks_module
from sat_tracker.propagate.elements import Elset, Position
from sat_tracker.propagate.tracks import Track

# Categorical palette, dark-surface steps. Validated all-pairs: worst
# CVD separation dE 9.4 (deutan), worst normal-vision dE 20.9, all three
# above 3:1 contrast on the dark basemap.
REGIME_COLOURS: dict[str, list[int]] = {
    "LEO": [57, 135, 229],  # blue
    "MEO": [217, 89, 38],  # orange
    "GEO/HEO": [25, 158, 112],  # aqua
}

# Recessive grey for `unknown`. Not a categorical slot — see the module docstring.
NEUTRAL_COLOUR = [138, 138, 133]

# Light neutral for orbit tracks, deliberately not a palette hue.
# The categorical colours carry regime identity on the satellite dots.
# Reusing one for a line would imply the track belongs to that regime.
TRACK_COLOUR = [200, 200, 194, 170]

# Layer id, needed twice over: Streamlit keys selection results by it, and
# a declared id is what keeps a layer stateful across reruns
SATELLITE_LAYER_ID = "satellites"

# Opacity of objects that are not being traced, out of 255.
# Faded rather than hidden. A single orbit is only meaningful against the
# sixteen thousand objects it sits among — removing them would leave a line
# with nothing to read it against. Alpha carries focus while hue keeps
# carrying regime, so neither encoding has to compromise.
FADED_ALPHA = 40

# How many orbits may be traced at once.
# Not a performance limit so much as a legibility one: 25 tracks is ~2,250
# vertices and a few milliseconds, while the full catalogue would be 1.5
# million vertices that no browser draws interactively and that would read
# as noise rather than as orbits.
FULL_ALPHA = 255

MAX_TRACKS = 25

# Where the observer stands until told otherwise.
DEFAULT_OBSERVER = (52.52, 13.40)


# How old an element set may be before its position is worth flagging.
#
# These are per regime rather than flat because staleness means different
# things at different altitudes. Measured over the live catalogue: 0.5%
# of LEO objects carry elements older than 48 h, against 21% of MEO ones
# — not because MEO data is worse, but because negligible drag makes
# those orbits predictable and operators republish far less often. A flat
# 48 h threshold would flag a fifth of Galileo as untrustworthy.
STALENESS_THRESHOLD_HOURS: dict[str, float] = {
    "LEO": 48.0,
    "GEO/HEO": 96.0,
    "MEO": 168.0,
    "unknown": 48.0,
}

# Columns the map needs from `gold.dim_object`.

# `object_name` comes from here rather than from the propagation: it lives
# on `Elset` but is deliberately not carried onto `Position`, since a
# position is a measurement and a name is an attribute of the object.

DIMENSION_COLUMNS = (
    "norad_cat_id",
    "object_name",
    "object_type",
    "orbit_regime",
    "owner",
    "launch_date",
    "launch_site",
)

# SATCAT records launch sites as opaque codes, so the map expands the ones
# that actually appear. Covers every site with more than a handful of
# objects; anything unlisted falls back to its raw code rather than being
# blanked, since a code is more useful than nothing.
LAUNCH_SITES: dict[str, str] = {
    "AFETR": "Cape Canaveral",
    "AFWTR": "Vandenberg",
    "PLMSC": "Plesetsk",
    "TYMSC": "Baikonur",
    "TAISC": "Taiyuan",
    "FRGUI": "Kourou",
    "JSC": "Jiuquan",
    "SRILR": "Satish Dhawan",
    "XICLF": "Xichang",
    "TANSC": "Tanegashima",
    "VOSTO": "Vostochny",
    "WSC": "Wenchang",
    "RLLB": "Mahia, New Zealand",
    "KYMSC": "Kapustin Yar",
    "WLPIS": "Wallops Island",
    "KSCUT": "Uchinoura",
    "YSLA": "Yellow Sea launch platform",
    "DLS": "Dombarovsky",
    "SEAL": "Sea Launch platform",
    "ERAS": "Eastern Range air launch",
    "WRAS": "Western Range air launch",
    "HGSTR": "Hammaguir",
    "KODAK": "Kodiak",
    "NSC": "Naro",
    "SCSLA": "South China Sea platform",
    "YAVNE": "Palmachim",
    "SVOBO": "Svobodny",
    "SEMLS": "Semnan",
    "SNMLP": "San Marco platform",
    "SMTS": "Shahrud",
}


_DIMENSION_QUERY = f"SELECT {', '.join(DIMENSION_COLUMNS)} FROM gold.dim_object"

# Columns the globe actually draws or shows in a tooltip. Everything else
# — the TEME state vector, lineage, launch data — is dropped before
# rendering, because the globe embeds its data inline in the page rather
# than streaming it, so every unused column is dead weight in the DOM.
GLOBE_RENDER_COLUMNS = (
    "norad_cat_id",
    "object_name",
    "object_type",
    "orbit_regime",
    "latitude_deg",
    "longitude_deg",
    "altitude_km",
    "epoch_age_hours",
    "speed_km_s",
    "owner",
    "launch_date",
    "launch_site_name",
    "colour",
    "elevation_m",
)

# Decimal places kept per column when embedding. Three decimal places of
# latitude is about 100 m, which is far finer than a globe resolves and
# far coarser than a float's default repr, where a single coordinate can
# run to seventeen characters.
GLOBE_ROUNDING = {
    "latitude_deg": 3,
    "longitude_deg": 3,
    "altitude_km": 1,
    "epoch_age_hours": 1,
    "speed_km_s": 2,
}

GLOBE_HEIGHT_PX = 700

# Country outlines for the globe view. See `assets/README.md` for provenance.
LAND_GEOJSON_PATH = Path(__file__).parent / "assets" / "land.json"


# Land and ocean for the globe. Deliberately recessive: the satellites
# carry the palette's saturated hues, so the planet underneath them has
# to stay out of the way or the map becomes unreadable.
_GLOBE_LAND_COLOUR = [58, 58, 55]
_GLOBE_LAND_LINE_COLOUR = [92, 92, 87]
_GLOBE_OCEAN_COLOUR = [16, 20, 26]


# ── Data preparation (pure, and therefore testable) ──────────────────


def positions_to_frame(positions: list[Position]) -> pd.DataFrame:
    """Convert propagated positions into a dataframe.

    Args:
        positions: Output of `sat_tracker.propagate.elements.propagate`.

    Returns:
        One row per position, with `Position`'s fields as columns. Empty
        input yields an empty frame carrying the same columns, so
        downstream joins and filters do not need to special-case it.
    """
    if not positions:
        return pd.DataFrame(columns=[field for field in Position.__dataclass_fields__])
    return pd.DataFrame([asdict(position) for position in positions])


def add_speed(frame: pd.DataFrame) -> pd.DataFrame:
    """Attach orbital speed, from the velocity vector SGP4 already returned.

    The magnitude of the TEME velocity, so this is **inertial** speed —
    motion relative to the stars, not relative to the ground beneath.
    The two differ by up to Earth's surface rotation, about 0.46 km/s at
    the equator. Inertial is the honest number here because it is what
    the propagator computes; converting to ground-relative would mean
    subtracting the rotation vector, which is a different question.

    Expect roughly 7.7 km/s in low orbit and 3.1 km/s at geostationary
    altitude: orbital speed falls as the orbit widens.

    Args:
        frame: Positions carrying the three TEME velocity components.

    Returns:
        The frame with a `speed_km_s` column added.
    """
    if frame.empty:
        return frame.assign(speed_km_s=pd.Series(dtype=float))

    return frame.assign(
        speed_km_s=np.sqrt(
            frame["velocity_x_km_s"] ** 2
            + frame["velocity_y_km_s"] ** 2
            + frame["velocity_z_km_s"] ** 2
        )
    )


def name_launch_sites(frame: pd.DataFrame) -> pd.DataFrame:
    """Expand SATCAT's launch-site codes into readable place names.

    `AFETR` is Cape Canaveral and `TYMSC` is Baikonur, which no reader
    should be expected to know.

    Args:
        frame: Positions carrying `launch_site`.

    Returns:
        The frame with a `launch_site_name` column. An unrecognised code
        falls through to itself rather than becoming blank — a code is
        less useful than a name but far more useful than nothing.
    """
    if frame.empty:
        return frame.assign(launch_site_name=pd.Series(dtype=object))

    return frame.assign(
        launch_site_name=frame["launch_site"].map(
            lambda code: LAUNCH_SITES.get(code, code)
        )
    )


def attach_dimension(positions: pd.DataFrame, dimension: pd.DataFrame) -> pd.DataFrame:
    """Join object attributes onto propagated positions.

    `gold.fact_propagatable_elset` deliberately carries the element set
    only, so object type and orbital regime have to come from
    `gold.dim_object`. That separation is what stops a fact and its
    dimension drifting apart; the cost is this join.

    Args:
        positions: Frame from `positions_to_frame`.
        dimension: Rows from `gold.dim_object`.

    Returns:
        `positions` with the dimension's columns attached. A left join:
        every propagated satellite resolves in the dimension (the
        relationship test guarantees it), but a left join fails visibly
        with nulls rather than silently dropping rows if that ever
        stops being true.
    """
    return positions.merge(dimension, on="norad_cat_id", how="left")


def classify_staleness(frame: pd.DataFrame) -> pd.DataFrame:
    """Flag positions whose element set is old *for its orbital regime*.

    Args:
        frame: Positions with `orbit_regime` and `epoch_age_hours`.

    Returns:
        The frame with an added boolean `is_stale` column. Note
        `epoch_age_hours` is signed: a handful of deep-space objects
        publish epochs in the future, and those are early rather than
        stale, so the comparison is against the raw value and not its
        magnitude.
    """
    if frame.empty:
        return frame.assign(is_stale=pd.Series(dtype=bool))

    thresholds = frame["orbit_regime"].map(STALENESS_THRESHOLD_HOURS).fillna(48.0)
    return frame.assign(is_stale=frame["epoch_age_hours"] > thresholds)


def add_colours(frame: pd.DataFrame) -> pd.DataFrame:
    """Attach the per-regime RGB colour each point is drawn in.

    Args:
        frame: Positions carrying `orbit_regime`.

    Returns:
        The frame with a `colour` column of ``[r, g, b]`` lists, which is
        the form pydeck's ``get_fill_color`` expects.
    """
    if frame.empty:
        return frame.assign(colour=pd.Series(dtype=object))

    return frame.assign(
        colour=frame["orbit_regime"].map(
            lambda regime: REGIME_COLOURS.get(regime, NEUTRAL_COLOUR)
        )
    )


def add_look_angles(
    frame: pd.DataFrame,
    observer_latitude: float,
    observer_longitude: float,
    observer_altitude_km: float = 0.0,
) -> pd.DataFrame:
    """Attach where each satellite appears from a point on the ground.

    ── Why the satellite is converted back to ECEF ──────────────────
    `Position` carries geodetic latitude, longitude and altitude, not an
    Earth-fixed vector, and look angles need both bodies in the same
    Cartesian frame before they can be subtracted. `geodetic_to_ecef` is
    exact and closed-form, and the round trip through it recovers the
    position `ecef_to_geodetic` started from to well under a millimetre,
    so nothing is lost by going back.

    The alternative — rotating the stored TEME vector with
    `teme_to_ecef` — would need the propagation instant threaded down
    here as well, for a result identical to the millimetre.

    Args:
        frame: Positions carrying `latitude_deg`, `longitude_deg` and
            `altitude_km`.
        observer_latitude: Observer geodetic latitude in degrees.
        observer_longitude: Observer longitude in degrees.
        observer_altitude_km: Observer height above the ellipsoid.

    Returns:
        The frame with `azimuth_deg`, `elevation_deg` and `range_km`
        added. Elevation is the one that matters: positive means above
        the observer's horizon.
    """
    if frame.empty:
        return frame.assign(
            azimuth_deg=pd.Series(dtype=float),
            elevation_deg=pd.Series(dtype=float),
            range_km=pd.Series(dtype=float),
        )

    observer_ecef = frames.geodetic_to_ecef(
        observer_latitude, observer_longitude, observer_altitude_km
    )

    angles = [
        frames.ecef_to_look_angles(
            observer_ecef,
            frames.geodetic_to_ecef(latitude, longitude, altitude),
            observer_latitude,
            observer_longitude,
        )
        for latitude, longitude, altitude in zip(
            frame["latitude_deg"],
            frame["longitude_deg"],
            frame["altitude_km"],
            strict=True,
        )
    ]

    return frame.assign(
        azimuth_deg=[a[0] for a in angles],
        elevation_deg=[a[1] for a in angles],
        range_km=[a[2] for a in angles],
    )


def filter_visible(frame: pd.DataFrame, minimum_elevation: float) -> pd.DataFrame:
    """Keep only what is above the observer's horizon.

    ── What "visible" means here ────────────────────────────────────
    Geometric visibility: the satellite is above the local horizontal.
    NOT optical visibility, which additionally requires the satellite to
    be sunlit and the observer to be in darkness — that needs a solar
    ephemeris and Earth's shadow cone, and is a different piece of work.

    Args:
        frame: Positions carrying `elevation_deg`.
        minimum_elevation: Degrees above the horizon to require. Zero is
            the true geometric horizon; ten is a more honest floor for a
            real sky, where buildings, trees and haze eat the first few
            degrees.

    Returns:
        The filtered frame.
    """
    if frame.empty:
        return frame

    return frame[frame["elevation_deg"] >= minimum_elevation]


def apply_focus(frame: pd.DataFrame, traced_names: set[str]) -> pd.DataFrame:
    """Dim every object that is not being traced.

    Args:
        frame: Positions carrying a `colour` column of ``[r, g, b]``.
        traced_names: Object names whose orbits are drawn. Empty means
            nothing is focused, and every object stays fully opaque.

    Returns:
        The frame with `colour` widened to ``[r, g, b, a]``. Regime hue
        is preserved through the fade — a dimmed LEO satellite is still
        blue — because alpha carries focus and hue carries identity, and
        overloading either would cost the other.
    """
    if frame.empty:
        return frame

    if not traced_names:
        return frame.assign(
            colour=frame["colour"].map(lambda rgb: [*rgb[:3], FULL_ALPHA])
        )

    focused = frame["object_name"].isin(traced_names)
    return frame.assign(
        colour=[
            [*rgb[:3], FULL_ALPHA if is_focused else FADED_ALPHA]
            for rgb, is_focused in zip(frame["colour"], focused, strict=True)
        ]
    )


def selected_names(selection: object) -> set[str]:
    """Read object names out of a pydeck selection event.

    Streamlit hands back the underlying data rows for whatever was
    clicked, keyed by layer id. Anything unexpected in that structure is
    treated as "nothing selected" rather than raised: a click is not
    worth crashing the map over, and the shape is set by two libraries
    rather than by this project.

    Args:
        selection: The `selection` attribute of the value
            `st.pydeck_chart` returns.

    Returns:
        The object names that were clicked.
    """
    try:
        objects = selection["objects"][SATELLITE_LAYER_ID]  # type: ignore[index]
    except (KeyError, TypeError, IndexError):
        return set()

    return {
        row["object_name"]
        for row in objects
        if isinstance(row, dict) and row.get("object_name")
    }


def apply_filters(
    frame: pd.DataFrame,
    *,
    regimes: list[str],
    object_types: list[str],
    altitude_range: tuple[float, float],
    name_query: str = "",
    hide_stale: bool = False,
) -> pd.DataFrame:
    """Narrow the frame to what the sidebar currently selects.

    Args:
        frame: Positions with dimension attributes and `is_stale`.
        regimes: Orbital regimes to keep. Empty keeps none, which is
            what an empty multiselect means.
        object_types: SATCAT object types to keep (PAY, R/B, DEB, UNK).
        altitude_range: Inclusive ``(min_km, max_km)`` bounds.
        name_query: Case-insensitive substring of the object name. Empty
            matches everything.
        hide_stale: Drop objects flagged stale for their regime.

    Returns:
        The filtered frame.
    """
    if frame.empty:
        return frame

    mask = (
        frame["orbit_regime"].isin(regimes)
        & frame["object_type"].isin(object_types)
        & frame["altitude_km"].between(*altitude_range)
    )
    if name_query:
        mask &= frame["object_name"].str.contains(name_query, case=False, na=False)
    if hide_stale:
        mask &= ~frame["is_stale"]

    return frame[mask]


# ── Warehouse access (cached for the session) ────────────────────────


@st.cache_data(show_spinner="Loading element sets…")
def load_elsets() -> list[Elset]:
    """Read every propagatable element set. Changes only when the pipeline runs."""
    return elements.load_propagatable_elsets()


@st.cache_data(show_spinner="Loading object catalogue…")
def load_dimension() -> pd.DataFrame:
    """Read the object dimension. Changes only when the pipeline runs.

    Built from a cursor rather than `pd.read_sql`, which supports only
    SQLAlchemy connectables and warns on a raw psycopg connection. Adding
    SQLAlchemy purely to silence that would pull a substantial dependency
    the project has so far avoided.
    """
    with psycopg.connect(settings.postgres_dsn) as connection:
        rows = connection.execute(_DIMENSION_QUERY).fetchall()
    return pd.DataFrame(rows, columns=list(DIMENSION_COLUMNS))


@st.cache_data(show_spinner="Tracing orbits…")
def traced_orbits(
    object_names: tuple[str, ...], when: datetime, *, ground_track: bool
) -> list[Track]:
    """Trace one revolution for each named satellite.

    Args:
        object_names: Names as they appear in `dim_object`. A tuple
            rather than a list so Streamlit can hash it for the cache.
        when: The instant every revolution starts from. Passing it
            explicitly ties the cache to the current snapshot, so
            pressing Refresh re-traces rather than serving stale paths.
        ground_track: True traces the ground, False the closed orbit.
            Part of the cache key, so switching projection re-traces
            instead of reusing the other frame's geometry.

    Returns:
        One `Track` per satellite that resolved and propagated.
    """
    if not object_names:
        return []

    wanted = set(object_names)
    selected = [elset for elset in load_elsets() if elset.object_name in wanted]
    return tracks_module.orbit_tracks(selected, when, ground_track=ground_track)


def _selected_paths(
    object_names: list[str], *, globe: bool, exaggeration: float = 1.0
) -> list[dict]:
    """Trace the selected satellites and shape them for the path layer.

    Args:
        object_names: Names chosen in the sidebar.
        globe: Whether to build 3-D paths and skip dateline splitting.
            Also selects the frame: the globe draws the closed orbit,
            the flat map the ground track.

    Returns:
        Path records ready for a `PathLayer`, empty when nothing is
        selected.
    """
    if not object_names:
        return []

    return tracks_to_paths(
        traced_orbits(
            tuple(sorted(object_names)),
            st.session_state.snapshot_ts,
            ground_track=not globe,
        ),
        globe=globe,
        exaggeration=exaggeration,
    )


@st.cache_data(show_spinner="Propagating orbits…")
def snapshot_frame(when: datetime) -> pd.DataFrame:
    """Propagate every element set to `when` and prepare it for display.

    Cached on `when`, so adjusting a filter reuses the positions already
    computed while Refresh — which changes `when` — computes new ones.

    Args:
        when: Timezone-aware instant to propagate to.

    Returns:
        One row per satellite SGP4 produced a valid answer for, with
        dimension attributes, a staleness flag and a colour attached.
    """
    positions = elements.propagate(load_elsets(), when)
    frame = attach_dimension(positions_to_frame(positions), load_dimension())
    frame = name_launch_sites(add_speed(frame))
    return add_colours(classify_staleness(frame))


# ── The app ──────────────────────────────────────────────────────────


def _build_layer(frame: pd.DataFrame, *, globe: bool = False) -> pdk.Layer:
    """Build the scatterplot layer of satellite positions.

    Args:
        frame: The filtered frame to draw. On the globe it must carry an
            `elevation_m` column.
        globe: Place each satellite at its true altitude above the
            sphere, rather than flat on the surface.

    Returns:
        A pydeck `ScatterplotLayer`. Radius is set in pixels rather than
        metres so points stay legible when zoomed out — at true scale a
        satellite would be sub-pixel at every useful zoom level.
    """
    return pdk.Layer(
        "ScatterplotLayer",
        id=SATELLITE_LAYER_ID,
        data=frame,
        # On a globe the third component lifts each satellite off the
        # surface by its true altitude. A flat map has nowhere to put it,
        # and deck.gl would read a third value as a Mercator elevation
        # that means something quite different.
        get_position=(
            ["longitude_deg", "latitude_deg", "elevation_m"]
            if globe
            else ["longitude_deg", "latitude_deg"]
        ),
        get_fill_color="colour",
        get_radius=30000,
        radius_min_pixels=2,
        radius_max_pixels=6,
        pickable=True,
        # Opacity lives in the per-row alpha channel instead, so tracing
        # can dim individual satellites. A layer-wide value here would
        # multiply against it and mute the focused ones too.
        opacity=1.0,
    )


def split_at_antimeridian(
    points: list[tuple[float, float, float]],
) -> list[list[tuple[float, float, float]]]:
    """Break a path wherever it crosses the ±180° meridian.

    A satellite passing from 179°E to 179°W has moved two degrees, but
    the coordinates jump by 358. Drawn naively on a flat map that becomes
    a horizontal line straight across the world — the single most common
    bug in ground-track rendering.

    A jump larger than 180° cannot be real movement between adjacent
    samples, so it is taken as a wrap and the path is cut there.

    This is a flat-map concern only. A globe has no dateline; the path
    simply continues around, which is why the globe view skips it.

    Args:
        points: ``(latitude, longitude, altitude)`` tuples in time order.

    Returns:
        One or more segments. A path that never crosses comes back as a
        single segment, so callers need no special case.
    """
    if len(points) < 2:
        return [points] if points else []

    segments: list[list[tuple[float, float, float]]] = []
    current = [points[0]]

    for previous, point in pairwise(points):
        if abs(point[1] - previous[1]) > 180.0:
            segments.append(current)
            current = [point]
        else:
            current.append(point)

    segments.append(current)
    return [segment for segment in segments if len(segment) >= 2]


def unwrap_longitudes(
    points: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """Make a path's longitudes continuous across the ±180° meridian.

    The globe's counterpart to `split_at_antimeridian`, and the opposite
    treatment for the same underlying problem.

    `PathLayer` interpolates between consecutive vertices in
    longitude/latitude space *before* projecting onto the sphere. A step
    from 179° to −179° is two degrees of travel, but the renderer reads
    the raw difference of −358° and sweeps the long way round the
    planet, drawing a band at roughly constant latitude.

    Splitting would fix that but leave a gap in an orbit that is
    genuinely continuous. Adding a running multiple of 360° instead
    keeps every step small::

        raw         178.0   179.5   -179.0   -177.0
        unwrapped   178.0   179.5    181.0    183.0

    Longitudes outside ±180 are perfectly valid on a sphere — 181° *is*
    −179° — and the projection wraps them correctly. A high-inclination
    orbit crosses the antimeridian twice per revolution, so one closed
    path can legitimately run past ±540°.

    Args:
        points: ``(latitude, longitude, altitude)`` tuples in time order.

    Returns:
        The same points with longitudes made continuous.
    """
    if len(points) < 2:
        return points

    unwrapped = [points[0]]
    offset = 0.0

    for previous, current in pairwise(points):
        difference = current[1] - previous[1]
        if difference > 180.0:
            offset -= 360.0
        elif difference < -180.0:
            offset += 360.0
        unwrapped.append((current[0], current[1] + offset, current[2]))

    return unwrapped


def tracks_to_paths(
    tracks: list[Track], *, globe: bool, exaggeration: float = 1.0
) -> list[dict]:
    """Turn orbit tracks into the path records a `PathLayer` draws.

    Args:
        tracks: Tracks from `sat_tracker.propagate.tracks.orbit_tracks`.
        globe: Build 3-D paths carrying altitude, and unwrap longitudes
            instead of splitting them. The two projections need mirror
            treatments: a flat map draws a line across the canvas if the
            path is not cut, while a globe sweeps the long way round if
            the longitudes are not made continuous.
        exaggeration: Altitude multiplier, which must match the one
            applied to the satellite dots by `add_elevation`. If the two
            disagree, a satellite is drawn floating off the orbit it is
            actually flying — the dots move outward with the slider and
            the paths stay pinned at true altitude.

    Returns:
        One record per drawable segment, each with a `path` of
        ``[longitude, latitude]`` pairs — or ``[longitude, latitude,
        elevation_metres]`` triples on the globe, so a geostationary
        orbit visibly stands off from a low one. A flat map has nowhere
        to put that third dimension, and drawing every orbit at the same
        apparent height is one of the things a globe fixes.
    """
    records = []
    for track in tracks:
        segments = (
            [unwrap_longitudes(track.points)]
            if globe
            else split_at_antimeridian(track.points)
        )
        for segment in segments:
            path = [
                [longitude, latitude, altitude * 1000.0 * exaggeration]
                if globe
                else [longitude, latitude]
                for latitude, longitude, altitude in segment
            ]
            records.append(
                {
                    "path": path,
                    "norad_cat_id": track.norad_cat_id,
                    "object_name": track.object_name,
                    "period_minutes": round(track.period_minutes, 1),
                }
            )
    return records


@st.cache_data(show_spinner=False)
def load_land() -> dict:
    """Read the country outlines used to draw the globe.

    Returns:
        A GeoJSON ``FeatureCollection`` of country polygons, stripped of
        every property. Cached because it never changes.
    """
    return json.loads(LAND_GEOJSON_PATH.read_text())


def _globe_layers(frame: pd.DataFrame) -> list[pdk.Layer]:
    """Build the layers that make up the globe: ocean, land, satellites.

    A globe cannot use a raster basemap — map tiles are Mercator images
    and will not drape on a sphere — so the planet has to be drawn from
    vector geometry. The ocean is a `SolidPolygonLayer` covering the
    whole graticule, which reads as the sphere's surface once the globe
    projection wraps it.

    Args:
        frame: The filtered positions to draw.

    Returns:
        Layers in draw order: ocean underneath, land, then satellites.
    """
    ocean = pdk.Layer(
        "SolidPolygonLayer",
        data=[{"polygon": [[-180, -90], [180, -90], [180, 90], [-180, 90]]}],
        get_polygon="polygon",
        get_fill_color=_GLOBE_OCEAN_COLOUR,
        stroked=False,
    )
    land = pdk.Layer(
        "GeoJsonLayer",
        data=load_land(),
        get_fill_color=_GLOBE_LAND_COLOUR,
        get_line_color=_GLOBE_LAND_LINE_COLOUR,
        line_width_min_pixels=0.5,
        stroked=True,
        filled=True,
    )
    return [ocean, land, _build_layer(frame, globe=True)]


def _track_layer(paths: list[dict], *, globe: bool) -> pdk.Layer:
    """Build the layer that draws selected orbits as lines.

    Args:
        paths: Records from `tracks_to_paths`.
        globe: Whether the paths carry an elevation component.

    Returns:
        A pydeck `PathLayer`. Width is set in pixels so a track stays
        visible at every zoom, and the colour is a light neutral rather
        than a palette hue — the categorical colours belong to the
        satellite dots, and reusing one here would imply a regime the
        line does not have.
    """
    return pdk.Layer(
        "PathLayer",
        data=paths,
        get_path="path",
        get_color=TRACK_COLOUR,
        width_min_pixels=1.5,
        width_max_pixels=3,
        pickable=True,
        # deck.gl interpolates a flat path along great circles unless
        # told the vertices are already dense; these are, at ~1 sample
        # per minute, so straight segments are correct.
        billboard=globe,
    )


def _build_deck(
    frame: pd.DataFrame, *, globe: bool, paths: list[dict] | None = None
) -> pdk.Deck:
    """Assemble the deck for whichever projection is selected.

    The globe is the more honest projection for this data — satellites
    orbit a sphere, and Mercator badly exaggerates high latitudes. The
    polar Starlink shell reaches ±83°, which occupies a huge share of a
    Mercator canvas and a small cap on a globe. The latter is what is
    physically true.

    It is not the default, though: `_GlobeView` is experimental in
    deck.gl — hence the leading underscore — so the flat map stays the
    reliable option.

    Args:
        frame: The filtered positions to draw.
        globe: Render as a globe rather than a flat Mercator map.

    Returns:
        A configured `pdk.Deck`.
    """

    # round altitude and age to 3 and 2 decimal places (cm)
    frame = frame.assign(altitude_km=frame["altitude_km"].round(3),
                         epoch_age_hours=frame["epoch_age_hours"].round(2),
                         speed_km_s=frame["speed_km_s"].round(3))

    tooltip = {
        "html": "<b>{object_name}</b><br/>"
        "NORAD {norad_cat_id} · {object_type} · {orbit_regime} · {owner}<br/>"
        "{altitude_km} km · {speed_km_s} km/s<br/>"
        "launched {launch_date} from {launch_site_name}<br/>"
        "element set {epoch_age_hours} h old",
    }

    # Tracks go underneath the dots, so a satellite is never hidden by
    # its own orbit.
    track_layers = [_track_layer(paths, globe=globe)] if paths else []

    if not globe:
        return pdk.Deck(
            layers=[*track_layers, _build_layer(frame, globe=False)],
            initial_view_state=pdk.ViewState(latitude=20, longitude=0, zoom=1),
            map_style="dark",
            tooltip=tooltip,
        )

    ocean, land, satellites = _globe_layers(frame)
    return pdk.Deck(
        views=[pdk.View(type="_GlobeView", controller=True)],
        layers=[ocean, land, *track_layers, satellites],
        initial_view_state=pdk.ViewState(latitude=20, longitude=0, zoom=0),
        # No basemap provider: the sphere is drawn from the vector layers
        # above, and a tile provider here would fight with them.
        map_provider=None,
        map_style=None,
        tooltip=tooltip,
    )

# Mean Earth radius, used only to explain altitude as a fraction of it.
EARTH_RADIUS_KM = 6371.0



def add_elevation(frame: pd.DataFrame, exaggeration: float = 1.0) -> pd.DataFrame:
    """Attach the height, in metres, at which each satellite is drawn.

    deck.gl works in metres while the pipeline works in kilometres, so
    the conversion happens here, once, at the boundary.

    ── Why exaggeration is offered at all ───────────────────────────
    At true scale the orbital regimes differ by two orders of magnitude
    relative to Earth's 6,371 km radius:

    - the ISS at 420 km sits 6.6% of a radius up — a hair's breadth
    - GPS at 20,200 km sits 3.2 radii out
    - geostationary at 35,786 km sits 5.6 radii out

    So a truthful globe shows LEO clinging to the surface, because that
    is what LEO does. That is the right default, and it is also why the
    control exists: pushing LEO out far enough to separate visually is
    a deliberate distortion, and the caller has to ask for it rather
    than get it by accident.

    Args:
        frame: Positions carrying `altitude_km`.
        exaggeration: Multiplier on the drawn height. 1.0 is true scale.

    Returns:
        The frame with an `elevation_m` column added.
    """
    if frame.empty:
        return frame.assign(elevation_m=pd.Series(dtype=float))

    return frame.assign(elevation_m=frame["altitude_km"] * 1000.0 * exaggeration)


def trim_for_globe(frame: pd.DataFrame) -> pd.DataFrame:
    """Reduce a frame to what the globe needs, at the precision it needs.

    The globe is rendered by writing a self-contained page and embedding
    it in an iframe, so its data travels as literal JSON inside the HTML
    rather than over a socket. That makes payload size a real cost:
    unabridged, ~16,000 satellites with every column produce a 14 MB
    document, which the browser must parse on each rerun.

    Args:
        frame: The prepared, filtered positions.

    Returns:
        The same rows with only the drawn and tooltipped columns, and
        coordinates rounded to a precision the display cannot exceed.
    """
    if frame.empty:
        return frame

    trimmed = frame[list(GLOBE_RENDER_COLUMNS)].copy()
    for column, places in GLOBE_ROUNDING.items():
        trimmed[column] = trimmed[column].round(places)
    return trimmed


def globe_html(
    frame: pd.DataFrame, paths: list[dict], exaggeration: float = 1.0
) -> str:
    """Render the globe as a standalone page for embedding.

    ── Why this exists ──────────────────────────────────────────────
    Streamlit ships a trimmed deck.gl build with no `@deck.gl/globe`
    module. Passing a `_GlobeView` through `st.pydeck_chart` produces a
    correct JSON spec that the frontend cannot resolve, so it silently
    falls back to a flat `MapView` — the failure looks like the toggle
    doing nothing.

    pydeck's own `to_html` loads the full deck.gl bundle from a CDN,
    which does include the globe. So the globe is written out as a
    complete page and embedded in an iframe.

    Two costs come with that, both real:

    - **It needs the network.** The land outlines are local, but
      deck.gl itself is fetched from jsDelivr. Offline, the globe is a
      blank frame while the flat map keeps working.
    - **Clicks cannot come back.** An iframe has no channel into
      Python, so selection works only on the flat map. The name search
      remains available on both.

    Args:
        frame: Positions to draw.
        paths: Orbit path records from `tracks_to_paths`.

    Returns:
        A complete HTML document.
    """
    lifted = trim_for_globe(add_elevation(frame, exaggeration))
    return _build_deck(lifted, globe=True, paths=paths).to_html(
        as_string=True, offline=False
    )


def _render_table(frame: pd.DataFrame, count: int) -> None:
    """Draw the collapsible table beneath the map.

    Required rather than decorative: identity must never rest on colour
    alone, so a reader who cannot separate the regime hues has a
    non-visual route to the same data.

    Args:
        frame: The filtered positions.
        count: How many rows are shown, for the expander's label.
    """
    columns = [
        "norad_cat_id",
        "object_name",
        "object_type",
        "orbit_regime",
        "owner",
        "launch_date",
        "launch_site_name",
        "latitude_deg",
        "longitude_deg",
        "altitude_km",
        "speed_km_s",
        "epoch_age_hours",
        "is_stale",
    ]
    # Look angles only exist when an observer has been set, and they are
    # the first thing you want when they do — so they lead the table
    # rather than trailing twelve columns of context.
    if "elevation_deg" in frame.columns:
        columns = ["azimuth_deg", "elevation_deg", "range_km", *columns]

    with st.expander(f"Table view — {count:,} objects"):
        st.dataframe(
            frame[columns],
            use_container_width=True,
            hide_index=True,
        )


def _render_legend() -> None:
    """Draw the regime legend.

    Identity is never carried by colour alone, so the legend is always
    present rather than appearing only on hover.
    """
    swatches = [
        f'<span style="display:inline-flex;align-items:center;margin-right:1.25rem">'
        f'<span style="width:10px;height:10px;border-radius:50%;'
        f"background:rgb({','.join(str(c) for c in colour)});"
        f'margin-right:0.4rem"></span>{label}</span>'
        for label, colour in [*REGIME_COLOURS.items(), ("unknown", NEUTRAL_COLOUR)]
    ]
    st.markdown(
        f'<div style="font-size:0.85rem;opacity:0.85">{"".join(swatches)}</div>',
        unsafe_allow_html=True,
    )


def main() -> None:
    """Render the map."""
    st.set_page_config(page_title="sat-tracker", page_icon="🛰️", layout="wide")

    if "snapshot_ts" not in st.session_state:
        st.session_state.snapshot_ts = datetime.now(UTC)
    if "clicked_names" not in st.session_state:
        st.session_state.clicked_names = set()

    st.title("🛰️ Where everything is")

    frame = snapshot_frame(st.session_state.snapshot_ts)

    with st.sidebar:
        st.header("Filters")

        if st.button("Refresh positions", type="primary", use_container_width=True):
            st.session_state.snapshot_ts = datetime.now(UTC)
            st.rerun()

        st.caption(
            f"Propagated to {st.session_state.snapshot_ts:%Y-%m-%d %H:%M:%S} UTC"
        )

        projection = st.radio(
            "Projection",
            options=["Flat map", "Globe"],
            horizontal=True,
            help="A globe is the truer projection for orbital data — Mercator "
            "stretches high latitudes, so polar orbits look far denser than "
            "they are. The globe view is experimental in deck.gl.",
        )

        exaggeration = 1.0
        if projection == "Globe":
            exaggeration = st.select_slider(
                "Altitude scale",
                options=[1, 2, 5, 10, 20],
                value=1,
                format_func=lambda x: "true scale" if x == 1 else f"{x}x",
                help="At true scale the ISS sits 6.6% of an Earth radius up, so "
                "LEO really does hug the surface, while geostationary sits 5.6 "
                "radii out. Exaggerating separates the low orbits, at the cost "
                "of no longer showing the real geometry.",
            )
        st.divider()

        regimes = st.multiselect(
            "Orbital regime",
            options=sorted(frame["orbit_regime"].dropna().unique()),
            default=sorted(frame["orbit_regime"].dropna().unique()),
        )
        object_types = st.multiselect(
            "Object type",
            options=sorted(frame["object_type"].dropna().unique()),
            default=sorted(frame["object_type"].dropna().unique()),
            help="PAY payload · R/B rocket body · DEB debris · UNK unknown",
        )

        max_altitude = float(frame["altitude_km"].max())
        altitude_range = st.slider(
            "Altitude (km)",
            min_value=0.0,
            max_value=max_altitude,
            value=(0.0, max_altitude),
            step=100.0,
        )
        name_query = st.text_input("Name contains", placeholder="e.g. STARLINK")
        hide_stale = st.checkbox(
            "Hide stale element sets",
            help="Stale is judged per regime: 48 h for LEO, 96 h for GEO/HEO, "
            "168 h for MEO. High orbits are predictable, so operators "
            "republish far less often — a flat threshold would wrongly flag them.",
        )

        st.divider()
        st.subheader("Visible from")

        observe = st.checkbox(
            "Only what is above the horizon",
            help="Geometric visibility — the satellite is above the local "
            "horizontal. Not optical visibility, which also needs the "
            "satellite sunlit and the observer in darkness.",
        )
        observer_lat = st.number_input(
            "Latitude", value=DEFAULT_OBSERVER[0], min_value=-90.0,
            max_value=90.0, step=0.01, format="%.4f",
        )
        observer_lon = st.number_input(
            "Longitude", value=DEFAULT_OBSERVER[1], min_value=-180.0,
            max_value=180.0, step=0.01, format="%.4f",
        )
        minimum_elevation = st.slider(
            "Minimum elevation", 0, 60, 10, step=5, format="%d°",
            help="0° is the true geometric horizon. 10° is a more honest "
            "floor for a real sky, where buildings and haze eat the first "
            "few degrees.",
        )

    visible = apply_filters(
        frame,
        regimes=regimes,
        object_types=object_types,
        altitude_range=altitude_range,
        name_query=name_query,
        hide_stale=hide_stale,
    )

    if observe:
        visible = filter_visible(
            add_look_angles(visible, observer_lat, observer_lon), minimum_elevation
        )

    tracked, stale = len(visible), int(visible["is_stale"].sum()) if len(visible) else 0
    left, middle, right = st.columns(3)
    left.metric("Objects shown", f"{tracked:,}")
    middle.metric("Of the catalogue", f"{len(frame):,}")
    if observe:
        right.metric(f"Above {minimum_elevation}° from here", f"{tracked:,}")
    else:
        right.metric("Stale for their regime", f"{stale:,}")

    _render_legend()

    if visible.empty:
        st.warning("No objects match these filters.")
        return

    with st.sidebar:
        st.divider()
        st.subheader("Orbit tracks")
        if projection == "Globe":
            st.caption(
                "Search by name to trace an orbit. Clicking works on the flat "
                "map only — the globe is an embedded page and cannot report "
                "clicks back."
            )
        else:
            st.caption("Click a satellite on the map, or search for one by name.")

        searched = st.multiselect(
            "Trace the orbit of",
            options=sorted(visible["object_name"].dropna().unique()),
            max_selections=MAX_TRACKS,
            help="Draws one full revolution, sampled over each satellite's own "
            "period. The flat map shows the ground track, which drifts west "
            "each orbit as the Earth turns beneath it. The globe shows the "
            "closed orbit itself, at true altitude.",
        )

        clicked = st.session_state.clicked_names
        traced_names = sorted(set(searched) | clicked)[:MAX_TRACKS]

        if clicked:
            st.caption(f"{len(clicked)} selected by clicking.")
            if st.button("Clear clicked", use_container_width=True):
                st.session_state.clicked_names = set()
                st.rerun()

    globe = projection == "Globe"
    paths = _selected_paths(traced_names, globe=globe, exaggeration=exaggeration)
    focused = apply_focus(visible, set(traced_names))

    if globe:
        # Rendered as an embedded page rather than through st.pydeck_chart,
        # which cannot draw a globe — see `globe_html` for why.
        components.html(
            globe_html(focused, paths, exaggeration), height=GLOBE_HEIGHT_PX
        )
        st.caption(
            "Closed orbits at true altitude. The globe is an embedded deck.gl "
            "page, so it needs a network connection."
        )
        _render_table(visible, tracked)
        return

    event = st.pydeck_chart(
        _build_deck(focused, globe=globe, paths=paths),
        on_select="rerun",
        selection_mode="multi-object",
        key="satellite_map",
    )

    # A click reruns the script, so the newly selected names are merged
    # into session state and the rerun triggered here draws their tracks.
    newly_clicked = selected_names(getattr(event, "selection", None))
    if newly_clicked and not newly_clicked <= st.session_state.clicked_names:
        st.session_state.clicked_names |= newly_clicked
        st.rerun()

    _render_table(visible, tracked)


def run() -> None:
    """Entry point for `sat-tracker-map`: launch Streamlit against this module.

    Streamlit runs a script rather than importing a callable, so the
    console command shells out the same way `sat-tracker-transform`
    shells out to dbt.
    """
    result = subprocess.run(
        ["streamlit", "run", str(Path(__file__).resolve()), *sys.argv[1:]],
        check=False,
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
