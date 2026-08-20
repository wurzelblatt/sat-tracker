"""Tests for the map's data preparation.

Only the pure functions are tested — the join, the staleness rule, the
colour assignment and the filters. Streamlit's rendering is not: it has
its own tests, and asserting on widget output would test the framework
rather than this project's logic.

None of these need a database or a browser.
"""

import json
from datetime import UTC, datetime
from itertools import pairwise

import pandas as pd
import psycopg
import pytest

from sat_tracker.app import (
    _DIMENSION_QUERY,
    DIMENSION_COLUMNS,
    FADED_ALPHA,
    FULL_ALPHA,
    GLOBE_RENDER_COLUMNS,
    NEUTRAL_COLOUR,
    REGIME_COLOURS,
    SATELLITE_LAYER_ID,
    STALENESS_THRESHOLD_HOURS,
    _build_deck,
    add_colours,
    add_elevation,
    add_look_angles,
    add_speed,
    apply_filters,
    apply_focus,
    attach_dimension,
    classify_staleness,
    filter_visible,
    globe_html,
    load_land,
    name_launch_sites,
    positions_to_frame,
    selected_names,
    split_at_antimeridian,
    tracks_to_paths,
    trim_for_globe,
    unwrap_longitudes,
)
from sat_tracker.config import settings
from sat_tracker.propagate.elements import Position
from sat_tracker.propagate.tracks import Track

SNAPSHOT_TS = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
EPOCH = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)


def _position(norad_cat_id: int, *, epoch_age_hours: float = 6.0) -> Position:
    """Build a position with a controllable element-set age."""
    return Position(
        norad_cat_id=norad_cat_id,
        snapshot_ts=SNAPSHOT_TS,
        epoch=EPOCH,
        epoch_age_hours=epoch_age_hours,
        latitude_deg=51.5,
        longitude_deg=-0.13,
        altitude_km=420.0,
        position_x_km=4000.0,
        position_y_km=3000.0,
        position_z_km=4000.0,
        velocity_x_km_s=-1.5,
        velocity_y_km_s=6.9,
        velocity_z_km_s=3.2,
    )


def _dimension(**overrides) -> pd.DataFrame:
    """Build a one-row object dimension.

    Keyed off `DIMENSION_COLUMNS` so the fixture cannot drift from what
    the real query selects — which it did once, hiding a missing
    `object_name` until the app was run against live data.
    """
    row = dict.fromkeys(DIMENSION_COLUMNS)
    row |= {"norad_cat_id": 1, "object_name": "TEST-1", "object_type": "PAY",
            "orbit_regime": "LEO", "owner": "US"}
    return pd.DataFrame([row | overrides])


def _frame(**overrides) -> pd.DataFrame:
    """Build a fully prepared one-row frame, ready for filtering.

    Runs the same steps in the same order as `snapshot_frame`, so a
    column added to the real pipeline cannot go missing here. Keeping
    the two in step by hand is what let a fixture claim `object_name`
    the production query did not select, and 25 passing tests coexist
    with a broken app.
    """
    positions = positions_to_frame([_position(1, **{
        k: v for k, v in overrides.items() if k == "epoch_age_hours"
    })])
    dimension = _dimension(
        **{k: v for k, v in overrides.items() if k != "epoch_age_hours"}
    )
    frame = attach_dimension(positions, dimension)
    frame = name_launch_sites(add_speed(frame))
    return add_colours(classify_staleness(frame))


# ── positions_to_frame ───────────────────────────────────────────────


def test_positions_become_rows() -> None:
    """Each Position becomes one row, with its fields as columns."""
    frame = positions_to_frame([_position(1), _position(2)])

    assert len(frame) == 2
    assert frame["norad_cat_id"].tolist() == [1, 2]
    assert "altitude_km" in frame.columns


def test_empty_positions_still_carry_the_columns() -> None:
    """An empty propagation must not produce a frame the joins cannot use.

    A bare `DataFrame()` has no columns, so the merge downstream would
    raise a KeyError rather than yielding an empty result.
    """
    frame = positions_to_frame([])

    assert frame.empty
    assert "norad_cat_id" in frame.columns
    assert "epoch_age_hours" in frame.columns


# ── attach_dimension ─────────────────────────────────────────────────


def test_dimension_attributes_are_joined_on() -> None:
    """Object type and regime come from dim_object, not from the fact."""
    joined = attach_dimension(positions_to_frame([_position(1)]), _dimension())

    assert joined.loc[0, "object_type"] == "PAY"
    assert joined.loc[0, "orbit_regime"] == "LEO"


def test_a_satellite_missing_from_the_dimension_survives_as_null() -> None:
    """A left join, so an unresolvable key is visible rather than dropped.

    The relationship test in dbt guarantees this cannot happen today.
    If that ever stops being true, nulls on the map are a far better
    failure than satellites silently vanishing.
    """
    joined = attach_dimension(positions_to_frame([_position(99)]), _dimension())

    assert len(joined) == 1
    assert pd.isna(joined.loc[0, "orbit_regime"])


