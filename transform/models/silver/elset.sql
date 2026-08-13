{#
    Normalised, deduplicated, SCD-2 historised orbital element sets.

    ── Why no dbt snapshot ──────────────────────────────────────────
    dbt snapshots exist to reconstruct history that a MUTABLE source
    fails to record. This source is not mutable. CelesTrak never
    revises the element set for a given (satellite, epoch); it
    publishes a NEW element set with a NEW epoch. History is not lost
    and awaiting reconstruction — history IS the data.

    So every SCD-2 column here is a pure function of rows already
    present, computed with window functions. This is not a shortcut,
    it is more correct: a snapshots dbt_valid_from records when WE
    FETCHED, whereas validity is physically defined by when the orbit
    was MEASURED (epoch). Snapshotting would conflate the two, and a
    missed pipeline run would silently corrupt the intervals.

    Consequence worth knowing: this model is fully rebuildable from
    bronze at any time and always converges to the same answer.

    ── Why table, not incremental ───────────────────────────────────
    lead() means a newly arriving epoch MUTATES the previous rows
    valid_to for that satellite. That is not an append, so incremental
    logic would have to reprocess each affected satellites tail — a
    classic source of silent correctness bugs. At realistic volume
    (~30k satellites x ~3 epochs/day) a full rebuild is seconds.
#}

with deduplicated as (

    -- Every 2h fetch re-lands the whole constellation, but CelesTrak
    -- only publishes new element sets a few times a day, so the same
    -- (norad_cat_id, epoch) arrives many times over. Keep the earliest
    -- observation: the payloads are identical, and first-seen is the
    -- honest lineage answer to "when did this enter our system?".
    select
        *,
        row_number() over (
            partition by norad_cat_id, epoch
            order by ingest_ts
        ) as observation_rank
    from {{ ref('stg_celestrak_gp') }}

),

historised as (

    select
        norad_cat_id,
        epoch,

        object_name,
        object_id,
        classification_type,

        mean_motion,
        eccentricity,
        inclination,
        ra_of_asc_node,
        arg_of_pericenter,
        mean_anomaly,
        mean_motion_dot,
        mean_motion_ddot,
        bstar,

        ephemeris_type,
        element_set_no,
        rev_at_epoch,

        -- SCD-2: an element set is authoritative from its own epoch
        -- until the epoch of the next one for the same satellite.
        epoch as valid_from,
        lead(epoch) over (
            partition by norad_cat_id
            order by epoch
        ) as valid_to,

        ingest_ts,
        ingestion_id,
        source,
        source_file

    from deduplicated
    where observation_rank = 1

)

select
    *,
    -- The newest element set for a satellite is the one with no
    -- successor. Derived rather than stored so it can never drift out
    -- of step with valid_to.
    valid_to is null as is_current
from historised
