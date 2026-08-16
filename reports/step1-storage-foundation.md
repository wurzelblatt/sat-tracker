# Step 1: Storage Foundation

**Date:** 2026-08-12
**Status:** Complete — all 5 sub-tasks done, verified end-to-end against a live Postgres instance.

Goal for this step (from `plans/assess-pipeline-state-chart-orbital-course.md`):
land CelesTrak data all the way from HTTP fetch through to a queryable
Postgres table, under a compliance shield that won't get the pipeline
IP-banned. Definition of done: `--group active` → Parquet → Postgres →
`SELECT count(*)` returns real rows.

## Summary

| # | Sub-task | Purpose | Key files changed | Time (CEST) |
|---|---|---|---|---|
| 1 | Client compliance pass (UA, ETag/304, volume guard) | Close two policy gaps: anonymous User-Agent, and re-downloading unchanged payloads risked the 100 MB/day firewall block | `config.py`, `ingest/celestrak_client.py`, `tests/conftest.py`, `tests/test_celestrak.py` | 22:58–23:05 |
| 2 | Fix SDS group bug + `ingest()` group support | `fetch_omm_sds_group` silently dropped all but the first satellite; `ingest()` couldn't target a group at all | `ingest/celestrak_client.py`, `tests/test_celestrak.py` | 22:58–23:00 |
| 3 | Docker Compose Postgres (bronze/silver/gold schemas) | Give the medallion architecture a queryable warehouse; no PostGIS (Streamlit needs plain lat/lon) | `docker-compose.yml`, `sql/init/01_schemas.sql`, `sql/init/02_bronze_raw_gp.sql`, `config.py` | 23:00–23:03 |
| 4 | Bronze CSV → Parquet writer (partitioned) | Bridge auditable-but-slow CSV to queryable columnar storage; `pyarrow.fs` keeps a future S3 move to a one-line config change | `storage/parquet_writer.py`, `storage/__init__.py`, `tests/test_parquet_writer.py`, `tests/conftest.py` | 23:03–23:10 (written), 23:24 (tested) |
| 5 | Loader: Parquet → `bronze.raw_gp` | Close the loop to "queryable in Postgres" — the step's definition of done | `storage/postgres_loader.py`, `cli.py`, `pyproject.toml`, `tests/test_postgres_loader.py` | 23:10–23:24 |

**Result:** 43 tests passing (39 unit + 4 live-Postgres), ruff clean, 10,889 Starlink rows loaded into `bronze.raw_gp` idempotently. Not yet committed — staged for review.

---

## 1. Client compliance pass: User-Agent, ETag/304, volume guard

**Time:** ~22:58–23:05 CEST

**Purpose:** The existing CelesTrak client had two policy gaps flagged
during the pipeline assessment: requests went out with the generic
`python-requests/x.y` User-Agent (exactly what CelesTrak's usage policy
targets), and every fetch re-downloaded the full payload even when
unchanged — at ~5 MB per `active`-group fetch and up to 12 fetches/day,
that put the pipeline within the same order of magnitude as CelesTrak's
100 MB/day firewall-block threshold, with no headroom for a re-run.

**What was done:**
- Added `user_agent` and `daily_volume_budget_bytes` (default 80 MB) to
  `Settings`, plus `state_dir` for small pipeline state files.
- `_get_celestrak()` now sends an identifying `User-Agent` on every
  request and checks a daily download-volume ledger *before* making the
  request, raising `CelesTrakVolumeBudgetExceeded` (a subclass of
  `CelesTrakFatalError`) if the budget is already spent. The ledger
  resets at UTC midnight.
- Added `ETag`/`If-None-Match` support: a stale cached file's `ETag`
  (persisted in its `.meta.json` sidecar) is replayed as
  `If-None-Match` on the next fetch. A `304 Not Modified` response
  reuses the cached bytes and touches the file's mtime to reset the 2h
  cache-freshness window, instead of re-downloading an unchanged
  payload. The status gate now accepts `200` and `304` only — anything
  else still fails fast with no retry.
- `_write_with_metadata()` sidecars now also carry `source`,
  `source_file`, `target`, `bytes`, and `etag` (when present) — lineage
  fields the downstream Parquet/Postgres load depends on.

**Files changed:**
- `src/sat_tracker/config.py` — added `user_agent`, `daily_volume_budget_bytes`, `state_dir`
- `src/sat_tracker/ingest/celestrak_client.py` — UA header, volume ledger (`_read_volume_ledger`, `_record_downloaded_bytes`, `_check_volume_budget`), conditional-request handling, expanded sidecar metadata
- `tests/conftest.py` — `isolated_settings` now also redirects `state_dir`; `mock_celestrak_response` gained `etag=` support
- `tests/test_celestrak.py` — new tests: `test_request_sends_identifying_user_agent`, `test_etag_is_recorded_in_sidecar`, `test_stale_cache_replays_etag_as_conditional_request`, `test_not_modified_response_reuses_cache_without_rewriting`, `test_download_volume_is_tallied_against_daily_budget`, `test_fetch_halts_when_daily_budget_is_exhausted`, `test_volume_ledger_resets_on_a_new_utc_day`