# ── classify_staleness ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("regime", "age_hours", "expected_stale"),
    [
        ("LEO", 47.0, False),
        ("LEO", 49.0, True),
        ("MEO", 49.0, False),  # would be stale as LEO, is normal for MEO
        ("MEO", 169.0, True),
        ("GEO/HEO", 95.0, False),
        ("GEO/HEO", 97.0, True),
    ],
)
def test_staleness_is_judged_per_regime(
    regime: str, age_hours: float, expected_stale: bool
) -> None:
    """The whole point of orbit_regime.

    A MEO satellite at 49 hours is entirely normal — negligible drag
    makes high orbits predictable, so operators republish far less often.
    A flat threshold would flag a fifth of the Galileo constellation.
    """
    frame = _frame(orbit_regime=regime, epoch_age_hours=age_hours)

    assert bool(frame.loc[0, "is_stale"]) is expected_stale


def test_a_future_epoch_is_early_not_stale() -> None:
    """`epoch_age_hours` is signed, and negative means the epoch is ahead.

    Three deep-space objects publish elements up to two days in the
    future. Comparing the magnitude rather than the value would report
    them as the stalest things in the catalogue.
    """
    frame = _frame(orbit_regime="GEO/HEO", epoch_age_hours=-48.0)

    assert not bool(frame.loc[0, "is_stale"])


def test_an_unknown_regime_falls_back_to_a_threshold() -> None:
    """A null regime must not make `is_stale` null and break the filter."""
    frame = _frame(orbit_regime=None, epoch_age_hours=100.0)

    assert bool(frame.loc[0, "is_stale"]) is True


def test_every_regime_has_a_threshold() -> None:
    """A regime with no threshold would silently take the fallback."""
    assert set(REGIME_COLOURS) <= set(STALENESS_THRESHOLD_HOURS)


# ── add_colours ──────────────────────────────────────────────────────


@pytest.mark.parametrize("regime", list(REGIME_COLOURS))
def test_each_regime_gets_its_own_colour(regime: str) -> None:
    """Colour carries identity, so each regime needs a distinct hue."""
    frame = _frame(orbit_regime=regime)

    assert frame.loc[0, "colour"] == REGIME_COLOURS[regime]


def test_unknown_regimes_fold_to_neutral() -> None:
    """`unknown` is not a fourth categorical slot.

    Four hues do not clear the all-pairs colour-separation floors, and a
    map is an all-pairs form — any two colours can land next to each
    other. So the unknown bucket folds to a recessive grey instead.
    """
    frame = _frame(orbit_regime="unknown")

    assert frame.loc[0, "colour"] == NEUTRAL_COLOUR
    assert NEUTRAL_COLOUR not in REGIME_COLOURS.values()


# ── apply_filters ────────────────────────────────────────────────────


def _filters(**overrides) -> dict:
    """Default filter arguments that select everything."""
    return {
        "regimes": ["LEO", "MEO", "GEO/HEO", "unknown"],
        "object_types": ["PAY", "R/B", "DEB", "UNK"],
        "altitude_range": (0.0, 200_000.0),
        "name_query": "",
        "hide_stale": False,
    } | overrides


def test_filters_pass_everything_by_default() -> None:
    """The default sidebar state must not hide anything."""
    assert len(apply_filters(_frame(), **_filters())) == 1


def test_regime_filter_excludes() -> None:
    """Deselecting a regime removes its objects."""
    assert apply_filters(_frame(orbit_regime="LEO"), **_filters(regimes=["MEO"])).empty


def test_object_type_filter_excludes() -> None:
    """Deselecting an object type removes its objects."""
    assert apply_filters(
        _frame(object_type="PAY"), **_filters(object_types=["DEB"])
    ).empty


def test_altitude_filter_is_inclusive_at_the_bounds() -> None:
    """A satellite exactly on the slider bound stays visible.

    An exclusive comparison would make the full-range default silently
    drop the highest and lowest objects.
    """
    assert len(apply_filters(_frame(), **_filters(altitude_range=(420.0, 420.0)))) == 1


def test_name_search_is_case_insensitive() -> None:
    """Typing 'starlink' should find STARLINK-1234."""
    frame = _frame(object_name="STARLINK-1234")

    assert len(apply_filters(frame, **_filters(name_query="starlink"))) == 1


def test_name_search_tolerates_a_missing_name() -> None:
    """A null object name must not raise, just fail to match."""
    frame = _frame(object_name=None)

    assert apply_filters(frame, **_filters(name_query="starlink")).empty


