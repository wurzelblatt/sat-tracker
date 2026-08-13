{#
    Type the bronze landing.

    Bronze stores every CelesTrak column as TEXT so that nothing is
    coerced or lost on the way in. This model is where the data becomes
    typed, and therefore where bad data becomes visible.

    Casts are deliberately plain. A safe-cast wrapper returning NULL on
    failure would turn a CelesTrak format change into thousands of
    silently-null orbital elements; a hard cast turns it into a red
    build. That matches the fail-fast stance the ingest client already
    takes on HTTP errors.

    Angular columns are NUMERIC(12, 8), not the NUMERIC(10, 8) sketched
    in .claude/data-model.md: 10,8 permits only two integer digits
    (max 99.99999999), but ra_of_asc_node, arg_of_pericenter and
    mean_anomaly all reach ~360, and inclination reaches 180.
#}

select
    -- Business key
    norad_cat_id::integer                     as norad_cat_id,
    epoch::timestamptz                        as epoch,

    -- Identity
    object_name::text                         as object_name,
    object_id::text                           as object_id,
    classification_type::text                 as classification_type,

    -- Orbital elements
    mean_motion::numeric(12, 8)               as mean_motion,
    eccentricity::numeric(12, 10)             as eccentricity,
    inclination::numeric(12, 8)               as inclination,
    ra_of_asc_node::numeric(12, 8)            as ra_of_asc_node,
    arg_of_pericenter::numeric(12, 8)         as arg_of_pericenter,
    mean_anomaly::numeric(12, 8)              as mean_anomaly,
    mean_motion_dot::numeric(15, 12)          as mean_motion_dot,
    mean_motion_ddot::numeric(15, 12)         as mean_motion_ddot,
    bstar::numeric(15, 12)                    as bstar,

    -- Bookkeeping from the element set itself
    ephemeris_type::integer                   as ephemeris_type,
    element_set_no::integer                   as element_set_no,
    rev_at_epoch::integer                     as rev_at_epoch,

    -- Lineage
    ingest_ts                                 as ingest_ts,
    ingestion_id                              as ingestion_id,
    source                                    as source,
    source_file                               as source_file,
    target                                    as target

from {{ source('bronze', 'raw_gp') }}