---

## 2. Fix SDS group bug and `ingest()` group support

**Time:** ~22:58–23:00 CEST

**Purpose:** `fetch_omm_sds_group()` fetched an entire constellation but
silently encoded only the first satellite, discarding the rest — a bug
that had been documented as a "known limitation" rather than fixed.
Separately, `ingest()` only accepted a `norad_id`, even though the CLI
already supported `--group`, making the public API inconsistent.

**What was done:**
- Rewrote FlatBuffer encoding to produce a **size-prefixed stream**
  (`_build_omm_flatbuffers()`): each satellite's `OMM` FlatBuffer is
  written with `Builder.FinishSizePrefixed()` and concatenated into one
  `.sds` file. Added `read_omm_sds()` to decode the full stream back
  into a list of `OMM` readers.
  - Side effect (flagged to the user): single-satellite `.sds` files
    are now also streams (of length 1), for structural consistency
    between the single and group code paths.
- `ingest()` signature changed to keyword-only `ingest(*, norad_id=None,
  group=None)`, raising `ValueError` unless exactly one is given.

**Files changed:**
- `src/sat_tracker/ingest/celestrak_client.py` — `_build_omm_flatbuffers`, `read_omm_sds`, `ingest()` signature
- `tests/test_celestrak.py` — `test_fetch_omm_sds_group_encodes_every_record` (regression test asserting 5/5 records survive), `test_ingest_supports_groups`, `test_ingest_requires_exactly_one_target`; updated existing SDS tests to use `read_omm_sds()` instead of `OMMReader.GetRootAs()` directly

---

## 3. Docker Compose Postgres with bronze/silver/gold schemas

**Time:** ~23:00–23:03 CEST (files written); verified once the user started Docker Desktop later in the session

**Purpose:** The medallion architecture needs a queryable warehouse.
Decision (confirmed with the user): Postgres via Docker Compose, no
PostGIS (the planned Streamlit map needs plain lat/lon floats; the
`GEOGRAPHY`/GIST setup in `.claude/data-model.md` would only add setup
friction for no benefit at this stage).

**What was done:**
- `docker-compose.yml`: single `postgres:17-alpine` service, published
  on host port **5433** (not 5432, to avoid colliding with a system
  Postgres), with a healthcheck and a named volume.
- `sql/init/01_schemas.sql`: creates `bronze`, `silver`, `gold` schemas.
- `sql/init/02_bronze_raw_gp.sql`: creates `bronze.raw_gp`. Every
  CelesTrak payload column is `TEXT` — bronze's contract is fidelity,
  not usability, so a value that doesn't parse must still land rather
  than be coerced or dropped; dbt does typed casting downstream.
  Primary key is `(source, source_file, norad_cat_id)`, making reloads
  of the same file idempotent.
  - **Deviation from `.claude/data-model.md`, called out explicitly in
    the SQL comments:** that document's bronze sketch lists
    `epoch_year`, `epoch_day`, `tle_line1`, `tle_line2`. The OMM-CSV
    feed CelesTrak actually returns has none of these — a single ISO
    `EPOCH` timestamp instead, and TLE line pairs only exist in the
    legacy format that six-digit NORAD IDs are phasing out. The table
    was built to match what CelesTrak sends rather than carry four
    permanently-NULL columns.
- Added `postgres_dsn` (default pointing at the Compose instance on
  5433) and `parquet_root` to `Settings`.

**Files changed:**
- `docker-compose.yml` (new)
- `sql/init/01_schemas.sql` (new)
- `sql/init/02_bronze_raw_gp.sql` (new)
- `src/sat_tracker/config.py` — added `postgres_dsn`, `parquet_root`

**Verification (after the user started Docker Desktop):**
```
docker compose ps            → sat_tracker_postgres, healthy, 0.0.0.0:5433->5432/tcp
\dn                           → bronze, gold, public, silver
\d bronze.raw_gp              → 22 columns as designed, PK on (source, source_file, norad_cat_id)
```

---

## 4. Bronze CSV → Parquet writer, partitioned by `ingest_date`/`hour`

**Time:** ~23:03–23:10 CEST (written); tested ~23:24 CEST once `pyarrow` was installed

**Purpose:** Bronze CSV is auditable but not efficiently queryable.
Converting to partitioned Parquet is the bridge to the warehouse, and
using `pyarrow.fs` for the destination means a later move to S3 is a
URI string change (`data/...` → `s3://bucket/...`) rather than a
rewrite — keeping AWS credential/IAM debugging off the critical path
per the plan's de-risking strategy.

