# Architecture

## Overview

`sat-tracker` ingests two CelesTrak feeds — orbital element sets (OMM/GP)
and the object catalogue (SATCAT) — lands both in a local **bronze** layer
byte-for-byte alongside structured audit metadata, and stores them in
partitioned Parquet datasets loaded idempotently into Postgres. dbt builds
a **silver** layer (deduplicated, SCD-2 historised element sets, plus
current object state) and a **gold** layer of serving models. SGP4 then
propagates the current element sets into `gold.position_snapshot`, a
PostGIS table recording where every tracked object is at a given instant.

The medallion architecture is complete end to end. What remains is
orchestration and presentation, not pipeline.

```
CelesTrak GP endpoint                    CelesTrak SATCAT dump
  gp.php?GROUP=|CATNR=                     /pub/satcat.csv
        │                                        │
        └───────────────┬────────────────────────┘
                        ▼
             CelesTrak Compliance Shield
   ├─ identifying User-Agent
   ├─ daily volume budget (halt before the request)
   ├─ cache check (2h for GP, 24h for SATCAT)
   ├─ conditional request (ETag → If-None-Match → 304 reuses cache)
   └─ fail-fast status gate (only 200/304; never retry anything else)
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   data/bronze/    data/sds/    data/bronze_satcat/   ← raw, byte-for-byte
     *.csv          *.sds            *.csv             + .meta.json sidecars
        │                               │
        ▼  write_bronze_parquet(dataset=GP | SATCAT)
   bronze_parquet/            bronze_satcat_parquet/  ← ingest_date=/hour=
        │                               │
        ▼  load_bronze_to_postgres(dataset=...)
   bronze.raw_gp                 bronze.raw_satcat    ← idempotent load
        │                               │
        ▼  dbt: stg_celestrak_gp        ▼  dbt: stg_celestrak_satcat
        ▼  dbt: silver.elset            ▼  dbt: silver.space_object
        │       (dedup + SCD-2)         │       (dedup to current)
        └───────────────┬───────────────┘
                        ▼  dbt: gold
        gold.dim_object  ◀── gold.fact_propagatable_elset
                        │
                        ▼  sat-tracker-propagate (SGP4 + TEME→WGS84)
              gold.position_snapshot                  ← PostGIS, GIST indexed
```

## Components

### `sat_tracker.config.Settings`

A single `pydantic-settings` `BaseSettings` subclass, exposed as the
module-level singleton `settings`. This is the **only** place
environment-specific values (URLs, paths, format defaults) may live —
client code must never hardcode them.

| Field           | Default                                        | Purpose                                   |
|-----------------|-------------------------------------------------|--------------------------------------------|
| `app_name`      | `"sat-tracker"`                                 | Logging/diagnostics label                  |
| `debug`         | `False`                                         | Reserved for verbose/less-strict behavior  |
| `celestrak_url` | `https://celestrak.org/NORAD/elements/gp.php`   | CelesTrak GP endpoint base URL             |
| `celestrak_satcat_url` | `https://celestrak.org/pub/satcat.csv`   | Full SATCAT dump (static file, not a query) |
| `satcat_cache_ttl_hours` | `24`                                  | SATCAT cache window; the file is rebuilt daily |
| `ingest_format` | `"csv"`                                         | Default format for `ingest()`: `csv`/`sds` |
| `bronze_dir`    | `data/bronze`                                   | GP CSV landing zone                        |
| `satcat_dir`    | `data/bronze_satcat`                            | SATCAT CSV landing zone                    |
| `sds_dir`       | `data/sds`                                      | SDS FlatBuffer landing zone                |
| `parquet_root`  | `data/bronze_parquet`                           | GP Parquet dataset root                    |
| `satcat_parquet_root` | `data/bronze_satcat_parquet`              | SATCAT Parquet dataset root                |

The two feeds have **separate landing directories on purpose**. Landing
filenames carry only a stem and an ingestion ID, nothing about schema, so a
bulk load globbing a shared directory would have no way to tell a 22-column
GP landing from a 22-column SATCAT one — and would convert the wrong file
with the wrong schema, silently. Directory *is* the dispatch key.