def test_hide_stale_drops_only_stale_rows() -> None:
    """The staleness filter is opt-in, and removes exactly what it flags."""
    fresh = _frame(orbit_regime="LEO", epoch_age_hours=6.0)
    stale = _frame(orbit_regime="LEO", epoch_age_hours=100.0)

    assert len(apply_filters(fresh, **_filters(hide_stale=True))) == 1
    assert apply_filters(stale, **_filters(hide_stale=True)).empty


def test_an_empty_frame_survives_every_filter() -> None:
    """No positions is a valid state, not an error."""
    empty = add_colours(
        classify_staleness(attach_dimension(positions_to_frame([]), _dimension()))
    )

    assert apply_filters(empty, **_filters()).empty


# ── The dimension query, against the real warehouse ──────────────────


def _postgres_available() -> bool:
    """Check whether the configured Postgres instance accepts connections."""
    try:
        with psycopg.connect(settings.postgres_dsn, connect_timeout=2):
            return True
    except psycopg.Error:
        return False


@pytest.mark.skipif(
    not _postgres_available(),
    reason="Postgres is not reachable; run `docker compose up -d` to exercise this.",
)
def test_the_dimension_query_returns_every_column_the_map_uses() -> None:
    """The query must actually supply what the fixtures pretend it does.

    This is the test that would have caught `object_name` being absent
    from the real query while present in the fixture — a gap no amount
    of unit testing could see, because both sides were mine.
    """
    with psycopg.connect(settings.postgres_dsn) as connection:
        row = connection.execute(f"{_DIMENSION_QUERY} LIMIT 1").fetchone()
        names = [column.name for column in connection.execute(
            f"{_DIMENSION_QUERY} LIMIT 0"
        ).description]

    assert row is not None, "gold.dim_object is empty; run sat-tracker-transform"
    assert names == list(DIMENSION_COLUMNS)


# ── Deck construction ────────────────────────────────────────────────


def _drawable_frame() -> pd.DataFrame:
    """A frame with the columns the layers read."""
    return _frame()


def test_flat_map_uses_a_basemap_and_one_layer() -> None:
    """The default projection is the reliable one: tiles plus satellites."""
    spec = json.loads(_build_deck(_drawable_frame(), globe=False).to_json())

    assert [layer["@@type"] for layer in spec["layers"]] == ["ScatterplotLayer"]
    assert spec["mapStyle"]


def test_globe_uses_the_globe_view() -> None:
    """The toggle must actually switch deck.gl's projection."""
    spec = json.loads(_build_deck(_drawable_frame(), globe=True).to_json())

    assert [view["@@type"] for view in spec["views"]] == ["_GlobeView"]


def test_globe_draws_its_own_planet() -> None:
    """A globe cannot use raster tiles, so land must come from vectors.

    Map tiles are Mercator-projected images and will not drape on a
    sphere. Without these layers the satellites would float against an
    empty background with nothing to locate them against.
    """
    spec = json.loads(_build_deck(_drawable_frame(), globe=True).to_json())
    layers = [layer["@@type"] for layer in spec["layers"]]

    assert layers == ["SolidPolygonLayer", "GeoJsonLayer", "ScatterplotLayer"]
    assert spec.get("mapStyle") is None


def test_the_land_asset_is_usable_geojson() -> None:
    """The bundled outlines must parse and carry geometry.

    Committed rather than fetched at runtime so the map works offline.
    """
    land = load_land.__wrapped__()

    assert land["type"] == "FeatureCollection"
    assert len(land["features"]) > 100
    assert all(f["geometry"]["coordinates"] for f in land["features"])


# ── Orbit tracks ─────────────────────────────────────────────────────


def _track(points: list[tuple[float, float, float]]) -> Track:
    """Build a track from bare coordinates."""
    return Track(
        norad_cat_id=25544, object_name="ISS (ZARYA)", period_minutes=92.9, points=points
    )


def test_a_path_that_never_crosses_stays_one_segment() -> None:
    """The common case must not be split, or every track would break apart."""
    points = [(0.0, 10.0, 420.0), (1.0, 20.0, 420.0), (2.0, 30.0, 420.0)]

    assert split_at_antimeridian(points) == [points]


def test_a_path_crossing_the_dateline_is_split() -> None:
    """The single most common ground-track bug.

    Going from 179E to 179W is two degrees of travel but a 358 degree
    jump in coordinates. Drawn unsplit on a flat map it becomes a
    horizontal line straight across the world.
    """
    segments = split_at_antimeridian(
        [(0.0, 178.0, 420.0), (1.0, 179.5, 420.0), (2.0, -179.0, 420.0), (3.0, -177.0, 420.0)]
    )

    assert len(segments) == 2
    assert [point[1] for point in segments[0]] == [178.0, 179.5]
    assert [point[1] for point in segments[1]] == [-179.0, -177.0]


def test_a_large_but_legitimate_step_is_not_split() -> None:
    """Only a jump beyond 180 degrees is impossible between samples."""
    points = [(0.0, -80.0, 420.0), (1.0, 80.0, 420.0)]

    assert len(split_at_antimeridian(points)) == 1


