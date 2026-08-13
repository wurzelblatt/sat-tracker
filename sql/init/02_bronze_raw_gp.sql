-- Bronze landing table for CelesTrak GP/OMM records.
--
-- Every source column is TEXT on purpose. Bronze's contract is fidelity,
-- not usability: a malformed value from CelesTrak must land intact rather
-- than fail the load or be silently coerced. Casting to real types is
-- dbt's job in the silver layer, where a bad value becomes a visible test
-- failure instead of a lost row.
--
-- Column names mirror the CelesTrak OMM-CSV header verbatim (lowercased).
-- Note this deliberately differs from the bronze sketch in
-- .claude/data-model.md, which lists epoch_year/epoch_day and
-- tle_line1/tle_line2: the OMM-CSV feed carries neither. It has a single
-- ISO `EPOCH` timestamp, and TLE line pairs only exist in the legacy TLE
-- format that six-digit NORAD IDs are phasing out.

CREATE TABLE IF NOT EXISTS bronze.raw_gp (
    -- Lineage (typed: these are ours, not CelesTrak's)
    ingest_ts            TIMESTAMPTZ NOT NULL,
    ingestion_id         UUID        NOT NULL,
    source               TEXT        NOT NULL,
    source_file          TEXT        NOT NULL,
    target               TEXT        NOT NULL,

    -- CelesTrak OMM-CSV payload, verbatim
    object_name          TEXT,
    object_id            TEXT,
    epoch                TEXT,
    mean_motion          TEXT,
    eccentricity         TEXT,
    inclination          TEXT,
    ra_of_asc_node       TEXT,
    arg_of_pericenter    TEXT,
    mean_anomaly         TEXT,
    ephemeris_type       TEXT,
    classification_type  TEXT,
    norad_cat_id         TEXT,
    element_set_no       TEXT,
    rev_at_epoch         TEXT,
    bstar                TEXT,
    mean_motion_dot      TEXT,
    mean_motion_ddot     TEXT,

    -- Re-loading the same file is a no-op rather than a duplicate.
    PRIMARY KEY (source, source_file, norad_cat_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_gp_ingest_ts ON bronze.raw_gp (ingest_ts DESC);
CREATE INDEX IF NOT EXISTS idx_raw_gp_norad ON bronze.raw_gp (norad_cat_id);
