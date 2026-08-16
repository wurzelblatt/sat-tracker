{{ config(materialized='view') }}

{#
    The serving contract for propagation: one row per satellite that can
    actually be propagated right now.

    ── Why a view, where the rest of gold is a table ────────────────
    It reads two tables that are both rebuilt on every dbt run, and it
    must never serve a stale pairing of current elements with catalogue
    state. Materialising it would add a third thing to keep in step for
    no gain: the join is between two indexed tables and costs
    milliseconds.

    ── What the filters mean ────────────────────────────────────────
    is_current        the newest element set per satellite, from
                      silver.elset
    not is_decayed    the object has not re-entered. Eight Starlinks
                      left the catalogue between two GP fetches a week
                      apart, and their final element sets are still in
                      silver looking perfectly valid
    is_earth_orbiting SGP4 is defined only for Earth orbit, and the
                      catalogue includes solar, lunar, planetary and
                      Lagrange objects

    16,352 satellites carry current element sets; 16,344 survive these
    filters. A later view wanting decayed objects at their last known
    position should be a SIBLING of this model rather than a loosening
    of it — the name here is a promise about what is safe to propagate.

    ── Column choice ────────────────────────────────────────────────
    The element set only. Object attributes stay in dim_object, one join
    away: copying object_type or orbit_regime in here is how a fact and
    its dimension drift apart. The list is shaped by what
    sgp4.omm.initialize consumes, since that is the only reader.

    Note both sources carry an inclination. The element set value is the
    authoritative one for propagation at numeric(12, 8); the SATCAT
    column is a rounded catalogue summary at numeric(6, 2). This model
    takes the element set.
#}

select
    e.norad_cat_id,
    e.epoch,

    -- Identity, carried so downstream output is readable without a join
    e.object_name,
    e.object_id,
    e.classification_type,

    -- The SGP4 element set proper
    e.mean_motion,
    e.eccentricity,
    e.inclination,
    e.ra_of_asc_node,
    e.arg_of_pericenter,
    e.mean_anomaly,
    e.mean_motion_dot,
    e.mean_motion_ddot,
    e.bstar,

    e.ephemeris_type,
    e.element_set_no,
    e.rev_at_epoch,

    -- Lineage of the element set this row serves
    e.ingest_ts,
    e.source_file

from {{ ref('elset') }} e
join {{ ref('dim_object') }} d
    on e.norad_cat_id = d.norad_cat_id
where e.is_current
    and not d.is_decayed
    and d.is_earth_orbiting