def test_multiple_crossings_produce_multiple_segments() -> None:
    """A long track can wrap more than once."""
    segments = split_at_antimeridian(
        [
            (0.0, 165.0, 420.0), (1.0, 170.0, 420.0),
            (2.0, -170.0, 420.0), (3.0, -100.0, 420.0),
            (4.0, 170.0, 420.0), (5.0, 175.0, 420.0),
        ]
    )

    assert len(segments) == 3


def test_a_segment_of_one_point_is_dropped() -> None:
    """A lone vertex cannot be drawn as a line and would render as nothing."""
    segments = split_at_antimeridian([(0.0, 179.0, 420.0), (1.0, -179.0, 420.0)])

    assert segments == []


def test_flat_paths_are_two_dimensional() -> None:
    """A flat map has nowhere to put altitude."""
    paths = tracks_to_paths([_track([(0.0, 10.0, 420.0), (1.0, 20.0, 420.0)])], globe=False)

    assert len(paths) == 1
    assert all(len(vertex) == 2 for vertex in paths[0]["path"])


def test_globe_paths_carry_altitude_in_metres() -> None:
    """Drawing at true altitude is the thing a globe can do and a flat map cannot.

    deck.gl elevations are metres, while the pipeline works in
    kilometres throughout, so this is also the one place that
    conversion has to happen.
    """
    paths = tracks_to_paths([_track([(0.0, 10.0, 420.0), (1.0, 20.0, 420.0)])], globe=True)
    vertex = paths[0]["path"][0]

    assert len(vertex) == 3
    assert vertex == [10.0, 0.0, 420_000.0]


def test_globe_paths_are_not_split() -> None:
    """A sphere has no dateline, so splitting there would sever a real path."""
    crossing = _track(
        [
            (0.0, 177.0, 420.0),
            (1.0, 179.0, 420.0),
            (2.0, -179.0, 420.0),
            (3.0, -177.0, 420.0),
        ]
    )

    # One unbroken path on the globe, two on the flat map.
    assert len(tracks_to_paths([crossing], globe=True)) == 1
    assert len(tracks_to_paths([crossing], globe=False)) == 2


def test_paths_keep_the_satellite_identity() -> None:
    """The tooltip needs to name what the line belongs to."""
    paths = tracks_to_paths([_track([(0.0, 10.0, 420.0), (1.0, 20.0, 420.0)])], globe=False)

    assert paths[0]["norad_cat_id"] == 25544
    assert paths[0]["object_name"] == "ISS (ZARYA)"


def test_a_deck_with_tracks_draws_them_under_the_satellites() -> None:
    """A satellite must never be hidden by its own orbit."""
    paths = tracks_to_paths([_track([(0.0, 10.0, 420.0), (1.0, 20.0, 420.0)])], globe=False)
    spec = json.loads(_build_deck(_frame(), globe=False, paths=paths).to_json())

    assert [layer["@@type"] for layer in spec["layers"]] == [
        "PathLayer",
        "ScatterplotLayer",
    ]


def test_a_deck_without_tracks_has_no_path_layer() -> None:
    """Selecting nothing must not add an empty layer."""
    spec = json.loads(_build_deck(_frame(), globe=False, paths=[]).to_json())

    assert "PathLayer" not in [layer["@@type"] for layer in spec["layers"]]


# ── Focus and click selection ────────────────────────────────────────


def test_nothing_focused_leaves_everything_opaque() -> None:
    """With no track selected the map must look exactly as before."""
    focused = apply_focus(_frame(), set())

    assert focused.loc[0, "colour"][3] == FULL_ALPHA


def test_tracing_dims_everything_else() -> None:
    """A single orbit is only legible against the objects around it.

    Filtering them away would leave a line with nothing to read it
    against, so they fade rather than disappear.
    """
    frame = pd.concat(
        [_frame(object_name="ISS"), _frame(object_name="OTHER")], ignore_index=True
    )

    focused = apply_focus(frame, {"ISS"})

    assert focused.loc[0, "colour"][3] == FULL_ALPHA
    assert focused.loc[1, "colour"][3] == FADED_ALPHA


def test_fading_preserves_the_regime_hue() -> None:
    """Alpha carries focus; hue keeps carrying identity.

    A dimmed LEO satellite must still be blue, or the fade would destroy
    the encoding the legend describes.
    """
    frame = _frame(object_name="OTHER", orbit_regime="LEO")

    faded = apply_focus(frame, {"ISS"}).loc[0, "colour"]

    assert faded[:3] == REGIME_COLOURS["LEO"]


def test_focus_is_idempotent() -> None:
    """Applying it twice must not stack alpha channels.

    Streamlit reruns the whole script on every interaction, so a
    function that appended rather than replaced would produce longer and
    longer colour lists on each click.
    """
    once = apply_focus(_frame(), {"ISS"})
    twice = apply_focus(once, {"ISS"})

    assert len(twice.loc[0, "colour"]) == 4


