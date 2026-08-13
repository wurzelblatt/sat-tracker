-- Validity intervals must tile a satellite's timeline with no gaps and
-- no overlaps: each element set's valid_to must be exactly the next
-- one's valid_from.
--
-- This is the core SCD-2 invariant. If it breaks, an "orbit as of time
-- T" lookup either matches nothing (gap) or matches two element sets
-- (overlap), and the failure surfaces far downstream as a wrong
-- satellite position rather than as a loud error here.
--
-- `is distinct from` is deliberate: it treats the final row's NULL
-- valid_to as equal to the absent next valid_from, so the open-ended
-- current interval passes rather than being flagged.

with ordered as (

    select
        norad_cat_id,
        valid_from,
        valid_to,
        lead(valid_from) over (
            partition by norad_cat_id
            order by valid_from
        ) as next_valid_from
    from {{ ref('elset') }}

)

select
    norad_cat_id,
    valid_from,
    valid_to,
    next_valid_from
from ordered
where
    valid_to is distinct from next_valid_from
    -- A closed interval must also move forward in time.
    or (valid_to is not null and valid_to <= valid_from)
