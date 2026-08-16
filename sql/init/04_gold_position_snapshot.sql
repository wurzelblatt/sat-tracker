-- Propagated satellite positions: where each object was at a given instant.
--
-- Unlike every other gold table, dbt does NOT own this one. SGP4 cannot
-- run in SQL, so the rows are computed in Python by sat-tracker-propagate
-- and written here. That makes this table a second landing zone rather
-- than a transformation output, which is why it lives in sql/init/
-- alongside the bronze tables instead of in transform/models/gold/.
--
-- Grain: one row per (satellite, snapshot instant).

CREATE TABLE IF NOT EXISTS gold.position_snapshot (
    norad_cat_id      INTEGER          NOT NULL,

    -- The instant propagated TO, and the epoch of the element set
    -- propagated FROM. Both are stored because accuracy is a function of
    -- the separation between them, not of either one alone.
    snapshot_ts       TIMESTAMPTZ      NOT NULL,
    epoch             TIMESTAMPTZ      NOT NULL,

    -- SIGNED, and deliberately not constrained to be positive. XMM-Newton,
    -- Chandra and Cluster II-FM7 publish element sets with epochs up to
    -- two days in the FUTURE, which is normal practice for highly
    -- eccentric orbits where the elements are anchored at a predicted
    -- perigee passage. A CHECK (epoch_age_hours >= 0) would reject real,
    -- valid data.
    --
    -- This is the confidence column. SGP4 error grows roughly 1-3 km per
    -- day of separation, but what counts as stale depends on the orbital
    -- regime: MEO satellites routinely carry element sets days old
    -- because negligible drag makes them predictable, so a map should
    -- interpret this against dim_object.orbit_regime rather than against
    -- a flat threshold.
    epoch_age_hours   DOUBLE PRECISION NOT NULL,

    -- Geodetic position on the WGS84 ellipsoid, converted from the TEME
    -- frame SGP4 returns. altitude_km is height above the ELLIPSOID, not
    -- above mean sea level or terrain.
    latitude_deg      DOUBLE PRECISION NOT NULL,
    longitude_deg     DOUBLE PRECISION NOT NULL,
    altitude_km       DOUBLE PRECISION NOT NULL,

    -- The raw TEME state vector, exactly as SGP4 returns it and
    -- deliberately NOT rotated into an Earth-fixed frame.
    --
    -- Position is stored alongside velocity rather than only in geodetic
    -- form because the two must live in the SAME frame to be useful
    -- together: conjunction screening needs the relative velocity between
    -- two objects, and that subtraction is only meaningful in a common
    -- inertial frame. Keeping r and v together means downstream work
    -- never has to invert the geodetic conversion to recover the state.
    --
    -- It also makes the TEME to WGS84 conversion auditable: the input and
    -- the output sit in the same row, so the transform can be checked
    -- with a query rather than by re-running it.
    position_x_km     DOUBLE PRECISION,
    position_y_km     DOUBLE PRECISION,
    position_z_km     DOUBLE PRECISION,

    velocity_x_km_s   DOUBLE PRECISION,
    velocity_y_km_s   DOUBLE PRECISION,
    velocity_z_km_s   DOUBLE PRECISION,

    -- Derived once at write time, stored, and indexed below.
    --
    -- The lon/lat argument order is not a typo: ST_MakePoint takes X then
    -- Y, which is longitude then latitude. Reversing them is the classic
    -- PostGIS mistake and would place every satellite in the wrong
    -- hemisphere while still producing perfectly valid-looking geometry.
    geo_point geography(Point, 4326)
        GENERATED ALWAYS AS (
            ST_SetSRID(ST_MakePoint(longitude_deg, latitude_deg), 4326)::geography
        ) STORED,

    -- One position per satellite per instant. Re-running a propagation
    -- for the same timestamp is then an upsert rather than a duplicate.
    PRIMARY KEY (norad_cat_id, snapshot_ts)
);

-- GIST over the geography: this is the index that makes "everything
-- within N km of a point" a range scan rather than a full table scan.
CREATE INDEX IF NOT EXISTS idx_position_snapshot_geo
    ON gold.position_snapshot USING GIST (geo_point);

-- The map always asks for the newest snapshot, so ordering matters more
-- than equality here.
CREATE INDEX IF NOT EXISTS idx_position_snapshot_ts
    ON gold.position_snapshot (snapshot_ts DESC);
