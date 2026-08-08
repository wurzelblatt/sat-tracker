# Runbook

Operational how-to for running and troubleshooting the ingestion
pipeline. For design rationale, see [`architecture.md`](architecture.md).

## Setup

```bash
uv sync
```

This creates/updates `.venv` from `pyproject.toml`/`uv.lock`. Never use
`pip` directly in this project — `uv` is the only supported way to
install or manage dependencies.

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
