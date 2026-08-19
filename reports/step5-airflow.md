# Step 5 — Airflow

Completed 2026-08-20, on `feature/start-airflow`.

Step 5 put the pipeline on a schedule. The DAG itself was straightforward
— six operators and a dependency chain, written in an afternoon. What took
the time was everything a container changes about code that had only ever
run on a laptop.

This report records that, because it is the part that does not survive in
the finished configuration.

---

## What was built

| | |
|---|---|
| `airflow/Dockerfile` | Airflow 3.3.1 + `uv` + the project in its own virtualenv |
| `docker-compose.airflow.yml` | metadata Postgres + Airflow `standalone` |
| `airflow/dags/sat_tracker_pipeline.py` | five tasks, every two hours |

```
ingest_gp ─────┐
               ├──► load ──► transform ──► propagate
ingest_satcat ─┘
```

Verified end to end: all five tasks green, **18,992 positions written to
`gold.position_snapshot` by the DAG** rather than by hand.

---

## Decision 1 — the DAG carries no pipeline logic

Every task is a `BashOperator` invoking a command that already worked
standalone. That was not a convenience; it was a decision made back in
Step 1 and only cashed in here.

Two things follow. A scheduling problem cannot masquerade as a data
problem, because the scheduler's only job is to invoke commands that work
on their own. And the DAG is portable: moving to ECS or Kubernetes changes
what each task *invokes*, not what the graph *means*.

**Retries are safe by construction, not by configuration.** Nothing was
added to make them so:

- ingestion caches per feed and replays an `ETag`, so a retry inside the
  cache window makes no request at all
- loading is `COPY` into staging plus `INSERT ... ON CONFLICT DO NOTHING`
- `dbt build` rebuilds from bronze and exits non-zero on a failed test
- propagation replaces the snapshot inside one transaction

**`transform` depends on both ingests**, not on GP alone. `is_decayed` is
only as fresh as the SATCAT pull behind it, and a GP refresh on its own
leaves recently re-entered objects looking alive. Two satellites were
caught by exactly that in Step 3, so the dependency is encoded in the graph
where it cannot be forgotten.

---

## Decision 2 — BashOperator over DockerOperator

`DockerOperator` would run each task in a fresh container, which is closer
to how ECS or Kubernetes work. It was rejected for a local demo because it
requires mounting the Docker socket — a real privilege escalation — and
because this pipeline shares state through `data/`, so every task container
would need the same volume and network or the ingest's landing file would
be invisible to the load.

The deciding argument was that **the DAG is identical either way**. It is
five task definitions and a dependency chain; swapping operators later
changes the task bodies, not the design. `BashOperator` is not a compromise
to regret, it is the cheap version of a portable thing.

---

## What containerising exposed

Three faults, all the same shape: **code that worked on the host and broke
once moved**, because a container changes where code sits relative to what
it needs. None was a design flaw. All were assumptions never tested,
because nothing had moved the code before.

### Two configuration vocabularies for one database

`Settings` drives the Python stages through `SAT_TRACKER_POSTGRES_DSN`. dbt
does not read `Settings` at all — it has its own config in
`transform/profiles.yml`, parameterised by `SAT_TRACKER_POSTGRES_HOST` and
`_PORT`.

Both are properly environment-driven; neither hardcodes anything. But they
are different names for the same database, so setting only the DSN produced
a run where `ingest`, `load` and `propagate` connected happily and
`transform` alone reached for `localhost:5433` — which inside a container is
the container itself.

The failure took a full ten-minute DAG run to surface, at the second-to-last
task.

### `uv` re-resolving the project on every task

The first verification run printed this, and I dismissed it:

```
Building sat-tracker @ file:///opt/airflow/sat_tracker
Uninstalled 1 package... Installed 1 package
```

`uv run` re-resolves and rebuilds the project on every invocation, even
though the image synced it at build time. Judged "about a second per task,
negligible".

It was not. Five wheel builds per DAG run saturated the CPU; the
scheduler's heartbeat stalled for 60–85 seconds; and Airflow's supervisor
concluded the tasks were unresponsive and **SIGKILLed the very work that was
starving it**.

The symptom was 148-byte task logs stopping after `Pre Execute`, and
`exit_code=<Negsignal.SIGKILL: -9>` in the scheduler. That reads exactly
like a crash. Only `Heartbeat recovered after 84.78 seconds` distinguished
"killed" from "too slow to stay alive".

`uv run --no-sync` is therefore load-bearing rather than an optimisation.
`load` went from 372 s to 175 s once it stopped rebuilding the package
before doing its work.

### Two compose files, one project

`docker compose -f docker-compose.airflow.yml down` failed every time with
"Resource is still in use".

Compose derives the project name from the **directory**, so both compose
files claimed `sat_tracker` — which made their `default` networks the same
network. The airflow service was joining it twice: implicitly as the
project default, and explicitly as the `warehouse` external. Tearing down
then tried to remove a network the warehouse containers were still using.

Declaring `name: sat_tracker_airflow` gives the stack its own project. The
belt and the braces had turned out to be the same object.

---

## Performance, and how much of it is Airflow

A scheduled run takes about ten minutes. Measured against the same commands
elsewhere:

| Task | Host | In the container | Under Airflow |
|---|---|---|---|
| `transform` | 12 s | 21 s | 96 s |
| `propagate` | 4 s | 6 s | 91 s |

Two distinct costs. **Host to container is ~1.7×** — Docker on macOS runs a
Linux VM with a virtualised filesystem, which is simply the price of
containerising. **Container to Airflow is another ~4×**, and that is
per-task orchestration overhead: a supervisor fork, a fresh Python
interpreter, the DAG file re-parsed in the worker, a metadata connection,
and logs streamed back over Airflow's own API.

That overhead is roughly **constant regardless of data volume**. `propagate`
did four seconds of real work and spent about eighty-seven being
orchestrated. Ten times the data would not grow the overhead — only shrink
its share.

Some of it is also the machine: 8 GB of RAM with 5.8 GB allocated to
Docker, and a load average of 13.7 on eight cores. On a cloud runner with
dedicated CPU the per-task tax is typically 10–20 s rather than 60–80.

At a two-hour schedule a ten-minute run uses 8% of its window, so none of
this is a problem in practice. The obvious remedy — merging the five tasks
into one — would remove per-stage retry and visibility, which is precisely
what Airflow was introduced to provide.

---

## The lesson that cost the most

Every earlier step in this project ran its tests in about four seconds, so
trial-and-error was nearly free and became the working habit. Here a
rebuild was four minutes and a DAG run was ten, so the same habit cost
ten to fifteen minutes per wrong guess.

With a slow feedback loop the economics invert: it becomes worth verifying
every assumption in one throwaway container before building anything. One
such check *was* run — and it printed the `uv` rebuild that caused the worst
failure of the day, twenty minutes before it happened.

---

## Open items carried forward

- A Terraform slice: S3, RDS with PostGIS, IAM — applied, run against once,
  and destroyed.
- Observer visibility and pass prediction, designed and deferred; see
  `plans/observer-visibility-and-passes.md`.
- `load` re-converts every landing on each run. Converting only new ones
  would save about two minutes, and matters only if the schedule tightens.
