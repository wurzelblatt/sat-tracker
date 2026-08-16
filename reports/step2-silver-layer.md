# Step 2: Silver Layer (`silver.elset`)

**Date:** 2026-08-13
**Status:** Complete — `dbt build` green, 24/24 nodes passing.

Goal (from `plans/step2-silver-layer-elset-historization.md`): turn the
raw bronze landing into a normalised, deduplicated, SCD-2 historised
`silver.elset` table, built and tested with dbt.

## Summary


| #   | Sub-task                         | Purpose                                                                                                           | Key files                                                                                                                     | Time (CEST)          |
| --- | -------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| 1   | dbt project scaffolding          | Give dbt an in-repo, env-var-driven home so the project is reproducible on another machine with no `~/.dbt` setup | `transform/dbt_project.yml`, `profiles.yml`, `packages.yml`, `macros/generate_schema_name.sql`, `models/staging/_sources.yml` | 09:30–09:38 (13 Aug) |
| 2   | Staging model `stg_celestrak_gp` | Cast bronze's all-`TEXT` columns to real types; the point where bad data becomes visible                          | `transform/models/staging/stg_celestrak_gp.sql`, `_stg_models.yml`                                                            | 09:33–09:38          |
| 3   | Silver model `elset`             | Dedup on `(norad_cat_id, epoch)` + derive SCD-2 columns with window functions                                     | `transform/models/silver/elset.sql`                                                                                           | 09:35–09:38          |
| 4   | Tests incl. custom SCD-2 guards  | Range assertions plus two singular tests for the invariants most likely to break silently                         | `transform/models/silver/_silver_models.yml`, `transform/tests/*.sql`                                                         | 09:36–09:40          |
| 5   | CLI, docs, report                | `sat-tracker-transform` so Airflow later calls a standalone-working command                                       | `src/sat_tracker/cli.py`, `pyproject.toml`, `README.md`, `docs/*`, `.gitignore`                                               | 09:40–11:47          |


**Result:** `silver.elset` = 10,889 rows, 10,889 satellites, one current
element set each. `dbt build` → `PASS=24 WARN=0 ERROR=0`. pytest 43
passing, ruff clean.

---



## 1. dbt project scaffolding

**Purpose:** Stand up dbt against the existing Postgres warehouse.

**What was done:**

- `transform/dbt_project.yml` — staging materialized as `view`, silver
as `table` (reasoning in 3.).
- `transform/profiles.yml` kept **in-repo**, every value an `env_var`
with a default matching `docker-compose.yml`. A fresh clone works
with no configuration; CI or a hosted Postgres only needs env vars.
Deliberately not `~/.dbt/profiles.yml`, which would be untracked and
unreviewable.
- `transform/macros/generate_schema_name.sql` — **necessary, not
cosmetic.** dbt's default `generate_schema_name` concatenates the
target schema with the model's custom schema, so `+schema: silver`
against target `public` would have written to `public_silver`. The
medallion layer names are part of the contract with everything
downstream (and `sql/init/` already creates real `bronze`/`silver`/
`gold` schemas), so dbt must not rename them.
- `transform/packages.yml` — `dbt_utils` for `expression_is_true` and
`unique_combination_of_columns`.
- `models/staging/_sources.yml` — declares `bronze.raw_gp` as a source
with a freshness block (warn 4h / error 12h, sized against
CelesTrak's 2h update cycle).

**Dependency install** (run by the user): `uv add dbt-core dbt-postgres`
→ dbt-core 1.12.2, dbt-postgres 1.11.0. No conflicts materialised.

## 2. Staging model

**Purpose:** Bronze deliberately stores every CelesTrak column as `TEXT` so nothing is changed or coerced on the way in. Staging is where the data becomes typed, and therefore where bad data becomes visible.

**Decision for hard casts, not safe casts.** A `try_cast` inserting `NULL` on failure would fail silently and turn a format change by Celestrak for example  
into thousands void orbital elements without any error. A hard cast in turn turns would fail immediatly (Red build). This matches the fail-fast stance the ingest client  
already takes on HTTP errors.

## 3. Silver model — the substantive decisions



### Decision against `dbt snapshot` (user-approved deviation from `data-model.md`)

Snapshots exist to reconstruct history that a **mutable** source fails to
record. This source is not mutable: CelesTrak never revises the element
set for a given `(satellite, epoch)` — it publishes a *new* one with a
*new* epoch. History is not lost awaiting reconstruction; **history is the data**. So every  (valid_from, valid_to, is_current) column (SCD-2, Slowly Changing Dimensions) is a pure function of rows already present:

```sql
valid_from = epoch
valid_to   = lead(epoch) over (partition by norad_cat_id order by epoch)
is_current = valid_to is null
```

This is **more correct, not a shortcut**: a snapshot's `dbt_valid_from`
records *when we fetched*, whereas validity is physically defined by
*when the orbit was measured* (`epoch`). Snapshotting would conflate the two, and a missed pipeline run would silently corrupt the intervals. That makes the model fully rebuildable from bronze and always converging to the same answer.

lead() is a window function that looks at the next row in the view (partitioned by x order by y). If we are in the newest row then it becomes NULL and is_current becomes TRUE

Saved ~1–1.5 days against the snapshot-based estimate.

### `table`, not `incremental`

The `lead()` means a newly arriving epoch **mutates the previous row's**
`valid_to` for that satellite. That is not an append, so incremental
logic would have to reprocess each affected satellite's tail — a classic
source of silent correctness bugs. At realistic volume (~30k satellites
× ~3 epochs/day ≈ 1.3M rows after two weeks) a full rebuild is seconds.

### Deduplication

Every 2h fetch re-lands the whole constellation, but CelesTrak publishes
new element sets only a few times a day, so the same
`(norad_cat_id, epoch)` will arrive many times over. Silver keeps the
row with the earliest `ingest_ts` — payloads are identical, and
first-seen is the honest answer to "when did this enter our system?".

## 4. Tests

1. Range assertions on the orbital elements:

- eccentricity ∈ [0,1)
- inclination ∈ [0,180]
- angles (ra_of_asc_node, arg_of_pericenter, mean_anomaly) ∈ [0,360)
- mean_motion ∈ (0,20)): Low earth orbits (LEOs) of fastest stable arteficial satellites and objects are at an altitude of around 150km (close to the upper atmosphere) correspondig to around 88min/revolutions or 16,3 revolutions/24h. 17 revs/day usually mean the object is about to re-enter. So mean_motion value of 20 serves as a comfortable safety margin for plausible physical data.

