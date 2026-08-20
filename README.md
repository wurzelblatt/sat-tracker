# sat-tracker

A satellite tracking pipeline, built as a medallion architecture. It pulls
orbital element sets (OMM/GP) and the object catalogue (SATCAT) from
[CelesTrak](https://celestrak.org), lands them byte-for-byte in a bronze
layer, curates them through dbt-built silver and gold layers, and
propagates every tracked object with SGP4 into a PostGIS table of where
things are right now.

> **Status:** capstone project in progress. The pipeline is complete end to
> end — ingestion, bronze, silver, gold, SGP4 propagation, an interactive
> map, Airflow orchestration and observer pass prediction — with 311 Python
> tests and 48 dbt tests. A Terraform slice is what remains. See
> [`docs/architecture.md`](docs/architecture.md) for design rationale and
> [`docs/runbook.md`](docs/runbook.md) for day-to-day operation.

At the time of writing it tracks **18,992 objects** — about 55% of
everything SATCAT lists as in orbit, which is as far as CelesTrak's public
feed reaches — propagated from element sets averaging 18 hours old.

## Features

- **Two CelesTrak feeds.** Orbital elements for a single satellite, a group
  (`starlink`, `active`, `gps-ops`, ...), plus the full ~70,000-object
  SATCAT catalogue that makes the objects describable. Holding the whole
  catalogue rather than just what has elements is what makes the coverage
  gap measurable: 34,512 objects are in orbit, 18,992 have public element
  sets, and the difference is visible instead of silently redefined away.
- **CelesTrak Compliance Shield** — an identifying User-Agent, a daily
  download budget enforced before the request, per-feed local caching (2 h
  for elements, 24 h for the catalogue), `ETag`/`If-None-Match` conditional
  requests, and fail-fast on any status other than `200`/`304` with no
  retry. CelesTrak firewall-blocks IPs that exceed 100 MB/day or hammer a
  blocking response.
- **Auditable by construction.** Collision-free landing filenames
  (`<stem>_<ingested_at>_<ingestion_id><suffix>`) with a `.meta.json`
  sidecar per file, and lineage columns carried through every layer.
- **Idempotent loading.** Partitioned Parquet datasets loaded into Postgres
  with `COPY` + `ON CONFLICT DO NOTHING` — re-running never duplicates,
  which is what makes an orchestrator's retry safe.
- **SCD-2 history without snapshots.** `silver.elset` derives
  `valid_from`/`valid_to` with window functions over `epoch` — when the
  orbit was *measured* — rather than ingestion time. History is not
  reconstructed; it *is* the data. Guarded by custom tests for interval
  contiguity and exactly-one-current-row-per-satellite.
- **A gold layer that knows what it cannot answer.** `dim_object` carries
  the full catalogue including decayed objects, so a fact can still resolve
  a satellite that re-entered, and flags what must not be propagated:
  objects that have decayed, and objects not in Earth orbit at all.
- **SGP4 propagation with a hand-verified frame conversion.** TEME → WGS84
  via GMST rotation and Bowring's closed form, tested against three
  independent oracles because every bug it can have produces a plausible
  position somewhere real.
- **PostGIS serving table** with a generated `geography` column and a GIST
  index, so "what is within 50 km of here" is a range scan.
- **Ask what is overhead.** Give it a latitude and longitude and it filters
  to what is above your horizon, then tabulates when a chosen satellite
  rises, peaks and sets over the next three days — each with a compass
  bearing. Geometric visibility, not optical: it does not claim you could
  see the thing with your eyes.
- **An interactive map** that propagates on demand rather than reading a
  stored snapshot, in both Mercator and globe projections. Click a
  satellite to trace its orbit: the flat map draws the ground track that
  marches west as the Earth turns beneath it, the globe draws the closed
  orbit at true altitude. Confidence is expressed as element-set age
  judged *per orbital regime*, because high orbits are predictable and a
  flat staleness threshold would libel a fifth of Galileo.
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

# Fetch the active catalogue and the object catalogue, loading both
uv run sat-tracker-ingest --group active --format csv --load
uv run sat-tracker-satcat --load

# Build silver and gold, running every test
uv run sat-tracker-transform --command deps   # first run only
uv run sat-tracker-transform

# Propagate every current element set to now
uv run sat-tracker-propagate

# Or explore it interactively
uv run sat-tracker-map

# Which satellites are closest to Berlin right now?
docker compose exec postgres psql -U sat_tracker -d sat_tracker -c "
SELECT d.object_name, d.orbit_regime,
       round((ST_Distance(p.geo_point,
             ST_SetSRID(ST_MakePoint(13.40, 52.52), 4326)::geography)/1000)::numeric, 0) AS km_away,
       round(p.altitude_km::numeric, 0) AS alt_km
FROM gold.position_snapshot p
JOIN gold.dim_object d USING (norad_cat_id)
ORDER BY p.geo_point <-> ST_SetSRID(ST_MakePoint(13.40, 52.52), 4326)::geography
LIMIT 5;"
```

That last query is the whole pipeline in one line of output — an object
name from SATCAT, a position from SGP4, and a distance from PostGIS.

See [`docs/runbook.md`](docs/runbook.md) for the full command reference,
including running stages separately, rebuilding the warehouse from
scratch, and troubleshooting.

## Project layout

```
src/sat_tracker/
├── config.py                 # single pydantic-settings Settings singleton
├── cli.py                    # one entry point per pipeline stage
├── ingest/
│   └── celestrak_client.py   # GP + SATCAT clients, compliance shield
├── storage/
│   ├── datasets.py           # BronzeDataset descriptors (GP, SATCAT)
│   ├── parquet_writer.py     # landed CSV → partitioned Parquet
│   ├── postgres_loader.py    # Parquet → bronze tables (idempotent)
│   └── snapshot_writer.py    # positions → gold.position_snapshot
├── propagate/
│   ├── frames.py             # TEME → WGS84 (GMST rotation + Bowring)
│   ├── elements.py           # warehouse rows → SatrecArray → positions
│   ├── tracks.py             # one satellite over one revolution
│   └── passes.py             # when it is above a given horizon
├── app.py                    # Streamlit map, propagating on demand
└── assets/land.json          # country outlines for the globe view
transform/                    # dbt project (in-repo profiles.yml)
├── models/staging/           # casts bronze TEXT → real types
├── models/silver/            # elset (SCD-2), space_object (current state)
├── models/gold/              # dim_object, fact_propagatable_elset
├── tests/                    # custom SCD-2 invariant tests
└── macros/                   # literal schema naming
sql/init/                     # extensions, schemas, and non-dbt table DDL
docker-compose.yml            # local PostGIS warehouse
docs/                         # architecture and runbook
reports/                      # per-step write-ups
tests/                        # pytest suite, mirrors src/ layout
```

The six CLI commands map one-to-one onto pipeline stages:

| Command | Stage |
|---|---|
| `sat-tracker-ingest` | CelesTrak → `data/bronze/` |
| `sat-tracker-satcat` | CelesTrak → `data/bronze_satcat/` |
| `sat-tracker-load` | landings → Parquet → `bronze.*` |
| `sat-tracker-transform` | dbt: staging → silver → gold |
| `sat-tracker-propagate` | SGP4 → `gold.position_snapshot` |
| `sat-tracker-map` | Streamlit map, propagating in-process |

Airflow schedules the first five every two hours, as `BashOperator` tasks
over those same commands — see
[`docs/runbook.md`](docs/runbook.md#scheduling-with-airflow). It is optional
and lives in its own compose file: orchestration is never a prerequisite
for running the pipeline.

Each works standalone, so an orchestrator is thin operators over commands
that already run on their own — and a scheduling problem can never
masquerade as a data problem.

## Configuration

All runtime settings live in `src/sat_tracker/config.py` and are
overridable via environment variables prefixed `SAT_TRACKER_`, or a
local `.env` file (never committed — see `.gitignore`). See
[`docs/architecture.md`](docs/architecture.md#sat_trackerconfigsettings)
for the full list of settings.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — system design, data
  flow, and the compliance/reliability guarantees the pipeline makes.
  Includes the reasoning behind decisions the code states but cannot
  justify: why the full SATCAT dump, why the coordinate transformation
  accepts the approximations it does, why `position_snapshot` is not a dbt
  model.
- [`docs/runbook.md`](docs/runbook.md) — operational how-to: running each
  stage, rebuilding the warehouse from scratch, and troubleshooting by
  symptom.
- [`reports/`](reports/) — per-step write-ups recording what was built and
  what the data turned out to say.
