-- STAGE 3: approve and apply. Only run after reviewing with the review queries.
-- Run STEP 1 (and any optional approvals), then STEP 2. Run each step by itself.

-- STEP 1: approve the high-confidence rows (Type specific alone, or Type and title agree).
update category_assignment_review
set approved = true
where confidence = 'high';

-- OPTIONAL approvals. Uncomment and run once you've checked them in the review queries:
--
-- update category_assignment_review set approved = true where confidence = 'medium';
-- update category_assignment_review set approved = true where confidence = 'low';
--
-- Approve or reject individual products (use the product_id from the review lists):
-- update category_assignment_review set approved = true  where product_id in (101, 102);
-- update category_assignment_review set approved = false where product_id in (103);
--
-- Send everything unrecognized to Miscellaneous (do this last, after you've looked at query D):
-- update category_assignment_review set approved = true where confidence = 'unmatched';
--
-- Conflicts are never auto-approved. Approve them by product_id after you've checked query C.

-- STEP 2: apply every approved row to products.
--   category_id  <- proposed category
--   part_type    <- the eBay Type value, only where part_type is currently empty
--   is_universal <- true if the title says "universal" (never flips a true back to false)
begin;

update products p
set category_id  = r.proposed_category_id,
    part_type    = coalesce(p.part_type, r.proposed_part_type),
    is_universal = p.is_universal or r.proposed_universal
from category_assignment_review r
where r.product_id = p.id
  and r.approved
  and r.proposed_category_id is not null;

commit;

-- AFTER: how many products are still uncategorized? (should drop to 0 as you approve more)
-- select count(*) from products where category_id is null;
