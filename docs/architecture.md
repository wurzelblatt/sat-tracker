# Architecture

## Overview

`sat-tracker` ingests satellite orbital-element data (OMM records) from
CelesTrak and lands it in a local **bronze** layer, preserving the raw
payload byte-for-byte alongside structured audit metadata. This is the
first stage of a medallion architecture; a silver layer (cleaned,
deduplicated, schema-conformed data suitable for analysis) is the next
planned milestone and is not yet implemented.

```
CelesTrak GP endpoint
        │
        │  HTTP GET (CATNR=<norad_id> or GROUP=<name>, FORMAT=CSV|json)
        ▼
 CelesTrak Compliance Shield
   ├─ cache check (skip HTTP if a landing file <2h old exists)
   └─ fail-fast status gate (raise on any non-200, never retry)
        │
        ▼
   ┌────┴─────┐
   ▼          ▼
 bronze/     sds/
 *.csv       *.sds (OMM FlatBuffer)
 *.meta.json *.meta.json
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

## Data flow (current state)

```
CelesTrak ──▶ bronze/ (data/bronze/*.csv, data/sds/*.sds)
```

Both `data/` and `notebooks/` are gitignored — ingested data and ad hoc
exploration notebooks are runtime/working artifacts, not source.

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

- Silver-layer curation (cleaning, deduplication, schema conformance)
  from the bronze/sds landing zones — not yet started.
- SDS batch/multi-record encoding for group ingestion.
- No scheduling/orchestration yet — ingestion is run by hand via the
  CLI; see [`docs/runbook.md`](docs/runbook.md).
