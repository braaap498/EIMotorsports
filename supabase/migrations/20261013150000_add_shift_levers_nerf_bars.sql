-- Shift levers are ~550 products, too many to sit inside "Footpegs & Foot Controls".
-- Nerf bars came in with the new (non-eBay) products. Run BEFORE re-running
-- category_assignment_1_propose.sql.

insert into categories (parent_id, name, slug, sort_order)
select p.id, v.name, v.slug, v.ord
from (values
  ('controls',   'Shift Levers', 'shift-levers', 25),
  ('body-frame', 'Nerf Bars',    'nerf-bars',    60)
) as v(parent_slug, name, slug, ord)
join categories p on p.slug = v.parent_slug and p.parent_id is null
where not exists (select 1 from categories c where c.slug = v.slug);
