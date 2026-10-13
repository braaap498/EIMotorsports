-- Front/rear bumpers showed up among products the rules couldn't place.
-- Pros file them with crash/armor protection, so they go under Crash Protection.
-- Run BEFORE re-running category_assignment_1_propose.sql.

insert into categories (parent_id, name, slug, sort_order)
select p.id, 'Bumpers & Brush Guards', 'bumpers-brush-guards', 60
from categories p
where p.slug = 'crash-protection' and p.parent_id is null
  and not exists (select 1 from categories c where c.slug = 'bumpers-brush-guards');
