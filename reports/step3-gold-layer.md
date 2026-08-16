# Step 3 — Gold Layer: SATCAT, `dim_object`, propagation

Completed 2026-08-16, over six commits on `feature/start-gold-layer`.

Step 3 took the pipeline from "deduplicated element sets in silver" to
"where every tracked object is right now, queryable spatially". This report
records the decisions that were genuinely contested and the evidence that
settled them. The resulting design is documented in
[`architecture.md`](../docs/architecture.md); what follows is why it looks
the way it does.

The recurring theme: **most of these decisions were settled by looking at
the data, not by reasoning about it.** Several of my initial plans were
wrong, and the data said so before the code was written.

---

## What was built

| | |
|---|---|
| Ingest | SATCAT client, second bronze feed, `BronzeDataset` descriptors |
| Silver | `space_object` — catalogue deduplicated to current state |
| Gold | `dim_object`, `fact_propagatable_elset`, `position_snapshot` |
| Propagation | `frames` (TEME→WGS84), `elements` (SGP4 driver) |
| Infrastructure | PostGIS, generated `geography` column, GIST index |
| Tests | 62 → 120 Python, 24 → 54 dbt |

End state, all reproducible from the CSV landing zone:

```
bronze.raw_gp                43,573
bronze.raw_satcat           140,540
silver.elset                 43,475     16,352 satellites, 27,123 closed intervals
silver.space_object          70,270
gold.dim_object              70,270     35,466 decayed, 416 not Earth-orbiting
gold.fact_propagatable_elset 16,342
gold.position_snapshot       16,340
```

---

## Decision 1 — the full SATCAT dump, not `GROUP=active`

**The question.** `dim_object` needs object type, owner, launch and decay
data, which the GP feed does not carry. CelesTrak offers a filtered query
(~16,000 active objects, aligned 1:1 with the fact) or a full static dump
(~70,000 objects including decayed ones, 6.7 MB).

**My initial plan was the filtered query**, on the grounds that it keeps
the dimension the same size as the fact.

**What changed it.** Comparing two GP fetches a week apart showed **8
Starlink satellites present in the first and absent from the second**. They
had re-entered. Their final element sets were still sitting in
`silver.elset`, looking perfectly valid.

A dimension filtered to currently-active objects would not contain them. So
the fact would hold eight rows whose foreign key resolved to nothing — not
because of a bug, but because the world changed between two pulls.

That is backwards for a dimension. A dimension is the complete reference
set, *including* retired members; that is what lets a historical fact still
resolve.

**Verification.** After loading the full dump, all **16,352 of 16,352**
satellites in `silver.elset` resolved against `dim_object` — zero
unresolvable. That let the relationship test run at hard `error` severity
rather than the `warn` the plan had specified, because an unresolvable key
became structurally impossible rather than merely unobserved.

**It recurred immediately.** Two days later, another GP fetch lost 4 more
satellites. Two of them (`STARLINK-1995`, `STARLINK-30195`) had decay dates
of 2026-08-14 in the refreshed SATCAT. The other two — `SKYNET 4C`,
non-operational, and `YAOGAN-39 04C`, operational — had simply left the
active feed **without decaying**. Dropping out of `GROUP=active` and
re-entering are different events, and only SATCAT distinguishes them.

**Cost.** 6.7 MB per fetch instead of 1.6 MB, and 70,270 dimension rows
instead of 16,352. Both trivial. The daily volume ledger sits under 10% of
its 80 MB budget with both feeds refreshed.

---

## Decision 2 — `is_earth_orbiting` must keep docked objects

**The question.** SGP4 is defined only for Earth orbit, but the catalogue
is not exclusively Earth-orbiting. The obvious filter is
`orbit_center = 'EA'`.

**What the data said.** `orbit_center` has 17 distinct values across 70,270
objects. Most are body codes — `SU` (Sun, 200 objects), `MO` (Moon, 102),
`MA`, `VE`, `JU`, plus Lagrange points. But **three are NORAD catalog
numbers**: `25544` (the ISS), `48274`, and `28358`.

Those denote objects docked to a host satellite. Fifteen such objects carry
element sets — and `orbit_type = 'DOC'` counts exactly fifteen, confirming
the reading.

A plain `orbit_center = 'EA'` test would have silently dropped all fifteen,
including anything docked to the ISS. The flag therefore reads:

