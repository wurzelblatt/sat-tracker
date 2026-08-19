# Step 4 — The Map

Completed 2026-08-19, on `feature/start-map`.

Step 4 turned a warehouse into something you can look at. This report
records the decisions that were contested and the ones I got wrong first,
which in this step is most of them: nearly every finding here came from
running the thing and seeing it misbehave, not from reasoning about it.

The design is documented in
[`architecture.md`](../docs/architecture.md#presentation-sat_trackerapp);
what follows is why it looks the way it does.

---

## What was built

| | |
|---|---|
| `sat_tracker.app` | Streamlit map — filters, click-to-trace, two projections |
| `sat_tracker.propagate.tracks` | One satellite over one revolution |
| `assets/land.json` | Natural Earth outlines, for the globe |
| Tests | 196 → 220 Python |

---

## Decision 1 — propagate on demand, don't read the snapshot

**The question.** `gold.position_snapshot` exists and holds 16,340
positions. The obvious app reads it.

**What decided it** was a line in the project's own notes: *"real-time =
on-demand client-side propagation, NOT streaming."* Reading the snapshot
would make the map as stale as the last `sat-tracker-propagate` run.

Measured, the case is clear: a full propagation costs **0.33 s** — cheaper
than most database round trips, and certainly cheaper than the machinery
needed to keep a table current. So the map calls `elements.propagate`
itself, and a Refresh button genuinely recomputes 16,000 positions.

**What that means for `position_snapshot`.** It is not made redundant; it
keeps a different job — the PostGIS artifact an orchestrator writes and
that spatial queries run against. "Why do you have a table your app
doesn't read?" is a fair interview question with a real answer: the app
needs *now*, the warehouse needs *a record*.

**The caching split matters more than the propagation.** Streamlit reruns
the whole script on every interaction, so element sets and the dimension
are cached for the session while the propagation is cached on *the instant
it was computed for*. Without that, every checkbox click would
re-propagate the catalogue.

---

## Decision 2 — colour by regime, and only three hues

Choosing colours turned out to have a real constraint rather than a
preference.

A map is an **all-pairs** form: any two points can land beside each other,
unlike segments in a stack where only adjacent pairs meet. Under that
stricter test the categorical palette validates only **three** slots —
worst CVD separation ΔE 9.4, worst normal-vision ΔE 20.9 — and a fourth
fails the floors.

There are four orbit regimes. So `unknown` folds to a recessive grey
rather than taking a fourth hue. That is the documented "fold to Other"
rule, and it cost nothing: `unknown` is a residual bucket, not a peer of
LEO, MEO and GEO/HEO.

**Staleness is deliberately not a colour.** Colour follows the entity.
Encoding element-set age as hue would make a stale LEO satellite
indistinguishable from a fresh MEO one, destroying the encoding the legend
describes. It became a count, a per-regime flag and a filter.

Tracing fades the rest to **alpha 40** rather than filtering them away —
one orbit is only legible against the objects it sits among. Regime hue
survives the fade, so alpha carries focus while hue keeps carrying
identity, and neither has to compromise.

---

## Decision 3 — ground track or orbit path

The most interesting thing in this step, and it arrived as a bug report:
orbits on the globe were not closed.

**They were not supposed to be.** What the code drew was a *ground track*
— every sample converted at its own GMST, so the Earth turns beneath the
satellite as it goes. Over one 93-minute ISS revolution the planet rotates
about 29°, and the trace ends that far west of where it began. It never
closes, and that westward march is exactly what makes a ground track a
sinusoid rather than a loop.

An *orbit path* is the same motion in the inertial frame: freeze GMST at
one instant, every sample rotates by the same angle, and what arrives is
the orbital ellipse itself, closed and correctly placed against the Earth
as it is now.

Measured on the ISS:

| | First→last gap |
|---|---|
| Ground track | **28.97°** |
| Orbit path | **0.07°** |

**The entire difference is one line** — which Julian date the rotation is
taken at:

```python
jd = start_jd + offset if ground_track else start_jd
```

Both are correct; they answer different questions. The flat map draws the
ground track (*where did it pass over*), the globe draws the orbit path
(*what does it fly*). The endpoint handling flips with the frame too: a
ground track excludes it, a closed orbit needs that duplicate vertex to
join the curve.

My own test docstring had stated the physics correctly — *"the ground
track drifts west each revolution rather than repeating"* — while the app
asked that same function for an orbit. Knowing a fact and applying it are
different things.

---

## Decision 4 — the antimeridian, solved twice, in opposite directions

`PathLayer` interpolates between vertices in longitude/latitude space
**before** projecting. A step from 179° to −179° is two degrees of travel
but a raw difference of −358°, and the renderer draws what the numbers
say.

| | Symptom | Fix |
|---|---|---|
| Flat map | A line straight across the canvas | **Split** the path |
| Globe | A sweep the long way round, as a band parallel to the equator | **Unwrap** into continuous longitudes |

I got the globe wrong first, reasoning that "a sphere has no dateline, so
no fix is needed." True of the geometry, false of the renderer — and the
result was a band circling the planet at constant latitude.

**Applying either fix to the wrong projection makes things worse.**
Splitting a globe path leaves a gap in an orbit that is genuinely
continuous; unwrapping a flat path draws off the canvas.

Unwrapping means adding a running multiple of 360°:

```
raw         178.0   179.5   -179.0   -177.0
unwrapped   178.0   179.5    181.0    183.0
```

Longitudes outside ±180 are valid on a sphere — 181° *is* −179° — and a
high-inclination orbit crosses the antimeridian twice per revolution, so
one closed path can legitimately run past ±540°. The offset has to
accumulate, not apply once.

**The general lesson:** coordinate wrapping is a property of the
*interpolation space*, not of the surface being drawn on.

---

## The globe: a limitation, not a bug

`st.pydeck_chart` **cannot draw a globe.** Streamlit ships a trimmed
deck.gl build with no `@deck.gl/globe` module:

```
MapView        → present in streamlit/static/js/DeckGlJsonChart.js
_GlobeView     → absent
@deck.gl/globe → not bundled anywhere
```

A `_GlobeView` spec produces perfectly correct JSON that the frontend
cannot resolve, so it **silently falls back** to a flat `MapView`. The
symptom is a toggle that appears to do nothing.

**Why the tests did not catch it.** They assert on the generated JSON,
which was right. The failure lives one layer further out, in a renderer
pytest cannot exercise. That is a real boundary of unit testing, and it is
why I flagged when proposing the globe that it would need eyes on it.

The workaround renders the globe through pydeck's own `to_html` — which
loads the full deck.gl bundle from a CDN — embedded in an iframe. Two
costs, both surfaced in the UI rather than hidden:

- **It needs the network.** The land outlines are local; deck.gl is not.
- **Clicks cannot come back.** An iframe has no channel into Python, so
  selection is flat-map only.

A globe also cannot use raster basemap tiles, since those are Mercator
images that will not drape on a sphere. The planet is therefore drawn from
vector geometry — Natural Earth outlines, public domain, stripped of ~90
properties per feature and rounded to 2 decimal places: **820 KB → 165 KB**
with nothing visible lost.

---

## Altitude, and the honesty of true scale

Placing satellites at their real height produces a picture that looks
wrong until you check the numbers:

| | Height as a fraction of Earth's 6,371 km radius |
|---|---|
| ISS (420 km) | **6.6%** |
| Starlink (550 km) | 8.6% |
| GPS (20,200 km) | **3.2×** |
| Geostationary (35,786 km) | **5.6×** |

At true scale LEO is a thin skin on the planet — because that is what LEO
is. Two orders of magnitude separate it from geostationary.

True scale is the default, and the exaggeration slider is opt-in for that
reason: pushing LEO out far enough to separate visually is a deliberate
distortion, and it should be something the viewer asks for rather than
something the map does quietly.

---

## Two findings worth carrying forward

**Debris cannot be shown, and it is not a display bug.** `dim_object`
holds 35,834 DEB and 158 UNK objects, but the propagatable set contains
only PAY (16,340) and R/B (2). CelesTrak's `GROUP=active` publishes
elements for *active* objects only, so debris has no position to draw.
Showing it means ingesting per-event debris groups
(`cosmos-1408-debris`, `fengyun-1c-debris`, …), which is a data decision
with a budget cost, not a code fix.

**A test fixture drifted from the query it was standing in for.** All 25
unit tests passed while the app was broken, because I wrote both the
fixture and the SQL — and the fixture asserted what I believed rather than
what the database returned (`object_name` was in one and not the other).
The structural fix was to derive the fixture's keys from the same constant
the query is built from, plus one Postgres-backed test comparing actual
column names. Same shape as `test_columns_match_the_table_ddl` in Step 3:
when two things must agree, derive one from the other.

---

## Performance

| | |
|---|---|
| Full propagation | 0.33 s |
| 25 orbit tracks | ~2,250 vertices, milliseconds |
| Globe payload | 14.1 MB → **7.0 MB** after trimming 20 columns to 10 |

The globe embeds its data inline rather than streaming it, so column count
is a direct cost. Rounding coordinates helped far less than expected
(7.4 → 7.0 MB) — pandas rounds the value but the serialiser still writes
full float precision — while dropping unused columns did nearly all the
work. I would have predicted the opposite.

Tracks are capped at **25**. Not a performance limit but a legibility one:
the full catalogue would be ~1.5 million vertices that read as noise
rather than as orbits.

---

## Open items carried forward

- Airflow orchestration, locally via docker-compose. The DAG is five
  `BashOperator`s over commands that already work standalone, so it is
  portable to any runtime later.
- A Terraform slice — S3, RDS with PostGIS, IAM — applied, run against
  once, and destroyed.
- Debris groups, if the map should show more than payloads.
- Vectorising the frame conversion, if ground tracks are ever wanted for
  thousands of objects rather than dozens.
