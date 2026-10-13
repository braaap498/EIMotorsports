-- REVIEW QUERIES. Run each one SEPARATELY (the SQL Editor only shows the last result).
-- Read-only: nothing here changes any data.

-- A) How confident are the proposals overall?
select confidence, count(*) as products
from category_assignment_review
group by confidence
order by 2 desc;

-- B) Where would products land? (category x confidence)
select r.proposed_slug, r.confidence, count(*) as products
from category_assignment_review r
group by 1, 2
order by 1, 2;

-- C) Conflicts: the Type specific and the title point to different categories.
select product_id, title, type_value, type_slug, title_slug
from category_assignment_review
where confidence = 'conflict'
order by type_slug, title_slug
limit 200;

-- D) Unmatched: no rule recognized these. Look for patterns worth new rules.
select product_id, title, type_value
from category_assignment_review
where confidence = 'unmatched'
order by random()
limit 100;

-- E) Products that already had a category and would be MOVED (e.g. the 154 in Gaskets).
select cc.name as current_category, r.proposed_slug, r.confidence, count(*) as products
from category_assignment_review r
join categories cc on cc.id = r.current_category_id
where r.current_category_id is distinct from r.proposed_category_id
group by 1, 2, 3
order by 4 desc;

-- F) Spot-check: 5 random titles per proposed category for medium/low confidence.
select proposed_slug, confidence, title
from (
  select proposed_slug, confidence, title,
         row_number() over (partition by proposed_slug, confidence order by random()) as rn
  from category_assignment_review
  where confidence in ('medium', 'low')
) s
where rn <= 5
order by proposed_slug, confidence;

-- G) Products flagged universal by title.
select product_id, title
from category_assignment_review
where proposed_universal
limit 100;