```sql
(orbit_center = 'EA' or orbit_center ~ '^[0-9]+$') as is_earth_orbiting
```

**Verified after the fact:** `dim_object` reports 416 objects as not
Earth-orbiting, and 15 with a numeric orbit centre retained. 416 + 15 = 431
= the count of non-`EA` values. The arithmetic closes.

**A known simplification.** This assumes any numeric orbit centre denotes
an Earth-orbiting host. True for all three today, but not *resolved* — a
rigorous version would look the host up recursively. Judged
over-engineering for three values.

**Today it excludes nothing from the fact.** CelesTrak's GP feed only
publishes Earth-orbiting objects, so the filter removes zero rows. It
exists so that the day someone ingests a different group, SGP4 is not
silently handed a heliocentric probe.

---

## Decision 3 — `orbit_regime`, because epoch age is not interpretable alone

**The question.** Propagation accuracy degrades with the age of the element
set, so the map needs to express confidence. The obvious approach is a
staleness threshold: flag anything older than N hours.

**What the data said.** Twenty-five satellites held element sets older than
a week. Investigating them split the group cleanly in two:

- **8 decayed Starlinks** (`ops_status_code = 'D'`) — physically gone.
- **17 healthy satellites** (`+`, no decay date) — mostly **Galileo**, plus
  Yaogan, Arktika-M, THEMIS, BeiDou and Navstar.

The second group is not stale because anything is wrong. They are stale
because **high orbits are boringly predictable**: negligible atmospheric
drag means operators republish far less often. Splitting epoch age by
orbital regime made this quantitative:

| Regime | Satellites | Avg epoch age | Older than 48 h |
|---|---|---|---|
| LEO | 15,530 | 16.5 h | 74 (**0.5%**) |
| GEO/HEO | 628 | 23.5 h | 40 (6.4%) |
| MEO | 185 | 43.7 h | 38 (**21%**) |

A MEO satellite is roughly **40× more likely** than a LEO one to carry an
element set older than 48 hours. A flat threshold would have flagged a
fifth of the Galileo constellation as low-confidence while nothing was
wrong with it.

**Consequence.** Two independent mechanisms rather than one:

- `is_decayed` — a hard **correctness** gate. A re-entered object has no
  position to plot, however fresh its last element set.
- `epoch_age_hours` + `orbit_regime` — a **confidence** signal, to be
  interpreted against regime rather than a fixed number.

Conflating them would have been wrong in both directions: excluding healthy
MEO satellites, and propagating objects that had burned up.

---

## Decision 4 — deduplication belongs in silver, not gold

**Raised in review**, not in the plan. My first implementation put the
`row_number()` deduplication inside `gold.dim_object` and justified it as a
"business decision". That was a stretch: picking the newest row per key is
conforming work, and conforming belongs in silver.

**The fix** was `silver.space_object`, which does the dedup and nothing
else, leaving `dim_object` a flat select plus three derived flags. The
structural tell that it landed correctly: `dim_object.sql` now has no CTE,
no window function and no `where rank = 1` — only expressions that
*interpret* rows rather than reshape them.

It also restored symmetry between the two feeds:

```
bronze.raw_gp     → stg_celestrak_gp     → silver.elset        → gold facts
bronze.raw_satcat → stg_celestrak_satcat → silver.space_object → gold.dim_object
```

**An instructive asymmetry survived.** The two silver models use the same
window function with opposite `order by`:

- `elset` keeps the **earliest** observation. CelesTrak never revises an
  element set; it publishes a new one with a new epoch, so first-seen is
  the honest lineage answer.
- `space_object` keeps the **latest**. CelesTrak *does* revise a SATCAT row
  in place — `ops_status_code` flips `+` to `D`, `decay_date` appears — so
  the newest landing is authoritative.

That difference also settles the snapshot question in opposite directions.
`elset` argues against `dbt snapshot` because its source is immutable:
history *is* the data. SATCAT is mutable and overwriting, so its history
genuinely would be lost — which makes `dbt snapshot` the **correct** tool
there, should object history ever be wanted. Same reasoning, opposite
conclusion, because the sources differ.

---

## Decision 5 — `epoch_age_hours` must be signed

**What the data said.** Three objects publish element sets with epochs in
the **future** — up to two days ahead:

