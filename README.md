# sat-tracker

A satellite orbital-data ingestion pipeline. It pulls Orbit Mean-Elements
Message (OMM) data from [CelesTrak](https://celestrak.org) for individual
satellites or entire constellations (e.g. Starlink), and lands it locally
in a medallion-style bronze layer — either as raw CSV or as binary
[SDS](https://spacedatastandards.org/) FlatBuffers — ready for curation
into a silver layer.

> **Status:** capstone project in progress. Ingestion, the bronze layer
> and the dbt-built silver layer (`silver.elset`, deduplicated and SCD-2
> historised) are implemented and tested; the gold layer and SGP4
> propagation are the next milestone. See
> [`docs/architecture.md`](docs/architecture.md) for design details and
> [`docs/runbook.md`](docs/runbook.md) for day-to-day operation.

## Features

- Fetch a single satellite by NORAD catalog number, or an entire
  CelesTrak group (e.g. `starlink`, `oneweb`, `gps-ops`).
- Two output formats: compact `OMM-CSV` (bronze) or binary `OMM`
  FlatBuffers via `spacedatastandards-org` (SDS).
- **CelesTrak Compliance Shield** — an identifying User-Agent, a daily
  download budget enforced before the request, a 2-hour local cache,
  `ETag`/`If-None-Match` conditional requests, and fail-fast on any
  status other than `200`/`304` with no retry. CelesTrak firewall-blocks
  IPs that exceed 100 MB/day or hammer a blocking response.
- Collision-free, auditable landing filenames
  (`<stem>_<ingested_at>_<ingestion_id><suffix>`) with a `.meta.json`
  sidecar per file.
- Bronze Parquet dataset partitioned `ingest_date=/hour=`, loaded into
  Postgres **idempotently** — re-running a load never duplicates rows.
- dbt-built `silver.elset`: deduplicated, SCD-2 historised element sets
  where validity is derived from `epoch` (when the orbit was *measured*)
  rather than ingestion time, guarded by custom tests for interval
  contiguity and exactly-one-current-row-per-satellite.
- Type-safe, environment-overridable configuration via
  `pydantic-settings`.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for dependency and environment
  management (this project does not use `pip` directly)
- Docker, for the local Postgres warehouse

## Quickstart

```bash
# Install dependencies into a local .venv
uv sync

# Start the Postgres warehouse (published on 5433, not 5432)
docker compose up -d

# Run the test suite
uv run pytest

# Fetch a constellation and load it all the way into Postgres
uv run sat-tracker-ingest --group starlink --format csv --load

# Build the silver layer and run its tests
uv run sat-tracker-transform --command deps   # first run only
uv run sat-tracker-transform

# Query what landed
docker compose exec postgres psql -U sat_tracker -d sat_tracker -c \
  "SELECT count(*), count(*) FILTER (WHERE is_current) FROM silver.elset;"
```

See [`docs/runbook.md`](docs/runbook.md) for the full command reference,
including loading stages separately, the SDS FlatBuffer format, and
troubleshooting.

## Project layout

```
src/sat_tracker/
├── config.py                 # single pydantic-settings Settings singleton
├── cli.py                    # ingest / load / transform entry points
├── ingest/
│   └── celestrak_client.py   # CelesTrak GP client + compliance shield
└── storage/
    ├── parquet_writer.py     # bronze CSV → partitioned Parquet
    └── postgres_loader.py    # Parquet → bronze.raw_gp (idempotent)
transform/                    # dbt project (in-repo profiles.yml)
├── models/staging/           # stg_celestrak_gp — casts bronze TEXT → types
├── models/silver/            # elset — dedup + SCD-2 historisation
├── tests/                    # custom SCD-2 invariant tests
└── macros/                   # literal schema naming
sql/init/                     # schema + table DDL, run on first boot
docker-compose.yml            # local Postgres warehouse
tests/                        # pytest suite, mirrors src/ layout
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
