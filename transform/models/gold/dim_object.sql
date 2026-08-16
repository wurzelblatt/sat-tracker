{#
    One row per catalogued space object — the descriptive dimension the
    map and the fact tables join against.

    Holds the FULL catalogue (~70,000 objects), not only those carrying
    element sets. A dimension filtered to whatever the fact happens to
    contain stops being a dimension: an object that decays after its last
    element set was published must still resolve, or the fact ends up
    with a key pointing at nothing. That is not hypothetical here — eight
    Starlinks re-entered between two GP fetches a week apart and had
    already left CelesTraks active list by the second one.

    Deduplication happens upstream in silver.space_object, which is why
    this model is a flat select. What gold adds is interpretation: three
    derived flags that turn catalogue attributes into decisions the
    propagation step and the map can act on.
#}

select
    norad_cat_id,

    -- Identity
    object_name,
    object_id,
    object_type,
    ops_status_code,
    owner,

    -- Lifecycle
    launch_date,
    launch_site,
    decay_date,

    -- Orbit as SATCAT summarises it
    period_minutes,
    inclination,
    apogee_km,
    perigee_km,
    rcs_m2,
    orbit_center,
    orbit_type,

    -- Propagation gate 1: the object physically no longer exists. This
    -- is a correctness gate, not a confidence one — a re-entered object
    -- has no position to plot, however fresh its last element set was.
    (decay_date is not null) as is_decayed,

    -- Propagation gate 2: SGP4 is defined only for Earth orbit, and the
    -- catalogue is not exclusively Earth-orbiting: 416 objects orbit the
    -- Sun, Moon, Mars, Venus or Jupiter, or sit at a Lagrange point.
    -- 'EA' is Earth. A NUMERIC orbit_center is a NORAD ID, meaning the
    -- object is docked to a host that is itself in Earth orbit (15 such
    -- objects carry element sets today, all orbit_type 'DOC'), so a
    -- plain orbit_center = 'EA' test would wrongly exclude them.
    (orbit_center = 'EA' or orbit_center ~ '^[0-9]+$') as is_earth_orbiting,

    -- Orbital regime, taken from the SATCAT apogee rather than from the
    -- element sets, so it is available even for objects with no current
    -- elements. Regime is what makes epoch age interpretable: MEO
    -- satellites are ~40x more likely than LEO ones to carry an element
    -- set older than 48 hours (21% vs 0.5%), because negligible drag
    -- makes their orbits predictable and operators republish far less
    -- often. A flat staleness threshold would wrongly flag a fifth of
    -- the Galileo constellation.
    case
        when apogee_km is null then 'unknown'
        when apogee_km < 2000 then 'LEO'
        when apogee_km < 35000 then 'MEO'
        else 'GEO/HEO'
    end as orbit_regime,

    -- Lineage: which landing this version of the row came from
    ingest_ts,
    source_file

from {{ ref('space_object') }}