| Object | Apogee | Perigee |
|---|---|---|
| XMM-Newton | 91,742 km | 29,382 km |
| Chandra (CXO) | 136,946 km | 11,873 km |
| Cluster II-FM7 | *(not published)* | |

All three are highly eccentric deep-space observatories, where elements are
routinely anchored at a *predicted* perigee passage rather than an observed
one.

A `CHECK (epoch_age_hours >= 0)` would have rejected real, valid data. The
column is signed and deliberately unconstrained, and
`test_epoch_age_is_negative_for_a_future_epoch` pins it down. The live
snapshot confirms it: minimum epoch age **−32.0 hours**.

---

## The coordinate transformation

The one genuinely non-obvious piece of mathematics in the project:
converting SGP4's TEME output into WGS84 latitude, longitude and altitude.
Implemented by hand, against tests written first.

**Why tests first mattered here more than usual.** Every bug this
conversion can have produces a plausible position *somewhere real*. A
reversed rotation sign, a degrees/radians slip, geocentric instead of
geodetic latitude — none of them produce an obviously broken map. "It looks
about right" is not evidence, so the oracle had to exist before the code.

**Three independent oracles:**

1. **GMST against `sgp4.propagation.gstime`** — a separate implementation
   of the same IAU 1982 polynomial, shipping with a dependency the project
   already had. Agreement across a century of dates exercises the quadratic
   and cubic terms that the J2000 anchor cannot, since `T = 0` there.
2. **Analytically exact fixed points** — equator, both poles, 90°E, a known
   altitude. Exact by construction rather than by reference data.
3. **A round trip** through the closed-form *forward* transform,
   implemented in the test file as the reference, over points spanning LEO
   to geostationary.

**The most valuable single test** is
`test_geodetic_latitude_is_not_geocentric_latitude`. Geodetic latitude is
measured from the ellipsoid normal, which does not point at the centre of
the Earth. Geocentric latitude — `atan2(z, p)` — is the intuitive answer,
is simpler to write, and is wrong by up to 0.19°, about **21 km on the
ground**, peaking near 45°. Far too small to look wrong on a world map, far
too large to accept.

**A bug the tests caught, and what it taught.** The altitude formula
`p/cos(lat) − N` is singular at the poles. The first fix replaced it with
`z/sin(lat) − N(1−e²)`, which is singular at the *equator* — swapping one
degeneracy for its mirror image, and producing `NaN` for every equatorial
satellite. Neither formula is wrong; both are exact, with complementary
blind spots. The correct answer is to **branch on the larger denominator**,
which is `|cos| > |sin|` below 45° and the reverse above. That guarantees a
denominator of at least 1/√2 at every latitude, with no epsilon to tune.

**A test tolerance that was wrong, and it was mine.** I originally asserted
the round trip to 1e-9 degrees — a 0.1 mm tolerance. Bowring's closed form
is exact on the ellipsoid surface but drifts with altitude: measured at
1.2 mm at 400 km and 3.6 cm at GPS height. No closed-form method meets 0.1
mm at 20,000 km, and nothing here needs it, since SGP4's own error is
kilometres. Loosened to 1e-6 degrees (~11 cm), with the fixed points still
asserting exact agreement on the surface, where the method has no
approximation error to hide behind.

**Documented approximations**, both dominated by SGP4's own 1–3 km/day
drift from epoch:

- UT1 taken as UTC — up to 0.9 s, so ~0.4 km of Earth rotation.
- Polar motion skipped — tens of metres. What the code calls ECEF is
  strictly PEF.

**A trap worth recording.** SGP4's gravity model is WGS72 — that is part of
the theory, and what `sgp4.omm.initialize` uses. The *ellipsoid* for the
geodetic conversion is WGS84. Unifying them "for consistency" costs a few
hundred metres and looks entirely correct.

---

## Independent validation

The strongest evidence the transformation is right came from a source that
knows nothing about it.

Propagating the full catalogue produced a **maximum altitude of 136,639
km**. SATCAT independently publishes Chandra's apogee as **136,946 km**.
Those numbers come from completely separate paths — one from CelesTrak's
catalogue summary, the other from SGP4 output through a hand-written frame
conversion. Agreeing to 0.2% across 136,000 km is not something a broken
transform does.

Second check: **zero of 16,340 positions landed below the surface**, across
objects from 152 km to 136,639 km altitude. A sign error, a unit error or a
transposed axis would put a substantial fraction underground.

