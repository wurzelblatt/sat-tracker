# Runbook

Operational how-to for running and troubleshooting the ingestion
pipeline. For design rationale, see [`architecture.md`](architecture.md).

## Setup

```bash
uv sync
docker compose up -d      # Postgres warehouse
```

`uv sync` creates/updates `.venv` from `pyproject.toml`/`uv.lock`. Never
use `pip` directly in this project — `uv` is the only supported way to
install or manage dependencies.

The Postgres container publishes on **5433**, not the default 5432, so
it will not collide with a system Postgres. Check it came up healthy:

```bash
docker compose ps
docker compose exec postgres psql -U sat_tracker -d sat_tracker -c "\dn"
```

You should see the `bronze`, `silver` and `gold` schemas. They are
created by `sql/init/`, which Postgres runs **only on first
initialisation of an empty data volume** — if you change those files
later you must `docker compose down -v` (destroys the volume; see
[Rebuilding the warehouse from scratch](#rebuilding-the-warehouse-from-scratch))
or apply the change by hand.

Confirm PostGIS is present, since `gold.position_snapshot` cannot be
created without it:

```bash
docker compose exec postgres psql -U sat_tracker -d sat_tracker -c \
  "SELECT extname, extversion FROM pg_extension;"
```

You should see `postgis`. If you see only `plpgsql`, the container is
running a plain Postgres image — see
[Troubleshooting](#postgis-missing-or-the-image-will-not-pull).

## Running ingestion

All commands are run from the project root (the directory containing
`pyproject.toml`), and write into `data/bronze/`, `data/bronze_satcat/`
or `data/sds/` (created automatically if missing).

### Single satellite

```bash
# ISS, as CSV (bronze)
uv run sat-tracker-ingest --norad-id 25544 --format csv

# ISS, as an SDS FlatBuffer
uv run sat-tracker-ingest --norad-id 25544 --format sds
```

### Entire constellation / group

```bash
# All Starlink satellites, as CSV (bronze) — recommended for bulk groups,
# see the SDS group limitation below
uv run sat-tracker-ingest --group starlink --format csv
```

Any CelesTrak GP group name is valid (`starlink`, `oneweb`, `gps-ops`,
`active`, ...) — see https://celestrak.org/NORAD/elements/ for the full
list.

> **Note:** `--format sds` with `--group` only encodes the *first*
> object CelesTrak returns for that group, because the `OMM` FlatBuffer
> schema represents a single record. Use `--format csv` for bulk groups
> until batch SDS encoding is implemented.

### The object catalogue (SATCAT)

The GP feed carries orbital elements but nothing descriptive — no object
type, owner, launch date or decay date. Those come from CelesTrak's
separate SATCAT dump, which `gold.dim_object` is built from:

```bash
# Fetch the full catalogue (~70,000 objects, 6.7 MB)
uv run sat-tracker-satcat

# Fetch and load in one shot
uv run sat-tracker-satcat --load
```

This is the **full** catalogue including decayed objects, not a filter of
currently-active ones — see `architecture.md` for why. It lands in
`data/bronze_satcat/`, separate from the GP landings.

Its cache window is **24 hours**, not the GP feed's 2, because CelesTrak
rebuilds the file about once a day. Re-running inside that window returns
the cached file and makes no request.

**Refresh SATCAT whenever you refresh GP.** The decay gate in
`gold.dim_object` is only as fresh as the SATCAT pull behind it: a GP fetch
alone leaves recently re-entered objects looking alive, and they will be
propagated. Two satellites were caught by exactly this during testing.

### Loading into the warehouse

Ingestion only lands raw files. Converting them to Parquet and loading
them into Postgres is a separate command, so an orchestrator can retry
either stage independently:

```bash
# Convert every landing of every feed, then load into its bronze table
uv run sat-tracker-load

# Only one feed
uv run sat-tracker-load --dataset satcat

# Just one landing
uv run sat-tracker-load --source-file starlink_20260807T201737187277Z_35874ee5-....csv

# Load existing Parquet without re-converting the CSVs
uv run sat-tracker-load --skip-parquet
```

With no `--dataset`, the command iterates every feed, resolving each one's
landing directory from its descriptor. That is why the two feeds land in
separate directories: filenames carry no schema information, so directory
is what tells a SATCAT landing from a GP one.

Or fetch and load in one shot:

```bash
uv run sat-tracker-ingest --group starlink --format csv --load
```

Both loading paths are **idempotent**: `bronze.raw_gp`'s primary key is
`(source, source_file, norad_cat_id)` and the insert uses
`ON CONFLICT DO NOTHING`, so re-running reports `Loaded 0 new rows`
rather than duplicating. That is what makes an Airflow retry safe.

Query what landed:

```bash
docker compose exec postgres psql -U sat_tracker -d sat_tracker -c \
  "SELECT count(*), count(DISTINCT norad_cat_id) FROM bronze.raw_gp;"
```

### Building the silver and gold layers (dbt)

Transformation is a third separate stage:

```bash
# Build every model and run its tests
uv run sat-tracker-transform

# Just one model and its tests
uv run sat-tracker-transform --select elset
uv run sat-tracker-transform --select dim_object

# A model and everything downstream of it
uv run sat-tracker-transform --select stg_celestrak_satcat+

# Tests only, without rebuilding
uv run sat-tracker-transform --command test
```

A full build runs 6 models and 54 tests in about 4 seconds. Expect
`PASS=54 WARN=0 ERROR=0`; anything else means a model produced data its own
tests reject.

`build` (the default) runs each model **and its tests together**, so a
model that produces bad data fails immediately rather than being tested
as an afterthought. The command exits with dbt's own exit code, so a
failed test fails the task — which is what makes it safe for Airflow to
call later.

First run on a fresh clone needs the dbt packages:

```bash
uv run sat-tracker-transform --command deps
```

To run dbt directly (for `dbt docs`, `dbt compile`, etc.), pass both
directory flags, since the profile lives in-repo rather than `~/.dbt`:

```bash
uv run dbt build --project-dir transform --profiles-dir transform
```

Inspect the result:

```bash
docker compose exec postgres psql -U sat_tracker -d sat_tracker -c \
  "SELECT count(*), count(*) FILTER (WHERE is_current) FROM silver.elset;"
```

### Propagating positions

The final stage: run SGP4 over every current element set, convert TEME to
WGS84, and replace `gold.position_snapshot`.

```bash
# Propagate to now
uv run sat-tracker-propagate

# Compute and summarise without writing
uv run sat-tracker-propagate --dry-run

# A specific instant
uv run sat-tracker-propagate --at 2026-08-16T12:00:00+00:00

# A quick partial run (still replaces the whole snapshot)
uv run sat-tracker-propagate --limit 100
```

Expect roughly:

```
Propagated 16340 satellites to 2026-08-16T20:49:03Z
SGP4 declined 2; they are omitted from the snapshot.
Wrote 16340 rows to gold.position_snapshot
```

The table holds **exactly one snapshot**. Each run truncates and refills it
inside a single transaction, so a reader mid-write blocks briefly rather
than seeing an empty table, and a failed write leaves the previous snapshot
intact.

An `--at` value with no UTC offset is read as UTC, and the command says so.

Check what landed, including the spatial index the map will use:

```bash
docker compose exec postgres psql -U sat_tracker -d sat_tracker -c "
SELECT count(*), round(min(altitude_km)::numeric,1) AS min_alt,
       round(max(altitude_km)::numeric,1) AS max_alt
FROM gold.position_snapshot;"

# Nearest satellites to Berlin — exercises the GIST index end to end
docker compose exec postgres psql -U sat_tracker -d sat_tracker -c "
SELECT p.norad_cat_id, d.object_name, d.orbit_regime,
       round((ST_Distance(p.geo_point,
             ST_SetSRID(ST_MakePoint(13.40, 52.52), 4326)::geography)/1000)::numeric, 0) AS km_away,
       round(p.altitude_km::numeric, 0) AS alt_km
FROM gold.position_snapshot p
JOIN gold.dim_object d USING (norad_cat_id)
ORDER BY p.geo_point <-> ST_SetSRID(ST_MakePoint(13.40, 52.52), 4326)::geography
LIMIT 10;"
```

The nearest objects should be LEO satellites a few hundred km up. Anything
else — geostationary objects at the top, or coordinates over the Arabian
Sea — points at a coordinate bug rather than an unusual sky.

### Logging verbosity

`INFO`-level logging (including compliance-shield decisions like cache
hits) is on by default. Pass `--quiet` to drop to `WARNING`-only:

```bash
uv run sat-tracker-ingest --group starlink --format csv --quiet
```

Every command accepts it.

## Rebuilding the warehouse from scratch

Needed whenever `sql/init/*.sql` or the Postgres image changes, since those
scripts run only on an empty volume.

**Postgres is derived, not authoritative.** The landing CSVs in
`data/bronze/` and `data/bronze_satcat/` are the source of truth and live
outside the Docker volume, so destroying it loses nothing that cannot be
rebuilt in about a minute.

```bash
# 1. FIRST — confirm the image pulls, before destroying anything
docker pull imresamu/postgis:17-3.5-alpine

# 2. Destroy the volume and recreate the container
docker compose down -v
docker compose up -d

# 3. Check the init scripts actually ran
docker compose logs postgres | grep -iE "error|fatal"

# 4. Rebuild from the landing zone
uv run sat-tracker-load
uv run sat-tracker-transform
uv run sat-tracker-propagate
```

**Step 1 is not optional.** A failed pull after `down -v` leaves you with
an empty volume and no image to restore into. This is not hypothetical —
the official `postgis/postgis` image publishes amd64 only and fails to pull
on arm64.

**Step 3 catches silent failures.** A rejected init script still leaves the
container reporting healthy; the error appears only in the logs. Empty
output means everything ran.

Verify the rebuild reproduced the warehouse exactly:

```bash
docker compose exec postgres psql -U sat_tracker -d sat_tracker -c "
SELECT (SELECT count(*) FROM bronze.raw_gp)                AS raw_gp,
       (SELECT count(*) FROM bronze.raw_satcat)            AS raw_satcat,
       (SELECT count(*) FROM silver.elset)                 AS elset,
       (SELECT count(*) FROM gold.dim_object)              AS dim_object,
       (SELECT count(*) FROM gold.fact_propagatable_elset) AS propagatable;"
```

Every count should match what you had before. They are pure functions of
the landing CSVs, so a difference means either a landing file went missing
or a model stopped being deterministic.

## Inspecting output

Each ingestion writes two files per target: the payload and a
`.meta.json` audit sidecar.

```bash
ls data/bronze/
# starlink_20260808T120000123456Z_3f9c1e2a-....csv
# starlink_20260808T120000123456Z_3f9c1e2a-....csv.meta.json

cat data/bronze/starlink_*.meta.json
# {
#   "ingested_at": "2026-08-08T12:00:00.123456+00:00",
#   "ingestion_id": "3f9c1e2a-..."
# }
```

### Quick sanity check with pandas

```bash
uv run --with pandas python -c "
import pandas as pd
from pathlib import Path

latest = max(Path('data/bronze').glob('starlink_*.csv'), key=lambda p: p.stat().st_mtime)
df = pd.read_csv(latest)
print(df.shape)
print(df.head())
"
```

`uv run --with pandas` uses an ephemeral overlay environment — it does
not modify `pyproject.toml`, `uv.lock`, or the project's `.venv`. If you
want `pandas` available persistently (e.g. for repeated notebook use),
add it to the dev dependency group yourself:

```bash
uv add --dev pandas ipykernel
```

## Repeated ingestion / caching behavior

Re-running the same command within the feed's cache window returns the
cached file and skips the HTTP request entirely (logged as
`Using cached local data (under N hours old)`). After that, the next run
re-fetches from CelesTrak.

| Feed | Window | Why |
|---|---|---|
| GP (`sat-tracker-ingest`) | 2 hours | CelesTrak recomputes GP data on roughly that cycle |
| SATCAT (`sat-tracker-satcat`) | 24 hours | The dump is rebuilt about once a day, per its `Last-Modified` header |

Even past the window, a stale landing carrying an `ETag` is re-sent as
`If-None-Match`, so an unchanged payload costs a `304` and no bytes.

This is intentional — see the
[Compliance Shield](architecture.md#celestrak-compliance-shield) — do
not attempt to bypass it by deleting cache files to force a refetch
faster than the window allows; CelesTrak may rate-limit or ban IPs that
poll too aggressively.

Check the day's download budget at any time:

```bash
cat data/state/download_volume.json
```

The budget is 80 MB per UTC day, against CelesTrak's 100 MB firewall
threshold. A full `active` fetch is ~2.5 MB and a SATCAT dump ~6.7 MB, so
ordinary use sits well under 10%.

## Troubleshooting

### `CelesTrakFatalError: CelesTrak returned fatal status ...`

CelesTrak responded with a non-200 status (commonly `403`/`404` for a
bad/unknown NORAD ID or group name, or `5xx` if CelesTrak itself is
down). The pipeline does **not** retry automatically — check the target
ID/group name, wait, and re-run manually. Do not loop/retry this
yourself in a script; that's the exact behavior the compliance shield
exists to prevent.

### `ValueError: No OMM data found for ...`

CelesTrak responded `200` but returned an empty result (e.g. an invalid
NORAD ID that CelesTrak doesn't reject outright). Double-check the
NORAD ID or group name.

### `CelesTrakVolumeBudgetExceeded`

The pipeline has already downloaded `settings.daily_volume_budget_bytes`
(default 80 MB) from CelesTrak today and has stopped **before** making a
request, to stay clear of CelesTrak's 100 MB/day firewall block. The
ledger is `data/state/download_volume.json` and resets at UTC midnight.

Do not delete the ledger to get around this. If you legitimately need a
larger budget, raise `SAT_TRACKER_DAILY_VOLUME_BUDGET_BYTES` — but the
100 MB ceiling is CelesTrak's, not ours, and exceeding it gets your IP
blocked.

### `MissingSidecarError`

A landed `.csv` has no readable `.meta.json` sidecar, so its rows have
no ingestion timestamp or ID and cannot be partitioned or traced. This
usually means the file was created by hand rather than by the ingest
command. Re-fetch it rather than fabricating a sidecar.

### PostGIS missing, or the image will not pull

```
Error response from daemon: no matching manifest for linux/arm64/v8
```

The official `postgis/postgis` repository publishes **amd64 only**, for
both its Debian and its alpine variants. `docker-compose.yml` therefore
uses `imresamu/postgis:17-3.5-alpine`, the multi-arch build from one of the
same maintainers. An amd64 CI runner can substitute the official image with
no other change.

If `SELECT extname FROM pg_extension` shows only `plpgsql`, the container
is running a non-PostGIS image, or `sql/init/00_extensions.sql` never ran
because the volume was not empty. Check the logs, then rebuild from
scratch — see
[Rebuilding the warehouse from scratch](#rebuilding-the-warehouse-from-scratch).

### `SGP4 declined N of M satellites`

A warning, not an error, and a normal one at small N. SGP4 reports failures
as a per-satellite error code rather than raising, and those satellites are
dropped from the snapshot because the position array still holds numbers
for them and those numbers are meaningless.

Common causes: an orbit that has decayed below the surface during
propagation, or an eccentricity driven out of range by an element set far
past its epoch. Two declined out of ~16,000 is routine.

**A sudden jump is a data-quality signal**, not a propagation bug. Check
how stale the element sets are:

```bash
docker compose exec postgres psql -U sat_tracker -d sat_tracker -c "
SELECT count(*) FILTER (WHERE epoch < now() - interval '7 days') AS over_a_week,
       round(avg(EXTRACT(EPOCH FROM (now() - epoch))/3600)::numeric, 1) AS avg_age_h
FROM gold.fact_propagatable_elset;"
```

If the average age has grown into days, the fix is a fresh
`sat-tracker-ingest --group active`, not a code change.

### Positions look wrong on a map

If the propagation succeeds but positions land in the wrong place, the
suspects in order of likelihood:

| Symptom | Likely cause |
|---|---|
| Everything mirrored east/west | Rotation sign in `teme_to_ecef` |
| Latitude and longitude transposed | `ST_MakePoint` argument order — it takes X (longitude) first |
| Consistent ~21 km northward offset | Geocentric instead of geodetic latitude |
| Drifts wrong by ~1°/day | GMST polynomial's linear term |

Each of these has a dedicated test in `tests/test_frames.py` or
`tests/test_snapshot_writer.py`, so run the suite before reaching for a
debugger:

```bash
uv run pytest tests/test_frames.py tests/test_snapshot_writer.py -q
```

### Postgres connection refused

Check the container is up (`docker compose ps`) and that nothing else
holds port 5433. The DSN lives in `settings.postgres_dsn` and is
overridable with `SAT_TRACKER_POSTGRES_DSN`.

### Tests

```bash
uv run pytest            # full suite
uv run pytest -v         # verbose
uv run pytest tests/test_celestrak.py::test_fetch_omm_data_not_implemented
```

### Lint

```bash
uv run ruff check .
```

## Development environment notes

- `.env` (if/when one is needed for future settings) must never be
  committed — it's gitignored. Use `SAT_TRACKER_<FIELD>` environment
  variables or a local `.env` for anything environment-specific.
- `.vscode/` and `.claude/settings.local.json` are gitignored — they
  contain machine-local paths/permissions and are not meant to be
  shared across contributors.
