"""Scheduled run of the sat-tracker pipeline.

Every task is a `BashOperator` invoking a command that already works on
its own. That is the whole design: the DAG contains no pipeline logic, so
a scheduling problem cannot masquerade as a data problem, and the pipeline
stays demoable with the scheduler down.

It also makes the DAG portable. Moving to ECS or Kubernetes later means
changing what each task *invokes*, not what the graph *means*.

── Why retries are safe here ────────────────────────────────────────
Not by configuration, but by construction, decided long before Airflow
was introduced:

- ingestion caches per feed and replays an ETag, so a retry inside the
  cache window makes no request at all
- loading is `COPY` into staging plus `INSERT ... ON CONFLICT DO NOTHING`,
  so a repeated load reports zero new rows rather than duplicating
- `dbt build` rebuilds from bronze and exits non-zero if any test fails,
  so a failed test fails the task
- propagation replaces `gold.position_snapshot` inside one transaction

── Why both feeds must refresh together ─────────────────────────────
`transform` depends on *both* ingests rather than on GP alone, and that
is not tidiness. `gold.dim_object.is_decayed` is only as fresh as the
SATCAT pull behind it: a GP refresh on its own leaves recently re-entered
objects looking alive, and they get propagated. Two satellites were caught
by exactly that during development, so the dependency is encoded here
where it cannot be forgotten.
"""

from datetime import UTC, datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

PROJECT_DIR = "/opt/airflow/sat_tracker"

# CelesTrak recomputes GP data on roughly a two-hour cycle, and the client
# caches for the same window. Scheduling faster would gain nothing: the
# compliance shield would serve the cached file and make no request.
SCHEDULE = "0 */2 * * *"

DEFAULT_ARGS = {
    "owner": "sat-tracker",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    # A retry that waits five minutes is well inside CelesTrak's cache
    # window, so a transient failure re-runs against the cached landing
    # rather than fetching again.
    "depends_on_past": False,
}


def _task(task_id: str, command: str) -> BashOperator:
    """Build a task that runs one pipeline command in the project's venv.

    Args:
        task_id: Airflow task identifier.
        command: The `sat-tracker-*` invocation, without `uv run`.

    Returns:
        A `BashOperator` running the command from the project directory.
        `cd` matters: the landing-zone settings are relative paths, so
        they resolve against the working directory.
    """
    return BashOperator(
        task_id=task_id,
        # --no-sync is not an optimisation, it is load-bearing. Without
        # it `uv run` re-resolves and rebuilds the project on every task,
        # even though the image synced it at build time. Five rebuilds
        # per DAG run starve the scheduler of CPU, its heartbeat stalls,
        # and the supervisor SIGKILLs the very tasks doing the work.
        # VIRTUAL_ENV is unset because Airflow activates its own venv,
        # which uv then warns about on every task line.
        bash_command=(
            f"unset VIRTUAL_ENV && cd {PROJECT_DIR} && uv run --no-sync {command}"
        ),
    )


with DAG(
    dag_id="sat_tracker_pipeline",
    description="Fetch CelesTrak data, build the warehouse, propagate positions",
    default_args=DEFAULT_ARGS,
    schedule=SCHEDULE,
    # Timezone-aware on purpose: a naive start_date leaves the
    # scheduler to assume a zone, and every run boundary inherits
    # that assumption.
    start_date=datetime(2026, 8, 19, tzinfo=UTC),
    # No backfill: element sets describe the present, and CelesTrak does
    # not serve history at this endpoint. A catch-up run would fetch
    # today's data and label it with yesterday's date.
    catchup=False,
    max_active_runs=1,
    tags=["sat-tracker"],
) as dag:
    ingest_gp = _task(
        "ingest_gp",
        "sat-tracker-ingest --group active --format csv --load",
    )

    ingest_satcat = _task("ingest_satcat", "sat-tracker-satcat --load")

    load = _task("load", "sat-tracker-load")

    transform = _task("transform", "sat-tracker-transform")

    propagate = _task("propagate", "sat-tracker-propagate")

    # The two feeds are independent, so they run in parallel. Everything
    # downstream is sequential because each stage consumes the previous
    # stage's output — and `transform` waits for both, so the decay gate
    # can never be built from a stale catalogue.
    [ingest_gp, ingest_satcat] >> load >> transform >> propagate
