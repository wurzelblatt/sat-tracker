{#
    Type the bronze SATCAT landing.

    Same contract as stg_celestrak_gp: one row in, one row out, renaming
    and casting only. Collapsing the daily landings to one row per object
    is a business decision and lives in gold.dim_object, exactly as
    historisation lives in silver.elset rather than here.

    Casts are hard, for the reason given in stg_celestrak_gp: a CelesTrak
    format change should be a red build, not thousands of silently-null
    columns. Precisions were sized against the real 70,270-row catalogue
    rather than guessed:
      - apogee reaches 4,082,876 km (lunar and highly eccentric orbits)
        and carries no decimal places, so integer is safe and honest
      - period reaches 486,100.22 minutes, about 337 days
      - rcs carries up to four decimal places
      - launch_date runs from 1957-10-04 (Sputnik 1) to the present

    NULL is meaningful throughout. The ingest reads empty CSV fields as
    NULL, so `decay_date is null` means "still in orbit" rather than
    "unknown", and a null apogee means SATCAT publishes no orbit for the
    object at all.
#}

select
    -- Business key
    norad_cat_id::integer                     as norad_cat_id,

    -- Identity
    object_name::text                         as object_name,
    object_id::text                           as object_id,
    object_type::text                         as object_type,
    ops_status_code::text                     as ops_status_code,
    owner::text                               as owner,

    -- Lifecycle
    launch_date::date                         as launch_date,
    launch_site::text                         as launch_site,
    decay_date::date                          as decay_date,

    -- Orbit as SATCAT summarises it. Note this is CelesTrak's own
    -- summary, not something derived from the element sets in silver.
    period::numeric(10, 2)                    as period_minutes,
    inclination::numeric(6, 2)                as inclination,
    apogee::integer                           as apogee_km,
    perigee::integer                          as perigee_km,
    rcs::numeric(8, 4)                        as rcs_m2,

    -- Classification
    data_status_code::text                    as data_status_code,
    orbit_center::text                        as orbit_center,
    orbit_type::text                          as orbit_type,

    -- Lineage
    ingest_ts                                 as ingest_ts,
    ingestion_id                              as ingestion_id,
    source                                    as source,
    source_file                               as source_file,
    target                                    as target

from {{ source('bronze', 'raw_satcat') }}