def test_a_click_yields_the_object_name() -> None:
    """Streamlit returns the underlying rows, keyed by layer id."""
    event = {"objects": {SATELLITE_LAYER_ID: [{"object_name": "ISS (ZARYA)"}]}}

    assert selected_names(event) == {"ISS (ZARYA)"}


def test_multiple_clicks_yield_multiple_names() -> None:
    """`selection_mode` is multi-object, so several can arrive at once."""
    event = {
        "objects": {SATELLITE_LAYER_ID: [{"object_name": "A"}, {"object_name": "B"}]}
    }

    assert selected_names(event) == {"A", "B"}


@pytest.mark.parametrize(
    "event",
    [None, {}, {"objects": {}}, {"objects": {"other_layer": [{"object_name": "A"}]}},
     {"objects": {SATELLITE_LAYER_ID: [{}]}}, "not a dict"],
    ids=["none", "empty", "no-layer", "wrong-layer", "no-name", "wrong-type"],
)
def test_an_unusable_selection_is_treated_as_no_selection(event: object) -> None:
    """A click is not worth crashing the map over.

    The event's shape is set by Streamlit and deck.gl rather than by
    this project, so anything unexpected degrades to "nothing selected".
    """
    assert selected_names(event) == set()


# ── Globe rendering ──────────────────────────────────────────────────


def test_the_globe_frame_keeps_only_what_it_draws() -> None:
    """The globe embeds its data in the page, so unused columns cost bytes.

    Untrimmed, the full catalogue produces a 14 MB document the browser
    must parse on every rerun.
    """
    trimmed = trim_for_globe(add_elevation(_frame()))

    assert list(trimmed.columns) == list(GLOBE_RENDER_COLUMNS)
    # The TEME state vector is never drawn or tooltipped, so it is dead
    # weight in a document the browser has to parse.
    for unused in ("position_x_km", "velocity_x_km_s", "epoch", "snapshot_ts"):
        assert unused not in trimmed.columns


def test_the_globe_frame_keeps_every_row() -> None:
    """Trimming is about columns; dropping satellites would be a lie."""
    frame = pd.concat([_frame(), _frame()], ignore_index=True)

    assert len(trim_for_globe(add_elevation(frame))) == 2


def test_coordinates_are_rounded_to_a_visible_precision() -> None:
    """Three decimals of latitude is ~100 m, finer than a globe resolves."""
    frame = _frame()
    frame.loc[0, "latitude_deg"] = 51.123456789

    assert trim_for_globe(add_elevation(frame)).loc[0, "latitude_deg"] == pytest.approx(
        51.123
    )


def test_an_empty_globe_frame_is_not_an_error() -> None:
    """Filters can exclude everything, and that must not raise."""
    empty = add_colours(
        classify_staleness(attach_dimension(positions_to_frame([]), _dimension()))
    )

    assert trim_for_globe(add_elevation(empty)).empty


def test_the_globe_page_actually_asks_for_a_globe() -> None:
    """The whole reason this path exists.

    `st.pydeck_chart` cannot draw a globe — Streamlit ships a deck.gl
    build with no globe module, so a `_GlobeView` spec silently falls
    back to a flat map. pydeck's own HTML loads the full bundle, so the
    view survives into the rendered page.
    """
    html = globe_html(_frame(), [])

    assert "_GlobeView" in html
    assert html.lstrip().startswith("<!DOCTYPE html>")


def test_the_globe_page_carries_its_orbit_paths() -> None:
    """Tracks must survive into the embedded document, not just the deck."""
    paths = tracks_to_paths(
        [_track([(0.0, 10.0, 420.0), (1.0, 20.0, 420.0)])], globe=True
    )

    assert "PathLayer" in globe_html(_frame(), paths)


# ── Altitude on the globe ────────────────────────────────────────────


def test_elevation_converts_kilometres_to_metres() -> None:
    """deck.gl works in metres; the pipeline works in kilometres throughout."""
    assert add_elevation(_frame()).loc[0, "elevation_m"] == pytest.approx(420_000.0)


def test_true_scale_is_the_default() -> None:
    """A globe that quietly exaggerates would be lying about the geometry."""
    lifted = add_elevation(_frame())

    assert lifted.loc[0, "elevation_m"] == lifted.loc[0, "altitude_km"] * 1000.0


@pytest.mark.parametrize("exaggeration", [2.0, 5.0, 20.0])
def test_exaggeration_scales_elevation(exaggeration: float) -> None:
    """Opt-in distortion, so LEO can be separated from the surface.

    At true scale the ISS sits 6.6% of an Earth radius up, which is a
    hair's breadth on screen — accurate, but hard to read.
    """
    lifted = add_elevation(_frame(), exaggeration)

    assert lifted.loc[0, "elevation_m"] == pytest.approx(420_000.0 * exaggeration)


