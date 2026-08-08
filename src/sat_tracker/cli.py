"""Command-line entry point for running CelesTrak ingestion by hand."""

import argparse
import logging

from sat_tracker.ingest.celestrak_client import (
    fetch_omm_csv,
    fetch_omm_csv_group,
    fetch_omm_sds,
    fetch_omm_sds_group,
)


def main() -> None:
    """Parse CLI arguments and ingest one NORAD ID or CelesTrak GP group."""
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
        "--quiet",
        action="store_true",
        help="Abridge output: suppress INFO-level logging (only warnings/errors and the final "
        "result are printed). Verbose INFO logging is on by default.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO)

    if args.norad_id is not None:
        path = fetch_omm_csv(args.norad_id) if args.format == "csv" else fetch_omm_sds(args.norad_id)
    else:
        path = (
            fetch_omm_csv_group(args.group)
            if args.format == "csv"
            else fetch_omm_sds_group(args.group)
        )

    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
