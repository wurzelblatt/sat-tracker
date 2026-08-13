# Architecture

## Overview

`sat-tracker` ingests satellite orbital-element data (OMM records) from
CelesTrak, lands it in a local **bronze** layer, preserves the raw
payload byte-for-byte alongside structured audit metadata, and stores
the data in a partitioned Parquet dataset that is loaded idempotently
into Postgres. dbt then builds a **silver** layer — deduplicated,
SCD-2 historised element sets — on top of it. This is the medallion
architecture through silver; a **gold** layer of serving models and
SGP4-propagated positions is the next planned milestone.

```
CelesTrak GP endpoint
        │
        │  HTTP GET (CATNR=<norad_id> or GROUP=<name>, FORMAT=CSV|json)
        ▼
 CelesTrak Compliance Shield
   ├─ identifying User-Agent
   ├─ daily volume budget (halt before the request)
   ├─ cache check (skip HTTP if a landing file <2h old exists)
   ├─ conditional request (ETag → If-None-Match → 304 reuses cache)
   └─ fail-fast status gate (only 200/304; never retry anything else)
        │
        ▼
   ┌────┴─────┐
   ▼          ▼
 bronze/     sds/                    ← raw landing, byte-for-byte
 *.csv       *.sds                     + .meta.json audit sidecars
        │
        ▼  write_bronze_parquet()
 bronze_parquet/                     ← columnar, ingest_date=/hour=
 ingest_date=YYYY-MM-DD/hour=HH/*.parquet
        │
        ▼  load_bronze_to_postgres()
 Postgres bronze.raw_gp              ← queryable, idempotent load
        │
        ▼  dbt: stg_celestrak_gp (view, casts TEXT → real types)
        ▼  dbt: silver.elset (table, dedup + SCD-2)
 silver.elset
        │
        ▼  dbt (next milestone)
 gold.dim_object / fact_latest_elset / position_snapshot
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
| `ingest_format` | `"csv"`                                         | Default format for `ingest()`: `csv`/`sds` |
| `bronze_dir`    | `data/bronze`                                   | CSV landing zone                           |
| `sds_dir`       | `data/sds`                                      | SDS FlatBuffer landing zone                |

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

### CLI (`sat-tracker-ingest`)

Thin `argparse` wrapper (`src/sat_tracker/cli.py`) over the client
functions. Verbose (`INFO`-level) logging is the default; `--quiet`
drops it to `WARNING`. Target selection (`--norad-id` vs `--group`) is a
required mutually-exclusive group; `--format` selects `csv`/`sds`.

## Storage layer

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

## Data flow (current state)

```
CelesTrak ──▶ data/bronze/*.csv ──▶ data/bronze_parquet/ ──▶ bronze.raw_gp
              data/sds/*.sds        (partitioned)            (Postgres)
                                                                    │
                                                                    ▼  dbt
                                                              silver.elset
```

`data/` and `notebooks/` are gitignored — ingested data, the Parquet
dataset, the volume ledger and ad hoc notebooks are runtime artifacts,
not source. `docker-compose.yml`, `sql/init/` and `transform/` are
committed.

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
- `isolated_settings` — additionally redirects `bronze_dir`/`sds_dir` to
  a pytest `tmp_path`, so tests never touch the real landing zones.
- `mock_celestrak_response` — patches `requests.Session.get`, supporting
  `text=`, `json_body=`, and `status_code=` (default `200`).

Coverage includes both formats (single + group), the compliance shield
(fresh/stale cache, fail-fast on multiple error statuses), and the
metadata sidecar's structure.

## Open follow-up work

- **SATCAT ingest.** `gold.dim_object` needs `object_type`, `country`,
  `launch_date` and `rcs_size_m2`, none of which are in the GP/OMM feed
  — they come from CelesTrak's separate `satcat.php` endpoint. The
  Payload/Rocket-Body/Debris classification stretch goal trains on
  `object_type`, so it is blocked on this.
- **dbt gold layer.** Serving models (`dim_object`, `fact_latest_elset`)
  on top of the now-implemented `silver.elset`.
- **SGP4 propagation** into `gold.position_snapshot`.
- **Airflow orchestration.** Deliberately last: every stage is already
  a standalone CLI command, so the DAG is thin operators over commands
  that work on their own. A scheduling problem then cannot masquerade
  as a data problem.
- **S3 flip.** Point `SAT_TRACKER_PARQUET_ROOT` at an `s3://` URI.
- **Streamlit map** with on-demand `SatrecArray` propagation.
