{#
    Deduplicated current state of the CelesTrak satellite catalogue.

    ── What this layer does, and does not ───────────────────────────
    Cleaning only: collapse the daily landings to one row per object.
    The derived business flags (is_decayed, is_earth_orbiting,
    orbit_regime) live in gold.dim_object. This layer conforms, gold
    interprets.

    ── Why latest wins, where elset keeps earliest ──────────────────
    silver.elset keeps the EARLIEST observation of each element set,
    because repeated fetches re-land byte-identical payloads and
    first-seen is the honest lineage answer. SATCAT is the opposite
    case: CelesTrak REVISES an object row in place, so ops_status_code
    flips from + to D and decay_date appears where it was NULL. The
    newest landing is therefore the authoritative one. Same window
    function as elset, opposite order by, for opposite reasons.

    ── Why no SCD-2 here ────────────────────────────────────────────
    The elset header argues against dbt snapshot because that source is
    immutable: history IS the data, already carried in the epochs. This
    source is mutable and overwrites, so its history genuinely would be
    lost, which makes a snapshot the correct tool if object history is
    ever wanted — the opposite conclusion, reached by the same
    reasoning. Deliberately out of scope for now: gold.dim_object is a
    Type-1 dimension, and the transition that matters most is already
    carried as an attribute in decay_date.
#}

with deduplicated as (

    -- Each daily fetch re-lands the whole ~70,000-object catalogue.
    -- source_file breaks ties within a single ingest_ts so the winner
    -- is deterministic rather than whatever order the planner produces.
    select
        *,
        row_number() over (
            partition by norad_cat_id
            order by ingest_ts desc, source_file desc
        ) as recency_rank

    from {{ ref('stg_celestrak_satcat') }}

)

select
    norad_cat_id,

    object_name,
    object_id,
    object_type,
    ops_status_code,
    owner,

    launch_date,
    launch_site,
    decay_date,

    period_minutes,
    inclination,
    apogee_km,
    perigee_km,
    rcs_m2,

    data_status_code,
    orbit_center,
    orbit_type,

    ingest_ts,
    ingestion_id,
    source,
    source_file

from deduplicated
where recency_rank = 1
