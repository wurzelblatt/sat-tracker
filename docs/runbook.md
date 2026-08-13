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
later you must `docker compose down -v` (destroys the data) or apply the
change by hand.

## Running ingestion

All commands are run from the project root (the directory containing
`pyproject.toml`), and write into `data/bronze/` or `data/sds/`
(created automatically if missing).

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

### Loading into the warehouse

Ingestion only lands raw files. Converting them to Parquet and loading
them into Postgres is a separate command, so an orchestrator can retry
either stage independently:

```bash
# Convert every bronze CSV to Parquet, then load into bronze.raw_gp
uv run sat-tracker-load

# Just one landing
uv run sat-tracker-load --source-file starlink_20260807T201737187277Z_35874ee5-....csv

# Load existing Parquet without re-converting the CSVs
uv run sat-tracker-load --skip-parquet
```

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

### Logging verbosity

`INFO`-level logging (including compliance-shield decisions like cache
hits) is on by default. Pass `--quiet` to drop to `WARNING`-only:

```bash
uv run sat-tracker-ingest --group starlink --format csv --quiet
```

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

Re-running the same command within 2 hours of the last successful fetch
returns the cached file and skips the HTTP request entirely (logged as
`Using cached local data (under 2 hours old)`). After 2 hours, the next
run re-fetches from CelesTrak. This is intentional — see the
[Compliance Shield](architecture.md#celestrak-compliance-shield) — do
not attempt to bypass it by deleting cache files to force a refetch
faster than every 2 hours; CelesTrak may rate-limit or ban IPs that
poll too aggressively.

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