def test_exaggeration_preserves_the_ordering_of_regimes() -> None:
    """Scaling must not reorder anything — a GEO object stays above a LEO one."""
    frame = pd.concat(
        [_frame(), _frame().assign(altitude_km=35786.0)], ignore_index=True
    )

    lifted = add_elevation(frame, 10.0)

    assert lifted.loc[1, "elevation_m"] > lifted.loc[0, "elevation_m"]


def test_elevation_survives_the_globe_trim() -> None:
    """Trimming for payload size must not drop the column that lifts the dots."""
    assert "elevation_m" in trim_for_globe(add_elevation(_frame())).columns


def test_the_globe_layer_reads_three_dimensions() -> None:
    """The bug this fixes: dots were drawn flat while only paths had altitude."""
    spec = json.loads(
        _build_deck(trim_for_globe(add_elevation(_frame())), globe=True).to_json()
    )
    layer = next(l for l in spec["layers"] if l["@@type"] == "ScatterplotLayer")

    assert "elevation_m" in layer["getPosition"]


def test_the_flat_layer_stays_two_dimensional() -> None:
    """A Mercator map has nowhere to put altitude.

    deck.gl would read a third component as an elevation in a projection
    where that means something quite different.
    """
    spec = json.loads(_build_deck(_frame(), globe=False).to_json())

    assert "elevation_m" not in spec["layers"][0]["getPosition"]


def test_elevation_on_an_empty_frame_is_not_an_error() -> None:
    """Filters can exclude everything."""
    empty = add_colours(
        classify_staleness(attach_dimension(positions_to_frame([]), _dimension()))
    )

    assert "elevation_m" in add_elevation(empty).columns


# ── Longitude unwrapping (the globe's dateline treatment) ────────────


def _max_step(points: list[tuple[float, float, float]]) -> float:
    """Largest longitude change between consecutive points."""
    return max(abs(b[1] - a[1]) for a, b in pairwise(points))


def test_unwrapping_removes_the_dateline_jump() -> None:
    """The property that actually matters, stated directly.

    `PathLayer` interpolates in longitude space before projecting, so any
    step above 180 degrees is drawn the long way round the planet. After
    unwrapping there must be no such step.
    """
    points = [(0.0, 178.0, 420.0), (1.0, 179.5, 420.0), (2.0, -179.0, 420.0)]

    assert _max_step(unwrap_longitudes(points)) < 180.0


def test_unwrapping_continues_past_180() -> None:
    """179.5 to -179 becomes 179.5 to 181.

    Longitudes outside the usual range are valid on a sphere — 181 is
    -179 — and the projection wraps them correctly.
    """
    points = [(0.0, 179.0, 420.0), (1.0, -179.0, 420.0), (2.0, -177.0, 420.0)]

    assert [p[1] for p in unwrap_longitudes(points)] == [179.0, 181.0, 183.0]


def test_unwrapping_handles_a_westward_crossing() -> None:
    """The correction has to work in both directions."""
    points = [(0.0, -179.0, 420.0), (1.0, 179.0, 420.0), (2.0, 177.0, 420.0)]

    assert [p[1] for p in unwrap_longitudes(points)] == [-179.0, -181.0, -183.0]


def test_unwrapping_accumulates_over_several_crossings() -> None:
    """A high-inclination orbit crosses the antimeridian twice per revolution.

    One closed path can legitimately run well past 360 degrees of
    unwrapped longitude, so the offset has to accumulate rather than
    being applied once.
    """
    points = [
        (0.0, 170.0, 420.0), (1.0, -170.0, 420.0),
        (2.0, 170.0, 420.0), (3.0, -170.0, 420.0),
    ]

    unwrapped = [p[1] for p in unwrap_longitudes(points)]

    assert unwrapped == [170.0, 190.0, 170.0, 190.0]
    assert _max_step(unwrap_longitudes(points)) < 180.0


def test_unwrapping_leaves_an_ordinary_path_alone() -> None:
    """A path that never crosses must come back byte-identical."""
    points = [(0.0, 10.0, 420.0), (1.0, 20.0, 420.0), (2.0, 30.0, 420.0)]

    assert unwrap_longitudes(points) == points


def test_unwrapping_preserves_latitude_and_altitude() -> None:
    """Only longitude is adjusted; the other two components are untouched."""
    points = [(51.5, 179.0, 420.0), (52.0, -179.0, 430.0)]

    unwrapped = unwrap_longitudes(points)

    assert [p[0] for p in unwrapped] == [51.5, 52.0]
    assert [p[2] for p in unwrapped] == [420.0, 430.0]


def test_unwrapping_a_short_path_is_safe() -> None:
    """Fewer than two points has nothing to unwrap."""
    assert unwrap_longitudes([]) == []
    assert unwrap_longitudes([(0.0, 10.0, 420.0)]) == [(0.0, 10.0, 420.0)]


