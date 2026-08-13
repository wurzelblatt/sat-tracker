-- Every satellite must have exactly one current element set.
--
-- Zero would mean the lead() partition is wrong or a satellite's newest
-- row was dropped; more than one would mean duplicate epochs survived
-- deduplication. Both are silent corruptions — the table still looks
-- fine, but "give me the current orbit" starts returning the wrong
-- number of rows, which would quietly break SGP4 propagation downstream.

select
    norad_cat_id,
    count(*) as current_row_count
from {{ ref('elset') }}
where is_current
group by norad_cat_id
having count(*) <> 1