Overridable via environment variables prefixed `SAT_TRACKER_` (e.g.
`SAT_TRACKER_INGEST_FORMAT=sds`) or a local `.env` file (gitignored —
never commit one; see [Secrets](#secrets)).

### `sat_tracker.ingest.celestrak_client`

The CelesTrak ingestion client. Two parallel flows — CSV and SDS — share
the same cache-check, HTTP-fetch, and file-write logic:

- `fetch_omm_csv(norad_id)` / `fetch_omm_csv_group(group)` — write the
  raw OMM-CSV response verbatim to `bronze_dir`.
- `fetch_omm_sds(norad_id)` / `fetch_omm_sds_group(group)` — parse the
  JSON response and encode it as a binary `OMM` FlatBuffer (via
  `spacedatastandards-org`) written to `sds_dir`.
- `ingest(norad_id)` — dispatches to the CSV or SDS flow based on
  `settings.ingest_format`.
- `fetch_satcat()` — downloads CelesTrak's full SATCAT dump (~70,000
  objects, 6.7 MB) into `satcat_dir`.

**Why the full SATCAT dump rather than `GROUP=active`.** `gold.dim_object`
is a dimension, and a dimension filtered to whatever the fact currently
contains stops being one: an object that decays *after* its last element
set was published leaves the fact with a key pointing at nothing. That is
not hypothetical — eight Starlinks re-entered between two GP fetches a week
apart and had already left the active list by the second. With the full
dump, all 16,352 satellites in `silver.elset` resolve, so the
fact-to-dimension relationship test runs at hard `error` severity rather
than as a warning.

SATCAT is fetched through the same compliance shield but with its own
**24-hour cache window**, taken from the `Last-Modified` header on the
file: CelesTrak rebuilds it about once a day, so the GP feed's 2-hour
window would only re-download identical bytes.

**Known limitation:** the `OMM` FlatBuffer schema encodes a single
satellite record. `fetch_omm_sds_group` therefore only encodes the
*first* object returned for a group — for bulk constellations, prefer
`fetch_omm_csv_group`, which has no such limitation. Extending SDS
group support (e.g. one FlatBuffer per object, or a batch container) is
open follow-up work.

### CelesTrak Compliance Shield

CelesTrak is a free public service; the pipeline is designed to be a
good citizen of it and to avoid getting the developer's IP banned:

1. **Local cache verification** (`_is_cache_fresh`): before any HTTP
   request, the landing zone is checked for an existing file for the
   same target (NORAD ID or group) less than 2 hours old
   (`_CACHE_TTL`). If found, it's returned directly and no request is
   made — logged as `"Using cached local data (under 2 hours old)"`.
2. **Fail-fast error gates** (`_get_celestrak`): `Retry(total=0)` is
   configured explicitly — any HTTP response other than `200` (redirects,
   `403`, `404`, `5xx`, ...) raises `CelesTrakFatalError` immediately.
   There is no automatic retry, because retrying against a blocking or
   rate-limiting response is what gets IPs banned.

### Auditability

Every write goes through `_write_with_metadata`, which:

- Names the file `<stem>_<ingested_at:%Y%m%dT%H%M%S%fZ>_<ingestion_id><suffix>`
  so repeated ingestions of the same target never collide or overwrite
  each other.
- Writes a `<filename>.meta.json` sidecar containing the UTC
  `ingested_at` timestamp and a UUID `ingestion_id`, without mutating
  the raw payload itself — the bronze file stays byte-for-byte what
  CelesTrak returned.

### CLI

Thin `argparse` wrappers in `src/sat_tracker/cli.py`, one command per
pipeline stage. Verbose (`INFO`-level) logging is the default; `--quiet`
drops it to `WARNING`.

| Command | Stage |
|---|---|
| `sat-tracker-ingest` | Fetch GP/OMM element sets into bronze |
| `sat-tracker-satcat` | Fetch the SATCAT catalogue into bronze |
| `sat-tracker-load` | CSV → Parquet → Postgres, dispatching per dataset |
| `sat-tracker-transform` | Run dbt: staging, silver, gold, and all tests |
| `sat-tracker-propagate` | SGP4 + TEME→WGS84 → `gold.position_snapshot` |

Keeping the stages separate is deliberate: an orchestrator should call
commands that already work standalone, so a scheduling problem can never
masquerade as a data problem — and the pipeline stays demoable with the
scheduler down.

`sat-tracker-load` iterates `ALL_DATASETS`, resolving each feed's landing
directory from its descriptor, so a SATCAT landing can never be converted
with the GP schema.

`sat-tracker-propagate` accepts `--at <ISO8601>` (default: now), `--limit`
and `--dry-run`. The library refuses timezone-naive datetimes; the CLI is
the boundary that may assume UTC, and does so out loud.

## Storage layer

### `sat_tracker.storage.datasets`

The storage layer was originally built to carry exactly one thing: GP rows,
into `bronze.raw_gp`, under one Parquet root. A second feed cannot reuse
that path — its columns differ, so writing it beneath the same dataset root
would break the GP dataset's schema the next time the root is read whole,
and its primary key differs, so the `ON CONFLICT` clause that makes loads
idempotent has to differ with it.

Table, column order, conflict key, landing directory and Parquet root
therefore **vary together**, which makes them one thing rather than five
parameters. Each feed is described once as a frozen `BronzeDataset`:

```python
GP     = BronzeDataset(name="gp",     table="bronze.raw_gp",     ...)
SATCAT = BronzeDataset(name="satcat", table="bronze.raw_satcat", ...)
```

`parquet_root_setting` and `landing_dir_setting` hold the **name** of a
`Settings` field rather than its value, resolved through the singleton at
call time. Capturing the value at import would silently ignore both the
`s3://` override and any test redirecting a root into a `tmp_path`.

`tests/test_datasets.py` asserts that each descriptor's column list matches
its `CREATE TABLE` **in order**, parsed from `sql/init/*.sql`. `COPY`
streams values positionally, so a descriptor that drifts from its DDL does
not fail loudly — it writes every value into the neighbouring column.

### `sat_tracker.storage.parquet_writer`

Reads a landed `.csv` plus its `.meta.json` sidecar and writes the rows
into a Parquet dataset partitioned `ingest_date=YYYY-MM-DD/hour=HH`,
prepending the lineage columns (`ingest_ts`, `ingestion_id`, `source`,
`source_file`, `target`).

**Every source column stays a string.** Bronze's contract is fidelity:
a value CelesTrak sends that does not parse must survive the trip and
fail loudly in a silver-layer test, rather than being coerced here.
This is not hypothetical — CelesTrak began issuing six-digit NORAD IDs
in July 2026, and type inference on a mixed catalogue is exactly the
kind of thing that silently mangles them.

A missing or malformed sidecar raises `MissingSidecarError` rather than
loading unattributable rows.

The destination is `settings.parquet_root`, resolved through
`pyarrow.fs`. A plain path writes locally; an `s3://bucket/prefix` URI
writes to S3 **with no other change anywhere in the pipeline** — which
is what keeps the S3 migration off the critical path.

### `sat_tracker.storage.postgres_loader`

`COPY`s the Parquet rows into a temp staging table, then moves them
across with `INSERT ... ON CONFLICT DO NOTHING`. `bronze.raw_gp`'s
primary key is `(source, source_file, norad_cat_id)`, so **re-running a
load is a no-op, not a duplicate** — the property that makes an Airflow
retry safe.

`COPY` is used rather than an ORM bulk insert because ~30,000 rows per
full-catalogue fetch is exactly where row-by-row inserts start to hurt,
and because it keeps SQLAlchemy out of the dependency tree entirely.

Both `write_bronze_parquet` and `load_bronze_to_postgres` take a
`dataset: BronzeDataset = GP` parameter, so every existing caller was left
unchanged when SATCAT was added.

### `sat_tracker.storage.snapshot_writer`

The only place in the project that writes to a gold table.
`write_position_snapshot(positions)` replaces `gold.position_snapshot`
wholesale with `TRUNCATE` then `COPY`, **inside one transaction**.

That transaction is load-bearing. `TRUNCATE` takes an `ACCESS EXCLUSIVE`
lock, so a reader arriving mid-write blocks until commit and then sees the
complete new snapshot; run as two statements, that reader could land in the
gap and render an empty map. PostgreSQL also makes `TRUNCATE` transactional
— if the `COPY` fails halfway, the previous snapshot survives untouched.
(MySQL forces an implicit commit here and would genuinely leave the table
empty.)

`TRUNCATE` rather than `DELETE FROM` because `DELETE` only marks rows dead
and leaves the space for `VACUUM`; replacing 16,000 rows repeatedly would
bloat both the table and its GIST index.

An **empty input is a no-op**, not a truncation. An empty result is far
more likely an upstream failure than a genuine report that no satellite
exists, and blanking the map is the worst possible response to a failed
propagation.

The column list is derived from the `Position` dataclass's fields rather
than written out, which also excludes `geo_point` by construction — it is
`GENERATED ALWAYS ... STORED`, and Postgres rejects any `COPY` supplying a
value for it.

## Warehouse

A single Postgres instance from `docker-compose.yml`, published on **5433**
to avoid clashing with a system Postgres on 5432. The medallion layers are
schemas within it: `bronze`, `silver`, `gold`.

`sql/init/*.sql` runs **once, on first initialisation of an empty volume**,
in filename order:

| Script | Creates |
|---|---|
| `00_extensions.sql` | `CREATE EXTENSION postgis` |
| `01_schemas.sql` | `bronze`, `silver`, `gold` |
| `02_bronze_raw_gp.sql` | GP landing table |
| `03_bronze_raw_satcat.sql` | SATCAT landing table |
| `04_gold_position_snapshot.sql` | Propagated positions, PostGIS |

The `00` prefix is load-bearing: `04` declares a `geography(Point, 4326)`
column that cannot resolve unless the extension already exists. Running
these scripts against a plain `postgres` image fails there, which is the
intended behaviour — a silently absent PostGIS would otherwise surface much
later as an unrecognised type.

**Image: `imresamu/postgis:17-3.5-alpine`, not the official
`postgis/postgis`.** That repo publishes **amd64 only**, for both its
Debian and its alpine variants, so it cannot run on an arm64 machine at
all. `imresamu` is the multi-arch build from one of the same maintainers,
tracking the same versions. An amd64 CI runner can substitute
`postgis/postgis:17-3.5-alpine` with no other change — worth knowing,
because that asymmetry means the official image would work in CI while
failing on an Apple Silicon laptop.

Because `sql/init/` only runs on an empty volume, changing any of these
scripts means recreating the volume. That is cheap here precisely because
Postgres is derived; see the runbook for the procedure.

## Silver layer (`transform/`, dbt)

### `stg_celestrak_gp` (view)

Casts bronze's all-`TEXT` columns to real types. Casts are deliberately
plain — a safe-cast wrapper returning `NULL` on failure would turn a
CelesTrak format change into thousands of silently-null orbital
elements, whereas a hard cast turns it into a red build.

### `silver.elset` (table)

One row per `(norad_cat_id, epoch)`, deduplicated and SCD-2 historised.

**Why no `dbt snapshot`.** Snapshots exist to reconstruct history that a
*mutable* source fails to record. This source is not mutable: CelesTrak
never revises the element set for a given `(satellite, epoch)` — it
publishes a *new* one with a *new* epoch. History is not lost and
awaiting reconstruction; **history is the data**. Every SCD-2 column is
therefore a pure function of rows already present:

```sql
valid_from = epoch
valid_to   = lead(epoch) over (partition by norad_cat_id order by epoch)
is_current = valid_to is null
```

This is not a shortcut, it is more correct. A snapshot's
`dbt_valid_from` records *when we fetched*, whereas validity is
physically defined by *when the orbit was measured* (`epoch`).
Snapshotting would conflate the two, and a missed pipeline run would
silently corrupt the intervals. The model is also fully rebuildable
from bronze at any time and always converges to the same answer.

**Why `table`, not `incremental`.** The `lead()` means a newly arriving
epoch *mutates the previous row's* `valid_to` for that satellite. That
is not an append, so incremental logic would have to reprocess each
affected satellite's tail — a classic source of silent correctness
bugs. At realistic volume (~30k satellites × ~3 epochs/day) a full
rebuild is seconds.

**Deduplication.** Every 2h fetch re-lands the whole constellation, but
CelesTrak only publishes new element sets a few times a day, so the same
`(norad_cat_id, epoch)` arrives many times. Silver keeps the earliest
`ingest_ts` — payloads are identical, and first-seen is the honest
answer to "when did this enter our system?".

### `silver.space_object` (table)

One row per catalogued object, deduplicated to current state. Cleaning
only — the derived business flags live in gold.

**Latest wins, where `elset` keeps earliest.** The two models use the same
`row_number()` window with the opposite `order by`, for opposite reasons.
CelesTrak *revises* an object's SATCAT row in place — `ops_status_code`
flips `+` to `D`, `decay_date` appears where it was NULL — so the newest
landing is authoritative. An element set is never revised; a new one is
published with a new epoch, so first-seen is the honest lineage answer.

**Why no SCD-2 here**, when `elset` has it. `elset`'s header argues against
`dbt snapshot` because that source is immutable: history *is* the data.
SATCAT is the opposite case — mutable, overwriting — which makes a snapshot
the *correct* tool if object history is ever wanted. The same reasoning
reaches the opposite conclusion. It is out of scope while `dim_object` is
Type-1 and `decay_date` already carries the transition that matters.

A model-level test asserts `ops_status_code = 'D'` agrees with a populated
`decay_date`. `gold.is_decayed` trusts `decay_date` alone, so a
disagreement would silently change which objects get propagated.

**Schema naming.** `macros/generate_schema_name.sql` overrides dbt's
default, which would concatenate the target schema with the model's
custom schema and write to `public_silver`. The medallion layer names
are part of the contract with everything downstream, so dbt must not
rename them.

### Tests

Beyond range assertions on the orbital elements, two custom singular
tests guard the SCD-2 logic specifically — the part most likely to
break silently:

- `assert_one_current_elset_per_satellite` — zero current rows means a
  broken partition; more than one means duplicate epochs survived
  deduplication.
- `assert_validity_intervals_are_contiguous` — each `valid_to` must
  equal the next `valid_from`. If this breaks, an "orbit as of time T"
  lookup either matches nothing or matches two element sets, and the
  failure surfaces downstream as a wrong satellite position rather than
  as a loud error.

### Deviations from `data-model.md`

`data-model.md` specifies `NUMERIC(10, 8)` for the orbital elements.
That permits only **two integer digits** (max `99.99999999`), and four
columns exceed it: `ra_of_asc_node`, `arg_of_pericenter` and
`mean_anomaly` all reach ~360, and `inclination` reaches 180. Verified
against real data — Postgres rejects it outright:

```
ERROR:  numeric field overflow
DETAIL:  A field with precision 10, scale 8 must round to an absolute
         value less than 10^2.
```

Angular columns therefore use `NUMERIC(12, 8)`. `object_name` is `TEXT`
rather than `VARCHAR(50)`; Postgres gains nothing from the length cap.

### Deviation from `data-model.md`

That document's bronze sketch lists `epoch_year`, `epoch_day`,
`tle_line1` and `tle_line2`. The OMM-CSV feed carries **none** of them:
it has a single ISO `EPOCH` timestamp, and TLE line pairs exist only in
the legacy TLE format that six-digit NORAD IDs are phasing out.
`bronze.raw_gp` therefore mirrors the 17 columns CelesTrak actually
sends, rather than carrying four permanently-NULL columns.

## Gold layer

### `gold.dim_object` (table)

One row per catalogued object — the descriptive dimension the map and the
facts join against. Holds the **full** catalogue (~70,000 objects), not
only the ~16,000 carrying element sets, for the reason given under SATCAT
ingest above.

Three derived flags, each earned by something observed in the data rather
than chosen up front:

| Flag | What it excludes | Evidence |
|---|---|---|
| `is_decayed` | Objects that have re-entered | 8 Starlinks decayed between two GP fetches a week apart; 2 more between fetches two days apart |
| `is_earth_orbiting` | 416 solar, lunar, planetary and Lagrange objects | SGP4 is defined only for Earth orbit |
| `orbit_regime` | *(classifies rather than excludes)* | MEO satellites are ~40× likelier than LEO ones to carry an element set older than 48 h — 21% vs 0.5% |

`is_earth_orbiting` has a subtlety worth knowing: three `orbit_center`
values are **NORAD IDs rather than body codes** (e.g. `25544`, the ISS),
meaning the object is docked to a host that is itself in Earth orbit.
Fifteen such objects carry element sets, so a plain `orbit_center = 'EA'`
test would wrongly drop all of them.

`orbit_regime` exists because epoch age is not interpretable on its own.
Negligible drag makes high orbits predictable, so operators republish far
less often; a flat staleness threshold would flag a fifth of the Galileo
constellation as low-confidence when nothing is wrong with it.

`accepted_values` on `object_type` uses `PAY`/`R/B`/`DEB`/`UNK` — the four
values CelesTrak's format page documents and the only four present across
all 70,270 rows. These differ from the literals in `data-model.md`.

### `gold.fact_propagatable_elset` (view)

One row per satellite that can *actually* be propagated right now: the
current element set, filtered to objects that have not decayed and that
orbit Earth. 16,352 satellites with current elements become **16,342**.

**Named for the filters, not for recency.** `is_current` already lives on
`silver.elset`, so `fact_latest_elset` (the name in `data-model.md`) would
describe the half this model does not add.

**A view, overriding the gold layer's table default.** It reads two tables
that are rebuilt on every run and must never serve a stale pairing of
current elements with catalogue state.

It carries the element set only. Object attributes stay one join away in
`dim_object` — copying `object_type` or `orbit_regime` into a fact is how a
fact and its dimension drift apart. Both sources carry an `inclination`,
and the model takes `elset`'s: `numeric(12, 8)` from the element set is
authoritative for propagation, while SATCAT's `numeric(6, 2)` is a rounded
catalogue summary.

### `gold.position_snapshot` (table, PostGIS)

Where each object is at a given instant. **dbt does not own this one** —
SGP4 cannot run in SQL, so rows are computed in Python and written here,
making it a second landing zone that happens to sit in the gold schema.
Its DDL therefore lives in `sql/init/`, alongside the bronze tables.

Stores **both** representations of each position: geodetic lat/lon/alt for
the map, and the raw TEME state vector as SGP4 returns it. Position is kept
alongside velocity because the two must share a frame to be useful together
— conjunction screening needs relative velocity between two objects, and
that subtraction is only meaningful in a common inertial frame.

`geo_point` is a `STORED` generated column over
`ST_MakePoint(longitude_deg, latitude_deg)` — X then Y, the classic PostGIS
trap — with a GIST index. Generated rather than written, so it can never
disagree with the coordinates it derives from; `STORED` because a GIST
index needs a value on disk.

`epoch_age_hours` is **signed and deliberately unconstrained**. Three
catalogued objects — XMM-Newton, Chandra and Cluster II-FM7 — publish
element sets with epochs up to two days in the *future*, normal practice
for highly eccentric orbits anchored at a predicted perigee passage. A
`CHECK (epoch_age_hours >= 0)` would reject valid data.

## Propagation (`sat_tracker.propagate`)

### `frames` — TEME to WGS84

SGP4 does not return a position on the Earth. It returns a vector in TEME
(True Equator, Mean Equinox), an inertial frame that does not rotate with
the planet:

```
TEME  --rotate by GMST-->  ECEF  --ellipsoid-->  lat / lon / alt
```

- `gmst_radians` — the IAU 1982 polynomial, verified against
  `sgp4.propagation.gstime` across a century and against the published
  J2000 constant, 280.46061837°.
- `teme_to_ecef` — a single Z-axis rotation. The *frame* rotates, not the
  vector, so a reversed sign gives a longitude wrong by twice GMST that
  still traces a plausible ground track.
- `ecef_to_geodetic` — Bowring's closed form. Geodetic latitude is measured
  from the ellipsoid **normal**, not from the centre of the Earth; the
  geocentric answer is the intuitive one and is wrong by up to 0.19°, about
  21 km on the ground.

Altitude branches at 45° between `p/cos(lat)` and `z/sin(lat)`. Both are
exact and singular at opposite ends — the first at the poles, the second at
the equator. Branching on the larger denominator keeps it above 1/√2 at
every latitude, so there is no epsilon to tune.

**Documented approximations.** UT1 is taken as UTC (up to 0.9 s, so ~0.4 km
of rotation) and polar motion is skipped (tens of metres). Both are
dominated by SGP4's own 1–3 km/day drift from epoch — at a typical 12-hour
epoch age the element set is already good only to about 1 km.

**WGS72 vs WGS84.** SGP4's gravity model is WGS72; that is part of the
theory and is what `sgp4.omm.initialize` uses. The *ellipsoid* for the
geodetic conversion is WGS84. Unifying them would cost a few hundred metres
and look entirely correct.

### `elements` — warehouse rows to positions

Reads `gold.fact_propagatable_elset`, builds one `Satrec` per satellite via
`sgp4.omm.initialize`, and propagates all of them in a single vectorised
`SatrecArray` call.

SGP4 reports failures as an **error code per satellite** rather than
raising. A non-zero code means the position array still holds numbers and
those numbers are meaningless, so such rows are dropped and counted — a
jump in that count is a genuine data-quality signal.

`Position` maps one for one onto `gold.position_snapshot`, so the writer is
a straight column mapping.

**Measured performance**, for the full 16,342-satellite catalogue:

| Stage | Time | Share |
|---|---|---|
| Building `Satrec` objects (Python loop) | 0.248 s | 62% |
| `SatrecArray.sgp4` (vectorised C) | 0.007 s | 1.6% |
| `teme_to_geodetic` (Python loop) | 0.148 s | 37% |

The propagation itself is optimally vectorised and is the cheapest stage;
the loops around it dominate. Left as is — 0.4 s for the catalogue is a
non-issue for an on-demand snapshot. **The trigger for revisiting** is
multi-timestamp ground tracks: `SatrecArray` handles many times in one
call, but the conversion loop would become ~1.5 M iterations, about 13 s.

## Data flow (current state)

Row counts are from a real run, and each is reproducible from the CSV
landing zone alone:

```
bronze.raw_gp        43,573   (3 landings)
bronze.raw_satcat   140,540   (2 landings)
        │
        ▼  dbt
silver.elset         43,475   16,352 satellites, 27,123 closed SCD-2 intervals
silver.space_object  70,270   one row per catalogued object
        │
        ▼  dbt
gold.dim_object      70,270   35,466 decayed, 416 not Earth-orbiting
gold.fact_propagatable_elset
                     16,342   = 16,352 current − 10 decayed
        │
        ▼  sat-tracker-propagate
gold.position_snapshot
                     16,340   = 16,342 − 2 SGP4 declined
```

`data/` and `notebooks/` are gitignored — ingested data, the Parquet
datasets, the volume ledger and ad hoc notebooks are runtime artifacts, not
source. `docker-compose.yml`, `sql/init/`, `transform/` and `reports/` are
committed.

**Postgres is derived, not authoritative.** The landing CSVs in
`data/bronze/` and `data/bronze_satcat/` are the source of truth, and they
live outside the Docker volume. This was demonstrated rather than assumed
during the PostGIS migration: `docker compose down -v` destroyed the
volume, and `sat-tracker-load` followed by `sat-tracker-transform`
reproduced every row count above exactly. See the runbook for the
procedure.

## Secrets

The project currently requires **no credentials** — CelesTrak's GP
endpoint is unauthenticated and public. `Settings` already supports a
local `.env` file for future secrets (e.g. if a paid/authenticated data
source is added later); `.env` and `.env.*` are gitignored so any future
credentials never reach version control. Only `.env.example` (a
documented, valueless template) should ever be committed.

## Testing

`tests/` mirrors the `src/` layout. Shared fixtures in
`tests/conftest.py`:

- `settings` — a fresh `Settings(_env_file=None)`, isolated from any
  developer `.env`.
- `isolated_settings` — additionally redirects `bronze_dir`, `satcat_dir`,
  `sds_dir` and `state_dir` to a pytest `tmp_path`, so tests never touch
  the real landing zones or the volume ledger.
- `mock_celestrak_response` — patches `requests.Session.get`, supporting
  `text=`, `json_body=`, and `status_code=` (default `200`).

**120 Python tests plus 54 dbt tests.** Coverage includes both formats
(single + group), the compliance shield (fresh/stale cache, fail-fast on
multiple error statuses), the metadata sidecar's structure, the dataset
descriptors, the coordinate transformation, propagation, and the snapshot
writer.

Tests that need Postgres skip when it is unreachable, so `uv run pytest`
passes on a machine with no containers running.

**The coordinate transformation is tested against three independent
oracles**, because every bug it can have produces a plausible position
somewhere real — "it looks about right" is not evidence:

1. GMST against `sgp4.propagation.gstime`, a separate implementation of the
   same polynomial that ships with a dependency the project already has.
2. Analytically exact fixed points — equator, poles, 90°E, a known altitude
   — which are exact by construction rather than by reference data.
3. A round trip through the closed-form *forward* transform, implemented in
   the test file as the reference, over points spanning LEO to
   geostationary.

The round-trip tolerance is 1e-6 degrees, bounded by Bowring's drift at
altitude (1.2 mm at 400 km, 3.6 cm at GPS height) rather than by the
implementation. The fixed points still assert exact agreement on the
surface, where the closed form has no approximation error to hide behind.

Two tests are worth singling out. `test_geodetic_latitude_is_not_geocentric`
forces the distinction the whole conversion turns on. And
`test_geo_point_is_generated_from_latitude_and_longitude` is the only check
capable of catching a reversed `ST_MakePoint`, since the DDL performs that
conversion and no Python test can see inside it.

## Open follow-up work

- **Streamlit map** over `gold.position_snapshot`, joined to `dim_object`
  for names, object type and `orbit_regime`. `epoch_age_hours` interpreted
  against regime — not against a flat threshold — is what expresses
  confidence honestly.
- **Airflow orchestration.** Deliberately last: every stage is already a
  standalone CLI command, so the DAG is thin operators over commands that
  work on their own. Note one coupling worth scheduling correctly —
  refreshing GP without refreshing SATCAT leaves the decay gate stale, and
  two satellites were caught by exactly that in testing.
- **S3 flip.** Point `SAT_TRACKER_PARQUET_ROOT` and
  `SAT_TRACKER_SATCAT_PARQUET_ROOT` at `s3://` URIs. No other change.
- **UT1 from IERS earth-orientation data**, removing the ~0.4 km
  approximation `frames` currently accepts. Only worth doing once SGP4's
  own drift stops dominating, i.e. with element sets hours rather than days
  old.
- **Vectorised frame conversion.** Not needed for single-instant snapshots;
  the trigger is multi-timestamp ground tracks, where the scalar loop would
  cost ~13 s.
- **Object history.** `silver.space_object` is Type-1. SATCAT is a mutable,
  overwriting source, so `dbt snapshot` is the correct tool here — the
  opposite conclusion to `silver.elset`, for the opposite reason.
- **SDS group support.** `fetch_omm_sds_group` encodes only the first
  object returned; the `OMM` FlatBuffer schema describes one satellite.
- **Debris classification (stretch).** `dim_object.object_type` now
  provides the labels, with realistic class balance — 35,834 DEB, 27,398
  PAY, 6,878 R/B, 160 UNK — which the `GROUP=active` alternative would not
  have.