def test_the_globe_unwraps_where_the_flat_map_splits() -> None:
    """The two projections need mirror treatments of the same problem.

    A flat map cut leaves a gap the globe must not have; a globe sweep
    is a wrong line the flat map would never draw.
    """
    crossing = _track(
        [
            (0.0, 177.0, 420.0), (1.0, 179.0, 420.0),
            (2.0, -179.0, 420.0), (3.0, -177.0, 420.0),
        ]
    )

    globe_paths = tracks_to_paths([crossing], globe=True)
    flat_paths = tracks_to_paths([crossing], globe=False)

    # One unbroken path on the globe, cut in two on the flat map.
    assert len(globe_paths) == 1
    assert len(flat_paths) == 2
    # And the unbroken one never asks the renderer to go the long way.
    longitudes = [vertex[0] for vertex in globe_paths[0]["path"]]
    assert max(abs(b - a) for a, b in pairwise(longitudes)) < 180.0


# ── Speed and launch site ────────────────────────────────────────────


def test_speed_is_the_magnitude_of_the_velocity_vector() -> None:
    """SGP4 already returns velocity; this only takes its length."""
    frame = _frame()  # velocity components are (-1.5, 6.9, 3.2)

    expected = (1.5**2 + 6.9**2 + 3.2**2) ** 0.5

    assert add_speed(frame).loc[0, "speed_km_s"] == pytest.approx(expected)


def test_speed_is_positive_regardless_of_direction() -> None:
    """A magnitude has no sign, whichever way the satellite is travelling."""
    frame = _frame()
    frame.loc[0, ["velocity_x_km_s", "velocity_y_km_s", "velocity_z_km_s"]] = [
        -7.0, -1.0, -2.0
    ]

    assert add_speed(frame).loc[0, "speed_km_s"] > 0


def test_a_low_orbit_speed_is_about_seven_and_a_half() -> None:
    """The sanity check for the units.

    A circular low orbit travels near 7.6 km/s. Reporting m/s or km/h
    would be off by three orders of magnitude and pass every other test
    here.
    """
    frame = _frame()
    frame.loc[0, ["velocity_x_km_s", "velocity_y_km_s", "velocity_z_km_s"]] = [
        -1.5, 6.9, 3.2
    ]

    assert 7.0 < add_speed(frame).loc[0, "speed_km_s"] < 8.0


def test_speed_on_an_empty_frame_is_not_an_error() -> None:
    """Filters can exclude everything."""
    empty = attach_dimension(positions_to_frame([]), _dimension())

    assert "speed_km_s" in add_speed(empty).columns


@pytest.mark.parametrize(
    ("code", "expected"),
    [("AFETR", "Cape Canaveral"), ("TYMSC", "Baikonur"), ("FRGUI", "Kourou")],
)
def test_launch_site_codes_become_place_names(code: str, expected: str) -> None:
    """`AFETR` is Cape Canaveral, which no reader should have to know."""
    frame = _frame(launch_site=code)

    assert name_launch_sites(frame).loc[0, "launch_site_name"] == expected


def test_an_unknown_launch_site_keeps_its_code() -> None:
    """A code is less useful than a name but far more useful than a blank.

    SATCAT carries a long tail of sites with a handful of objects each,
    and new ones appear as new pads come into service.
    """
    frame = _frame(launch_site="ZZZZZ")

    assert name_launch_sites(frame).loc[0, "launch_site_name"] == "ZZZZZ"


def test_a_missing_launch_site_does_not_raise() -> None:
    """Not every catalogued object records where it launched from."""
    frame = _frame(launch_site=None)

    assert "launch_site_name" in name_launch_sites(frame).columns


def test_the_globe_payload_carries_the_new_columns() -> None:
    """Speed, owner and launch details must survive the trim for the tooltip."""
    frame = name_launch_sites(add_speed(_frame()))

    trimmed = trim_for_globe(add_elevation(frame))

    for column in ("speed_km_s", "owner", "launch_date", "launch_site_name"):
        assert column in trimmed.columns


def test_orbit_paths_scale_with_the_same_exaggeration_as_the_dots() -> None:
    """A satellite must never be drawn floating off its own orbit.

    `add_elevation` lifts the dots and `tracks_to_paths` lifts the paths.
    If only one honours the slider, the dot moves outward while the orbit
    stays pinned at true altitude, and the two visibly separate.
    """
    track = _track([(0.0, 10.0, 420.0), (1.0, 20.0, 420.0)])
    frame = _frame()

    for exaggeration in (1.0, 5.0, 20.0):
        dot = add_elevation(frame, exaggeration).loc[0, "elevation_m"]
        path = tracks_to_paths(
            [track], globe=True, exaggeration=exaggeration
        )[0]["path"][0][2]

        assert path == pytest.approx(dot)