**What was done:**
- `write_bronze_parquet(csv_path)`: reads a landed CSV plus its
  `.meta.json` sidecar, reads the CSV with every column forced to
  `pyarrow.string()` (so six-digit NORAD IDs and exponent-notation
  BSTAR values survive untouched), prepends the lineage columns
  (`ingest_ts`, `ingestion_id`, `source`, `source_file`, `target`) plus
  the `ingest_date`/`hour` partition keys derived from the sidecar's
  `ingested_at`, and writes via
  `pyarrow.parquet.write_to_dataset(..., partition_cols=["ingest_date",
  "hour"])`.
- Output file is named after the `ingestion_id`
  (`existing_data_behavior="overwrite_or_ignore"`), so re-converting
  the same landing overwrites its own file instead of duplicating rows.
- `MissingSidecarError` raised if the sidecar is absent or malformed —
  rows with no traceable lineage are never written.

**Files changed:**
- `src/sat_tracker/storage/__init__.py` (new)
- `src/sat_tracker/storage/parquet_writer.py` (new)
- `tests/test_parquet_writer.py` (new) — 9 tests covering partitioning, row/column fidelity, string-typing of six-digit IDs and exponent values, lineage columns, re-conversion not duplicating rows, missing/malformed sidecar rejection, and partition-by-ingest-time (not wall-clock)
- `tests/conftest.py` — added shared `sample_csv_payload` / `sample_ingested_at` fixtures (extracted so `test_postgres_loader.py` could reuse them without a cross-test-module import)

**Verification (against the real, previously-migrated Starlink landing):**
```
10,889 rows written to data/bronze_parquet/ingest_date=2026-08-07/hour=20/
```

---

## 5. Loader: Parquet → `bronze.raw_gp` in Postgres

**Time:** ~23:10–23:24 CEST

**Purpose:** Close the loop to "queryable in Postgres" — the step's
definition of done.

**What was done:**
- `load_bronze_to_postgres(source_file=None)`: reads the Parquet
  dataset (optionally filtered to one `source_file`) via
  `pyarrow.dataset`, `COPY`s the rows into a `TEMP` staging table, then
  moves them into `bronze.raw_gp` with `INSERT ... ON CONFLICT (source,
  source_file, norad_cat_id) DO NOTHING`. Returns the number of rows
  actually inserted.
- `COPY` chosen over an ORM/row-by-row insert because ~30,000 rows per
  full-catalogue fetch is exactly where row-by-row starts to hurt;
  chosen over SQLAlchemy to keep the dependency tree smaller (`psycopg`
  only).
- CLI: added `sat-tracker-load` entry point (`src/sat_tracker/cli.py`
  gained a `load()` function) that runs the CSV→Parquet→Postgres chain,
  with `--source-file` and `--skip-parquet` options. `sat-tracker-ingest`
  gained a `--load` flag to fetch-and-load in one command.

**Files changed:**
- `src/sat_tracker/storage/postgres_loader.py` (new)
- `src/sat_tracker/cli.py` — added `load()` entry point, `--load` flag on `main()`, `_add_verbosity_flag`/`_configure_logging` helpers shared between both commands
- `pyproject.toml` — added `sat-tracker-load = "sat_tracker.cli:load"` script entry; `uv add pyarrow 'psycopg[binary]'` run by the user
- `tests/test_postgres_loader.py` (new) — 4 tests against a real Postgres instance (module skips cleanly if unreachable): load succeeds, reload is idempotent (0 new rows), lineage columns survive the round trip, unknown `source_file` is a safe no-op

**Verification (live, against Docker Postgres):**
```
First load:  10889 new rows inserted
Second load: 0 new rows inserted (idempotent)

SELECT count(*), count(DISTINCT norad_cat_id) FROM bronze.raw_gp;
 rows  | satellites
-------+------------
 10889 |      10889

Sample row: STARLINK-38128 | norad_cat_id=100001 | epoch=2026-08-07T07:03:44.978976
```
The six-digit NORAD IDs (`100001`, `100002`, ...) confirm the July 2026
catalog rollover mentioned in `.claude/api-references.md` is already
live in the real data, and that the all-`TEXT` bronze schema absorbed
it without any special-casing.

---

## Housekeeping done alongside the above

- Migrated the pre-existing `data/bronze/starlink.csv` (from before the
  collision-free filename scheme existed) to the current
  `<stem>_<ingested_at>_<ingestion_id><suffix>` naming, preserving its
  original `ingestion_id` and setting its file mtime back to the
  original fetch time so the cache-freshness window stays honest.
- Updated `README.md`, `docs/architecture.md`, and `docs/runbook.md` to
  document the compliance shield additions, the storage layer, the new
  CLI commands, and the `data-model.md` deviation.

## Final state at end of Step 1

- **43 tests passing** (39 unit + 4 live-Postgres integration tests),
  `ruff check .` clean.
- Not yet committed to git — left staged for the user to review first.

## Next step (not started)

Step 2 per the plan: SATCAT ingest (a separate CelesTrak endpoint;
`gold.dim_object` and the ML stretch goal both depend on it, and
neither is in the original day-by-day roadmap) + dbt scaffolding +
`silver.elset`.