`2. unique_combination_of_columns` on the declared grain:

- asserts the uniqueness/atomicity of the primary key, which is set by the data model as  (norad_cat_id, epoch) (declared grain). So this serves as a test for the deduplication

1. Two **custom singular tests** validate the SCD-2 logic specifically (this is the part most likely to break silently, because the table still *looks* fine when it's wrong):

- `assert_one_current_elset_per_satellite` — zero rows in this test means a broken `lead()` partition (no satellite epoch is_current = True); more than one means two or more  epochs survived deduplication. Either would quietly break SGP4 propagation downstream.
- `assert_validity_intervals_are_contiguous` — each `valid_to` must  
equal the next `valid_from`, and closed intervals must move forward  
in time. If this breaks, an "orbit as of time T" lookup either  
matches nothing (gap) or two element sets (overlap), surfacing as a  
wrong satellite position rather than a loud error. An offline pipeline (e.g. no new data ingested for a few days) would pass because the next valid_from interval would be joined adjacent to the previous valid_to so the test wouldn't fail.

Both were validated against real data **before dbt was installed**, by
running the equivalent SQL directly against Postgres: 0 failures each.

Also cleared 7 dbt deprecation warnings by nesting generic-test
arguments under the `arguments:` key (dbt 1.12 syntax).

## 5. Precision bug fixed (second user-approved deviation)

`.claude/data-model.md` specifies `NUMERIC(10, 8)` for the orbital
elements. That permits only **two integer digits** (max `99.99999999`).
Four columns exceed it — `ra_of_asc_node`, `arg_of_pericenter` and
`mean_anomaly` all reach ~360; `inclination` reaches 180.

Not theoretical. Proven against data already in `bronze.raw_gp`:

```
ERROR:  numeric field overflow
DETAIL:  A field with precision 10, scale 8 must round to an absolute
         value less than 10^2.
```

Angular columns now use `NUMERIC(12, 8)`. `object_name` is `TEXT` rather
than `VARCHAR(50)` — Postgres gains nothing from the length cap.

## 6. CLI and housekeeping

- `sat-tracker-transform` wraps `dbt build`, propagating dbt's exit code
so a failed test fails the task (what makes it safe for Airflow).
Supports `--select` and `--command {build,run,test,deps}`. Resolves
the project dir relative to the package, so no `~/.dbt` is needed.
- `.gitignore`: added `transform/target/`, `transform/dbt_packages/`,
`transform/logs/`, `transform/.user.yml`.
- Updated `README.md`, `docs/architecture.md`, `docs/runbook.md`.



## Verification

```
dbt build → PASS=24 WARN=0 ERROR=0 SKIP=0

SELECT count(*), count(*) FILTER (WHERE is_current), count(DISTINCT norad_cat_id)
FROM silver.elset;
 rows  | current_rows | satellites
-------+--------------+------------
 10889 |        10889 |      10889

\dt silver.*  →  silver | elset | table    (NOT public_silver — macro works)

pytest: 43 passed · ruff: All checks passed
```

All 10,889 rows are `is_current` because there is still only one
ingestion. The SCD-2 chaining logic is exercised but not yet
*interesting* — it becomes so once a second fetch brings newer epochs.
Worth re-running `sat-tracker-transform` after the next scheduled fetch
to see `valid_to` populate.

## Next step

Step 3: SATCAT ingest (`satcat.php` — a second CelesTrak endpoint) +
gold layer (`dim_object`, `fact_latest_elset`) + SGP4 propagation into
`position_snapshot`. SATCAT is on the critical path for both
`dim_object` and the ML stretch goal, and is not in the original
day-by-day roadmap.