def test_paths_default_to_true_scale() -> None:
    """Omitting the argument must not silently exaggerate."""
    track = _track([(0.0, 10.0, 420.0), (1.0, 20.0, 420.0)])

    assert tracks_to_paths([track], globe=True)[0]["path"][0][2] == 420_000.0


def test_exaggeration_does_not_reach_the_flat_map() -> None:
    """A Mercator path has no altitude component to scale."""
    track = _track([(0.0, 10.0, 420.0), (1.0, 20.0, 420.0)])

    vertex = tracks_to_paths([track], globe=False, exaggeration=10.0)[0]["path"][0]

    assert len(vertex) == 2


# ── Observer visibility ──────────────────────────────────────────────

BERLIN_LAT, BERLIN_LON = 52.52, 13.40


def test_a_satellite_overhead_reads_as_ninety_degrees() -> None:
    """The app-level check that the observer wiring is the right way round."""
    frame = _frame()
    frame.loc[0, ["latitude_deg", "longitude_deg", "altitude_km"]] = [
        BERLIN_LAT, BERLIN_LON, 400.0
    ]

    angled = add_look_angles(frame, BERLIN_LAT, BERLIN_LON)

    assert angled.loc[0, "elevation_deg"] == pytest.approx(90.0, abs=1e-6)
    assert angled.loc[0, "range_km"] == pytest.approx(400.0, abs=1e-6)


def test_a_satellite_on_the_far_side_is_below_the_horizon() -> None:
    """Negative elevation is what the filter exists to remove."""
    frame = _frame()
    frame.loc[0, ["latitude_deg", "longitude_deg", "altitude_km"]] = [
        -BERLIN_LAT, BERLIN_LON - 180.0, 400.0
    ]

    assert add_look_angles(frame, BERLIN_LAT, BERLIN_LON).loc[0, "elevation_deg"] < 0


def test_moving_the_observer_changes_the_angles() -> None:
    """The observer inputs must actually reach the computation.

    A wiring mistake that ignored them would still produce plausible
    angles, just always the same ones.
    """
    frame = _frame()
    frame.loc[0, ["latitude_deg", "longitude_deg", "altitude_km"]] = [
        BERLIN_LAT, BERLIN_LON, 400.0
    ]

    here = add_look_angles(frame, BERLIN_LAT, BERLIN_LON).loc[0, "elevation_deg"]
    elsewhere = add_look_angles(frame, -33.9, 151.2).loc[0, "elevation_deg"]

    assert here > 80.0
    assert elsewhere < 0.0


def test_the_horizon_filter_keeps_only_what_is_up() -> None:
    """Zero degrees is the geometric horizon."""
    overhead = _frame(object_name="UP")
    overhead.loc[0, ["latitude_deg", "longitude_deg", "altitude_km"]] = [
        BERLIN_LAT, BERLIN_LON, 400.0
    ]
    below = _frame(object_name="DOWN")
    below.loc[0, ["latitude_deg", "longitude_deg", "altitude_km"]] = [
        -BERLIN_LAT, BERLIN_LON - 180.0, 400.0
    ]

    frame = add_look_angles(
        pd.concat([overhead, below], ignore_index=True), BERLIN_LAT, BERLIN_LON
    )
    kept = filter_visible(frame, 0)

    assert kept["object_name"].tolist() == ["UP"]


@pytest.mark.parametrize("threshold", [0, 10, 30, 60])
def test_a_higher_threshold_never_keeps_more(threshold: int) -> None:
    """Raising the floor can only remove objects, never add them.

    Guards against a comparison written the wrong way round, which would
    still return a plausible-looking subset.
    """
    frame = _frame()
    frame.loc[0, ["latitude_deg", "longitude_deg", "altitude_km"]] = [
        BERLIN_LAT + 3.0, BERLIN_LON, 500.0
    ]
    angled = add_look_angles(frame, BERLIN_LAT, BERLIN_LON)

    assert len(filter_visible(angled, threshold)) <= len(filter_visible(angled, 0))


def test_an_empty_frame_survives_the_observer_pipeline() -> None:
    """Filters upstream can exclude everything before the observer runs."""
    empty = attach_dimension(positions_to_frame([]), _dimension())

    angled = add_look_angles(empty, BERLIN_LAT, BERLIN_LON)

    assert "elevation_deg" in angled.columns
    assert filter_visible(angled, 10).empty


def test_look_angles_leave_the_other_columns_alone() -> None:
    """Adding three columns must not disturb what the map already draws."""
    frame = _frame()

    angled = add_look_angles(frame, BERLIN_LAT, BERLIN_LON)

    assert angled.loc[0, "object_name"] == frame.loc[0, "object_name"]
    assert angled.loc[0, "colour"] == frame.loc[0, "colour"]
    assert set(frame.columns) <= set(angled.columns)
