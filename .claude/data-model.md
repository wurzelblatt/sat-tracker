# Data Model — Bronze/Silver/Gold

## Bronze Layer (raw_gp)
Immutable, raw OMM/CSV files as delivered by Celestrak.

**Parquet structure:**
```
ingest_ts (TIMESTAMP)
source (STRING: 'celestrak' | 'spacetrack')
source_file (STRING)
norad_cat_id (STRING)
object_name (STRING)
epoch_year (DOUBLE)
epoch_day (DOUBLE)
mean_motion (DOUBLE)
mean_motion_dot (DOUBLE)
mean_motion_ddot (DOUBLE)
eccentricity (DOUBLE)
inclination (DOUBLE)
ra_of_asc_node (DOUBLE)
arg_of_pericenter (DOUBLE)
mean_anomaly (DOUBLE)
bstar (DOUBLE)
element_set_no (INT)
rev_at_epoch (INT)
tle_line1 (STRING)
tle_line2 (STRING)
```

Partitioned by: `ingest_date=YYYY-MM-DD/hour=HH/`

---

## Silver Layer (elset)
Normalized, deduplicated element sets with SCD-Type-2 historization.

```sql
CREATE TABLE silver.elset (
  -- Business Key
  norad_cat_id INTEGER NOT NULL,
  epoch TIMESTAMP NOT NULL,
  
  -- Orbital Parameters
  object_name VARCHAR(50),
  mean_motion NUMERIC(10, 8),
  eccentricity NUMERIC(10, 8),
  inclination NUMERIC(10, 8),
  ra_of_asc_node NUMERIC(10, 8),
  arg_of_pericenter NUMERIC(10, 8),
  mean_anomaly NUMERIC(10, 8),
  mean_motion_dot NUMERIC(15, 12),
  bstar NUMERIC(15, 12),
  element_set_no INTEGER,
  rev_at_epoch INTEGER,
  
  -- SCD-2 (Slowly Changing Dimension)
  valid_from TIMESTAMP NOT NULL,
  valid_to TIMESTAMP,
  is_current BOOLEAN DEFAULT TRUE,
  
  -- Metadata
  ingest_ts TIMESTAMP,
  source VARCHAR(20),
  
  PRIMARY KEY (norad_cat_id, epoch)
);

CREATE INDEX idx_elset_current ON silver.elset(is_current, norad_cat_id);
CREATE INDEX idx_elset_epoch ON silver.elset(epoch DESC);
```

---

## Gold Layer

### 1. dim_object (Dimension Table)
Static satellite metadata (join point with Celestrak SATCAT).

```sql
CREATE TABLE gold.dim_object (
  norad_cat_id INTEGER PRIMARY KEY,
  object_name VARCHAR(100),
  object_type VARCHAR(10),  -- 'PAY', 'R', 'DEB', 'UNKNOWN'
  country VARCHAR(5),       -- ISO code
  launch_date DATE,
  decay_date DATE,
  rcs_size_m2 NUMERIC(10, 2),
  operational_status VARCHAR(20),
  update_ts TIMESTAMP
);
```

### 2. fact_latest_elset
Newest element set per satellite (materialized view).

```sql
CREATE VIEW gold.fact_latest_elset AS
SELECT 
  se.norad_cat_id,
  se.object_name,
  se.epoch,
  se.mean_motion,
  se.eccentricity,
  se.inclination,
  se.ra_of_asc_node,
  se.arg_of_pericenter,
  se.mean_anomaly,
  se.bstar,
  se.tle_line1,
  se.tle_line2
FROM silver.elset se
WHERE se.is_current = TRUE;
```

### 3. position_snapshot
Propagated positions at last ingest, WGS84 coordinates.

```sql
CREATE TABLE gold.position_snapshot (
  norad_cat_id INTEGER NOT NULL,
  snapshot_ts TIMESTAMP NOT NULL,
  latitude NUMERIC(10, 6),
  longitude NUMERIC(10, 6),
  altitude_km NUMERIC(10, 2),
  velocity_kms NUMERIC(10, 4),
  geo_point GEOGRAPHY(Point, 4326),  -- PostGIS
  
  PRIMARY KEY (norad_cat_id, snapshot_ts)
);

CREATE INDEX idx_position_geo ON gold.position_snapshot USING GIST(geo_point);
```

### 4. conjunction_candidates
Collision screening results (from SOCRATES or custom APSIS filter).

```sql
CREATE TABLE gold.conjunction_candidates (
  norad_id_1 INTEGER NOT NULL,
  norad_id_2 INTEGER NOT NULL,
  tca TIMESTAMP,
  miss_distance_km NUMERIC(10, 2),
  max_pc NUMERIC(10, 8),  -- Collision Probability
  source VARCHAR(20),  -- 'socrates' | 'custom_apsis'
  detected_at TIMESTAMP,
  
  PRIMARY KEY (norad_id_1, norad_id_2, tca)
);

CREATE INDEX idx_conjunction_pc ON gold.conjunction_candidates(max_pc DESC);
```

---

## Partitioning Strategy

- **Bronze:** `ingest_date/hour/` (daily partitions, hourly sub-partitions)
- **Silver:** `epoch_date` (partition by epoch date)
- **Gold:** `snapshot_date` or unpartitioned (small enough)

---

## dbt Tests (Silver Layer)

```yaml
# models/silver/elset.yml
version: 2
models:
  - name: elset
    columns:
      - name: norad_cat_id
        tests:
          - not_null
      - name: eccentricity
        tests:
          - dbt_utils.expression_is_true:
              expression: "eccentricity >= 0 AND eccentricity < 1"
      - name: inclination
        tests:
          - dbt_utils.expression_is_true:
              expression: "inclination >= 0 AND inclination <= 180"
      - name: mean_motion
        tests:
          - dbt_utils.expression_is_true:
              expression: "mean_motion > 0"
          - dbt_utils.expression_is_true:
              expression: "mean_motion < 20"
    tests:
      - dbt_utils.recency:
          datepart: hour
          interval: 3
          select_from_fieldname: epoch
```

---

## Summary

| Layer | Unique Key | Partitioning |
|-------|-----------|--------------|
| Bronze | (source, source_file, norad_cat_id) | ingest_date/hour |
| Silver | (norad_cat_id, epoch) | epoch_date |
| Gold (dim) | norad_cat_id | — |
| Gold (snapshot) | (norad_cat_id, snapshot_ts) | snapshot_date |
| Gold (conj) | (norad_id_1, norad_id_2, tca) | — |
