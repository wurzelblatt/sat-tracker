-- Bronze landing table for CelesTrak SATCAT records.
--
-- As in bronze.raw_gp, every source column is TEXT: bronze's contract is
-- fidelity, and SATCAT is full of fields that only sometimes carry a
-- value. DECAY_DATE is empty for every object still in orbit, RCS is
-- empty where the radar cross-section is not published, and PERIOD /
-- APOGEE / PERIGEE are empty for objects with no current orbit. Typing
-- those here would turn a normal absence into a load failure; dbt casts
-- them in the silver/gold layers where a bad value is a test failure.
--
-- Column names mirror the SATCAT CSV header verbatim (lowercased); see
-- https://celestrak.org/satcat/satcat-format.php.
--
-- This is the FULL catalogue, not a filter of currently-active objects.
-- gold.dim_object is a dimension: it must still resolve the NORAD ID of
-- a satellite that decayed after its last element set was published,
-- which an "active only" pull cannot guarantee.

CREATE TABLE IF NOT EXISTS bronze.raw_satcat (
    -- Lineage (typed: these are ours, not CelesTrak's)
    ingest_ts            TIMESTAMPTZ NOT NULL,
    ingestion_id         UUID        NOT NULL,
    source               TEXT        NOT NULL,
    source_file          TEXT        NOT NULL,
    target               TEXT        NOT NULL,

    -- CelesTrak SATCAT CSV payload, verbatim
    object_name          TEXT,
    object_id            TEXT,
    norad_cat_id         TEXT,
    object_type          TEXT,
    ops_status_code      TEXT,
    owner                TEXT,
    launch_date          TEXT,
    launch_site          TEXT,
    decay_date           TEXT,
    period               TEXT,
    inclination          TEXT,
    apogee               TEXT,
    perigee              TEXT,
    rcs                  TEXT,
    data_status_code     TEXT,
    orbit_center         TEXT,
    orbit_type           TEXT,

    -- Re-loading the same file is a no-op rather than a duplicate.
    PRIMARY KEY (source, source_file, norad_cat_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_satcat_ingest_ts ON bronze.raw_satcat (ingest_ts DESC);
CREATE INDEX IF NOT EXISTS idx_raw_satcat_norad ON bronze.raw_satcat (norad_cat_id);
