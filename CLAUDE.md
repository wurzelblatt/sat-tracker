# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

This is an early-stage scaffold for `sat_tracker`, a satellite orbital-data ingestion project. Most functionality is not yet implemented — e.g. `fetch_omm_data()` currently just raises `NotImplementedError` as a placeholder. When extending it, follow the existing scaffold's conventions rather than assuming prior working behavior.

## Commands

This project uses `uv` exclusively for dependency and environment management — never `pip` directly.

- Install/sync dependencies: `uv sync`
- Run all tests: `uv run pytest`
- Run a single test: `uv run pytest tests/test_celestrak.py::test_fetch_omm_data_not_implemented`
- Lint: `uv run ruff check .`

## Architecture

- **`src/` layout**: the installable package is `src/sat_tracker/`, imported as `sat_tracker`.
- **Configuration**: all runtime settings live in a single `pydantic-settings` `Settings` class in `src/sat_tracker/config.py`, exposed as a module-level singleton `settings`. Settings are overridable via environment variables prefixed `SAT_TRACKER_` or a local `.env` file. Do not hardcode URLs, credentials, or environment-specific values in client code — add a field to `Settings` instead and reference `settings.<field>`.
- **`src/sat_tracker/ingest/`**: subpackage for clients that pull raw orbital data from external sources. `celestrak_client.py` is the first such client, targeting the CelesTrak GP (General Perturbations) endpoint to fetch OMM (Orbit Mean-Elements Message) data by NORAD catalog number. Additional data sources should follow the same pattern: a client module under `ingest/` that reads its base URL/config from `sat_tracker.config.settings`.
- **Tests**: `tests/` mirrors the package structure, using `pytest` with shared fixtures in `tests/conftest.py` (e.g. a `settings` fixture that builds `Settings(_env_file=None)` to isolate tests from the developer's local `.env`).

## Code style (from project rules)

- Target Python 3.12+ (see `.python-version`; `pyproject.toml` requires `>=3.12`).
- Write comprehensive docstrings for all modules, classes, and functions, documenting parameters, return values, and exceptions raised.
- Add inline comments only for non-obvious business logic or complex algorithms, not self-explanatory code.
