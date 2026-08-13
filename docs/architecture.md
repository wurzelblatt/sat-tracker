# Architecture

## Overview

`sat-tracker` ingests satellite orbital-element data (OMM records) from
CelesTrak, lands it in a local **bronze** layer, preserves the raw
payload byte-for-byte alongside structured audit metadata, and stores
the data in a partitioned Parquet dataset that is loaded idempotently
into Postgres. This is the first stage of a medallion architecture; a
silver layer (cleaned, deduplicated, schema-conformed data suitable for
analysis) is the next planned milestone.

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

## Data flow (current state)

```
CelesTrak ──▶ data/bronze/*.csv ──▶ data/bronze_parquet/ ──▶ bronze.raw_gp
              data/sds/*.sds        (partitioned)            (Postgres)
```

`data/` and `notebooks/` are gitignored — ingested data, the Parquet
dataset, the volume ledger and ad hoc notebooks are runtime artifacts,
not source. `docker-compose.yml` and `sql/init/` are committed.

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
- **dbt silver/gold.** `silver.elset` (dedup + SCD-2) and the gold
  serving models.
- **SGP4 propagation** into `gold.position_snapshot`.
- **Airflow orchestration.** Deliberately last: every stage is already
  a standalone CLI command, so the DAG is thin operators over commands
  that work on their own. A scheduling problem then cannot masquerade
  as a data problem.
- **S3 flip.** Point `SAT_TRACKER_PARQUET_ROOT` at an `s3://` URI.
- **Streamlit map** with on-demand `SatrecArray` propagation.