Third: the nearest objects to Berlin are all LEO satellites 83–141 km away
at 468–1,167 km altitude, six of eight of them Starlink — which is what a
correct answer looks like, given Starlink is roughly half the propagatable
catalogue.

---

## Performance, measured

For the full 16,342-satellite catalogue:

| Stage | Time | Share | Per satellite |
|---|---|---|---|
| Building `Satrec` objects (Python loop) | 0.248 s | 62% | 15.1 µs |
| `SatrecArray.sgp4` (vectorised C) | **0.007 s** | 1.6% | 0.40 µs |
| `teme_to_geodetic` (Python loop) | 0.148 s | 37% | 9.0 µs |

**The propagation is the cheapest stage.** Per satellite, initialisation
costs 38× more than the orbital mechanics it enables — an inversion of the
obvious intuition. The cause is a round trip I introduced:
`sgp4.omm.initialize` expects strings, so already-typed warehouse columns
are stringified and immediately parsed back, `strptime` included.

**Left as is.** 0.4 s for the whole catalogue is a non-issue for an
on-demand snapshot, and the alternative — hand-implementing the unit
conversions `initialize` performs — trades a measured non-problem for a
class of silent numerical bug.

**The trigger for revisiting is recorded**: multi-timestamp ground tracks.
`SatrecArray` takes an array of times, so stage 2 stays nearly free, but
the conversion loop would become ~1.5 M iterations, roughly 13 s. That is
when vectorising the frame conversion stops being premature.

Lesson worth keeping: **vectorisation is a property of each stage, not of
the pipeline.** Reaching for `SatrecArray` optimised the stage that was
already cheapest.

---

## Infrastructure: the PostGIS migration

`gold.position_snapshot` stores a `geography(Point, 4326)` column, which
required swapping the Postgres image — and therefore destroying the data
volume, since `sql/init/` runs only on first initialisation.

**A finding worth recording:** the official `postgis/postgis` image
publishes **amd64 only**, for both its Debian and alpine variants, and
fails to pull on arm64. The project uses `imresamu/postgis:17-3.5-alpine`,
the multi-arch build from one of the same maintainers. An amd64 CI runner
could substitute the official image unchanged — an asymmetry that would
have surfaced as "works in CI, fails on the laptop".

**The ordering mattered.** `docker pull` ran *before* `docker compose down
-v`. The pull failed on architecture; in the other order, the volume would
already have been destroyed with no working image to restore into.

**Recovery is demonstrated, not claimed.** After destroying the volume,
`sat-tracker-load` followed by `sat-tracker-transform` reproduced **every
row count exactly** — all six figures identical. That is worth more than
the PostGIS upgrade itself: it proves Postgres is genuinely derived, that
the CSV landing zone is the real source of truth, and that no model
introduced non-determinism. The `order by ingest_ts desc, source_file desc`
tiebreaker in `space_object` returning the identical answer on a cold
rebuild is a small piece of evidence for idempotency.

---

## What Step 3 changed about later steps

- **The map can express confidence honestly.** `orbit_regime` and a signed
  `epoch_age_hours` are in the serving layer, so staleness is interpretable
  rather than a raw number.
- **The relationship test is a hard error, not a warning** — a dividend of
  the full-dump decision.
- **The debris-classification stretch goal is unblocked**, with realistic
  class balance: 35,834 DEB, 27,398 PAY, 6,878 R/B, 160 UNK. `GROUP=active`
  would have yielded near-pure payloads.
- **A scheduling constraint is now known.** Refreshing GP without
  refreshing SATCAT leaves the decay gate stale — two satellites were
  caught by exactly this. Whatever DAG eventually runs this must refresh
  both.
- **Ground tracks have a known cost** (~13 s) and a known fix
  (vectorise the frame conversion), so that decision can be made on
  evidence rather than rediscovered.

---

## Open items carried forward

- Streamlit map over `position_snapshot` joined to `dim_object`.
- Airflow orchestration — deliberately last, since every stage is already a
  standalone command.
- S3 flip: point both Parquet roots at `s3://` URIs.
- UT1 from IERS earth-orientation data, removing the ~0.4 km approximation.
  Only worth doing once SGP4's own drift stops dominating.
- Object history via `dbt snapshot` on `space_object`, if ever wanted.
