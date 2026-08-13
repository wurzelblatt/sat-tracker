"""Command-line entry points for running the pipeline by hand.

Each pipeline stage is a separate, independently runnable command:

- ``sat-tracker-ingest`` fetches from CelesTrak into the bronze landing zone
- ``sat-tracker-load`` converts landed CSV to Parquet and loads it to Postgres
- ``sat-tracker-transform`` builds the dbt silver/gold models and tests them

Keeping the stages separate is deliberate. An orchestrator (Airflow)
should call commands that already work standalone, so a scheduling
problem never masquerades as a data problem — and so the pipeline stays
demoable even if the scheduler is down.
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from sat_tracker.config import settings
from sat_tracker.ingest.celestrak_client import (
    fetch_omm_csv,
    fetch_omm_csv_group,
    fetch_omm_sds,
    fetch_omm_sds_group,
)
from sat_tracker.storage.parquet_writer import write_bronze_parquet
from sat_tracker.storage.postgres_loader import load_bronze_to_postgres


def _add_verbosity_flag(parser: argparse.ArgumentParser) -> None:
    """Attach the shared `--quiet` flag to a parser."""
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Abridge output: suppress INFO-level logging (only warnings/errors and the "
        "final result are printed). Verbose INFO logging is on by default.",
    )


def _configure_logging(quiet: bool) -> None:
    """Set the root log level from the `--quiet` flag."""
    logging.basicConfig(level=logging.WARNING if quiet else logging.INFO)


def main() -> None:
    """Fetch CelesTrak OMM data for one NORAD ID or one group into bronze."""
    parser = argparse.ArgumentParser(
        description="Fetch CelesTrak OMM data into the local bronze landing zone."
    )

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--norad-id",
        type=int,
        help="NORAD catalog number of a single satellite, e.g. 25544 (the ISS).",
    )
    target.add_argument(
        "--group",
        help="CelesTrak GP group name, e.g. 'starlink' (see https://celestrak.org/NORAD/elements/).",
    )

    parser.add_argument(
        "--format",
        choices=["csv", "sds"],
        default="csv",
        help="Output format to save under bronze: compact OMM-CSV or binary SDS FlatBuffer. "
        "Defaults to 'csv'.",
    )
    parser.add_argument(
        "--load",
        action="store_true",
        help="After fetching, also convert the landed CSV to Parquet and load it into "
        "Postgres. Ignored for --format sds, which the warehouse does not consume.",
    )
    _add_verbosity_flag(parser)
    args = parser.parse_args()

    _configure_logging(args.quiet)

    if args.norad_id is not None:
        path = fetch_omm_csv(args.norad_id) if args.format == "csv" else fetch_omm_sds(args.norad_id)
    else:
        path = (
            fetch_omm_csv_group(args.group)
            if args.format == "csv"
            else fetch_omm_sds_group(args.group)
        )

    print(f"Wrote {path}")

    if args.load:
        if args.format != "csv":
            print("Skipping --load: only the CSV format feeds the warehouse.")
            return
        write_bronze_parquet(path)
        inserted = load_bronze_to_postgres(source_file=path.name)
        print(f"Loaded {inserted} new rows into bronze.raw_gp")


def load() -> None:
    """Convert landed bronze CSV files to Parquet and load them into Postgres."""
    parser = argparse.ArgumentParser(
        description="Convert bronze CSV landings to Parquet and load them into Postgres."
    )
    parser.add_argument(
        "--source-file",
        help="Name of a single landed CSV to process (e.g. 'starlink_2026...csv'). "
        "Defaults to every CSV in the bronze landing zone.",
    )
    parser.add_argument(
        "--skip-parquet",
        action="store_true",
        help="Load the existing Parquet dataset into Postgres without re-converting "
        "the CSV landings first.",
    )
    _add_verbosity_flag(parser)
    args = parser.parse_args()

    _configure_logging(args.quiet)

    if not args.skip_parquet:
        if args.source_file:
            csv_paths = [settings.bronze_dir / args.source_file]
        else:
            csv_paths = sorted(settings.bronze_dir.glob("*.csv"))

        if not csv_paths:
            print(f"No CSV landings found in {settings.bronze_dir}")
            return

        for csv_path in csv_paths:
            write_bronze_parquet(csv_path)
            print(f"Converted {csv_path.name}")

    inserted = load_bronze_to_postgres(source_file=args.source_file)
    print(f"Loaded {inserted} new rows into bronze.raw_gp")


def transform() -> None:
    """Build and test the dbt models over the loaded bronze data."""
    parser = argparse.ArgumentParser(
        description="Run the dbt transformations that build the silver and gold layers."
    )
    parser.add_argument(
        "--select",
        help="dbt node selector, e.g. 'silver.elset' or 'staging'. Defaults to everything.",
    )
    parser.add_argument(
        "--command",
        default="build",
        choices=["build", "run", "test", "deps"],
        help="dbt subcommand to invoke. 'build' (the default) runs models and their "
        "tests together, so a model that produces bad data fails immediately rather "
        "than being tested as an afterthought.",
    )
    _add_verbosity_flag(parser)
    args = parser.parse_args()

    _configure_logging(args.quiet)

    # The dbt project keeps its own profiles.yml in-repo, so neither the
    # caller nor a future Airflow worker needs a ~/.dbt directory.
    project_dir = Path(__file__).resolve().parents[2] / "transform"
    command = [
        "dbt",
        args.command,
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(project_dir),
    ]
    if args.select:
        command += ["--select", args.select]

    # check=False so the dbt exit code propagates as-is: a failed test
    # must fail the task, not raise a Python traceback over the top of
    # dbt's own far more useful output.
    result = subprocess.run(command, check=False)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
