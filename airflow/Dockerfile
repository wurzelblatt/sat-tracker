# Airflow with the sat-tracker pipeline installed alongside it.
#
# ── Why the project gets its own virtualenv ──────────────────────────
# Airflow and dbt-core both pin jinja2, pydantic and packaging, and not
# to the same ranges. Installing the pipeline into Airflow's own Python
# invites a resolution conflict that is miserable to debug and that would
# couple two release cycles together for no reason.
#
# So the image carries `uv`, and the project is synced into a separate
# environment. Tasks invoke `uv run`, and the two dependency sets never
# meet. This is also what makes the BashOperator choice honest: the DAG
# runs the same commands a developer runs, in the same isolation.
#
# ── Why the source is baked in rather than mounted ───────────────────
# `uv sync` then happens once, at build time, instead of on every task.
# The cost is that changing pipeline code needs an image rebuild, which
# is the right trade for a demo and is documented in the runbook.
#
# Only `data/` is mounted at runtime, because that is genuinely shared
# state: the ingest task writes a landing file that the load task must
# be able to read.

FROM apache/airflow:slim-latest-python3.12

USER root

# git is required by dbt to resolve its package dependencies (dbt_utils).
RUN apt-get update \
    && apt-get install --no-install-recommends --yes git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# The slim variant ships none of these, and all three are needed:
#   standard  — provides BashOperator
#   psycopg2  — Airflow's synchronous metadata connection
#   asyncpg   — Airflow 3 also opens an ASYNC engine against the same
#               database, and SQLAlchemy needs a separate driver for it.
#               Installing only psycopg2 gets the scheduler running and
#               then fails in the API server, which is a confusing way to
#               discover the requirement.
#
# Note these are Airflow's drivers. The pipeline uses psycopg 3 in its own
# virtualenv, and the two never meet — which is the point of the split.
USER airflow
RUN pip install --no-cache-dir \
    apache-airflow-providers-standard \
    psycopg2-binary \
    asyncpg

# Absolute, and outside the project directory on purpose. Left to its
# default, `uv` would create `.venv` inside the project — which is
# bind-mounted from the developer's machine, so a Linux virtualenv would
# overwrite their macOS one and break the host setup.
ENV UV_PROJECT_ENVIRONMENT=/opt/airflow/sat_tracker_venv \
    UV_LINK_MODE=copy \
    PATH="/opt/airflow/sat_tracker_venv/bin:${PATH}"

WORKDIR /opt/airflow/sat_tracker

# Manifests first, so a code change does not invalidate the dependency
# layer and force a full re-resolve.
COPY --chown=airflow:root pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY --chown=airflow:root src/ ./src/
COPY --chown=airflow:root transform/ ./transform/
RUN uv sync --frozen --no-dev

# dbt resolves its packages into transform/dbt_packages at build time, so
# no task has to reach the network to run a transformation.
RUN uv run dbt deps --project-dir transform --profiles-dir transform
