# sat-tracker

A satellite orbital-data ingestion pipeline. It pulls Orbit Mean-Elements
Message (OMM) data from [CelesTrak](https://celestrak.org) for individual
satellites or entire constellations (e.g. Starlink), and lands it locally
in a medallion-style bronze layer — either as raw CSV or as binary
[SDS](https://spacedatastandards.org/) FlatBuffers — ready for curation
into a silver layer.

> **Status:** early-stage capstone project. The ingestion (bronze) layer
> is implemented and tested; silver-layer curation is the next milestone.
> See [`docs/architecture.md`](docs/architecture.md) for design details
> and [`docs/runbook.md`](docs/runbook.md) for day-to-day operation.

## Features

- Fetch a single satellite by NORAD catalog number, or an entire
  CelesTrak group (e.g. `starlink`, `oneweb`, `gps-ops`).
- Two output formats: compact `OMM-CSV` (bronze) or binary `OMM`
  FlatBuffers via `spacedatastandards-org` (SDS).
- **CelesTrak Compliance Shield**: a 2-hour local cache check before any
  HTTP request, and fail-fast (no retry) on any non-200 response, to
  avoid tripping CelesTrak's abuse detection.
- Collision-free, auditable landing filenames
  (`<stem>_<ingested_at>_<ingestion_id><suffix>`) with a `.meta.json`
  sidecar per file.
- Type-safe, environment-overridable configuration via
  `pydantic-settings`.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for dependency and environment
  management (this project does not use `pip` directly)

## Quickstart

```bash
# Install dependencies into a local .venv
uv sync

# Run the test suite
uv run pytest

# Fetch a single satellite (ISS) as CSV into data/bronze/
uv run sat-tracker-ingest --norad-id 25544 --format csv

# Fetch an entire constellation as CSV into data/bronze/
uv run sat-tracker-ingest --group starlink --format csv
```

See [`docs/runbook.md`](docs/runbook.md) for the full command reference,
including the SDS FlatBuffer format and log verbosity flags.

## Project layout

```
src/sat_tracker/
├── config.py              # single pydantic-settings Settings singleton
├── cli.py                 # `sat-tracker-ingest` entry point
└── ingest/
    └── celestrak_client.py  # CelesTrak GP client (bronze layer)
tests/                     # pytest suite, mirrors src/ layout
```

## Configuration

All runtime settings live in `src/sat_tracker/config.py` and are
overridable via environment variables prefixed `SAT_TRACKER_`, or a
local `.env` file (never committed — see `.gitignore`). See
[`docs/architecture.md`](docs/architecture.md#configuration) for the
full list of settings.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — system design, data
  flow, and the compliance/reliability guarantees the pipeline makes.
- [`docs/runbook.md`](docs/runbook.md) — operational how-to: running
  ingestion, inspecting output, troubleshooting.
