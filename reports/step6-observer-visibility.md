# Step 6 — Observer visibility and pass prediction

Completed 2026-08-20, on `feature/observer-visibility`.

The map could say where every object was. It could not say whether you
could see any of them from where you were standing — which is the question
most people actually have about satellites.

Answering it needed one transformation the pipeline lacked, and it surfaced
three defects that had been latent for weeks. This report records both,
because the defects are the more interesting half.

---

## What was built

| | |
|---|---|
| `frames.geodetic_to_ecef` | Promoted out of the test suite |
| `frames.ecef_to_look_angles` | Azimuth, elevation and range from a point on the ground |
| `propagate/passes.py` | Three-day pass search |
| App | Observer position, horizon filter, pass table, colour-by-type |
| Tests | 266 → 311 |

---

## Decision 1 — the oracle was already the implementation

`geodetic_to_ecef` existed in `tests/test_frames.py`, written months
earlier as the **oracle** for `ecef_to_geodetic`: the easy, closed-form
direction used to generate known vectors that the hard, approximate
direction had to invert.

Placing an observer needs exactly that conversion. So the function written
to *check* a transform turned out to be the one the next feature required,
and it moved into production unchanged. The test file now aliases it, which
means the round-trip tests exercise what ships rather than a lookalike that
could drift from it.

This is the second time this project has promoted a test oracle. The
pattern is worth naming: **a function written to verify something is, by
construction, the simplest correct implementation of it.** When a feature
later needs that conversion, the oracle is already the right answer.

---

## Decision 2 — rotate the range vector, not the position

The transformation itself is three steps: subtract the observer's position
from the satellite's, rotate the difference into the observer's
East-North-Up frame, read off the angles.

The trap is in the first step. Feeding the satellite's own ECEF vector into
the rotation gives the direction from the **centre of the Earth** rather
than from the observer — wrong by up to 90°, and still a perfectly
plausible-looking bearing.

The test that catches it does not check a number. It requires **two
different observers to disagree about the same satellite**, which the
mistaken version cannot do, because the direction from Earth's centre
barely depends on where you stand.

Two smaller traps, both about convention rather than geometry: `atan2(E, N)`
takes East first, because azimuth is measured clockwise *from* north — the
reverse of the usual `atan2(y, x)`. And the result runs (−180, 180], so due
west arrives as −90 and must be normalised to 270. A compass has no
negatives.

All 23 tests passed on the first run of the implementation.

---

## Decision 3 — a search, not an equation

Finding passes could be posed as root-finding: solve for the times when
elevation crosses zero. That would be more elegant and considerably more
fragile. SGP4 has no closed form to differentiate, passes arrive in
clusters, and a solver landing on the wrong root produces a plausible time
for a pass that never happens.

Dense sampling instead. Three days at 30-second steps is 8,640 samples —
one `Satrec.sgp4_array` call, a few milliseconds — and the runs above a
threshold are the passes.

**Sampling can be coarse, but it cannot be subtly wrong.** That is the
right trade when the output is a time someone will stand outside for.

Step size bounds how precisely an edge can be located, since the true
crossing lies inside the last step. The test that justifies 30 seconds does
not assert a number: it requires that **halving the step does not change
the pass count**, which would mean the sampling was too coarse to resolve
adjacent passes.

**Truncation is reported rather than hidden.** A pass touching either end
of the window was already in progress, so the reported time is the window's
edge. Silently presenting it as a real crossing would be wrong data dressed
as right; dropping such passes would hide the most actionable row in the
table, since a satellite overhead *right now* is exactly what someone
wants. So it is flagged, and the duration read as a lower bound.

Verified against the real ISS from Berlin: **13 passes over 72 hours** —
4.3 per day, the textbook figure — lasting 2.0 to 6.5 minutes, peaking
between 11° and 71°. They arrive in a morning cluster and then stop for a
day, which is the ground track's westward march showing up in the output
without anyone annotating it.

---

## What the feature exposed

### Names are not identifiers

The pass selector, the orbit-track multiselect and the fade all keyed on
`object_name`. That held for weeks — and broke the moment debris arrived,
because breakup fragments are catalogued by **event**, not individually.

All **1,938 Fengyun-1C pieces share the single name `FENGYUN 1C DEB`**. The
selector offered one entry standing for 1,938 satellites and resolved it
with `next()` to whichever came first, while the fade highlighted every one
of them at once. Across the propagatable catalogue, 18,992 objects carry
only 16,356 distinct names: **2,636 were unreachable by name at all**.

The warehouse had never made this mistake. `norad_cat_id` is the grain of
`dim_object`, the join key of every fact, the thing `unique` and
`relationships` tests guard. Only the UI conflated a label with an
identifier.

Worth noting how it surfaced: not from a test, but from ingesting real
data. Every fixture had been written with distinct names, so the tests
could not have caught it — the same fixture-drift trap as Step 4's missing
`object_name`, wearing different clothes.

### An early return split the two projections

The pass table appeared on the flat map and silently not on the globe,
because the globe branch ended in `return` and anything added below the map
only reached one path.

Converting `if … return` into `if … else` with a shared tail makes that
class of bug **impossible rather than fixed once**. The next thing added
below the map is automatically on both projections.

### Cache keys must not include presentation

Colouring by object type meant `add_colours` had to depend on a UI toggle —
and it was running *inside* the cached propagation. Folding a toggle into
that cache key would have re-propagated 19,000 objects on every switch: a
third of a second for what should be an instant repaint.

The rule it taught: **cached functions key on what the computation depends
on, never on what the presentation depends on.**

---

## Scope: geometric, not optical

"Visible" has two meanings and they differ enormously in effort.

| | Requires |
|---|---|
| **Geometric** | above the local horizontal |
| **Optical** | above the horizon **and** satellite sunlit **and** observer in darkness |

Optical is what makes ISS-spotting sites say "visible 21:14, magnitude
−3.2". It needs a solar ephemeris and Earth's shadow cone.

Scoped to geometric, and **the UI says so** rather than leaving the
distinction to the reader. Claiming optical visibility while computing
geometric would be the kind of quiet overstatement that is worse than the
missing feature.

---

## What the numbers say

From Berlin: **1,225 objects above the horizon, 642 above 10°, 158 above
30°.**

That 6.5% is what the geometry predicts rather than a coincidence. A
satellite's visibility footprint grows with altitude — roughly 3% of
Earth's surface at Starlink height, 38% at GPS — so a mixed population
landing near 6.5% is the expected result.

The ranges confirm the transformation independently. `STARLINK-32793` reads
**478 km away at 77.7° elevation**, barely more than its 470 km altitude,
because it is nearly overhead. `NAVSTAR 71` reads **20,155 km**, matching
GPS altitude. Computed from the satellite's own position vector rather than
the difference, both would have read about 6,700 km — an Earth radius.

Only 0.8% clear 30°, which is why practical observing uses a 10° floor:
most of what is technically up is low on the horizon and hard to see.

---

## Open items carried forward

- Optical visibility, if the sun is ever worth modelling.
- A Terraform slice — deliberately deferred until after the presentation,
  since it has the slowest feedback loop in the project and mistakes there
  cost money as well as time.
- The pass search recomputes on every parameter change. Caching is keyed on
  satellite, observer, instant and threshold, which is correct but means
  moving the observer re-searches; fine at one satellite, worth revisiting
  if a whole-sky pass view is ever wanted.
