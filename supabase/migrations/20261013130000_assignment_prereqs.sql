-- Prerequisites for the bulk category assignment.
-- Run AFTER 20261013120000_restructure_categories.sql.

begin;

-- 1) Steering stem bearings had no home in the tree (52+ products use this Type).
insert into categories (parent_id, name, slug, sort_order)
select p.id, 'Steering Stem Bearings', 'steering-stem-bearings', 25
from categories p
where p.slug = 'suspension' and p.parent_id is null
  and not exists (select 1 from categories c where c.slug = 'steering-stem-bearings');

-- 2) Review table: one proposed assignment per product. Nothing in `products`
--    changes until the apply script runs.
create table if not exists category_assignment_review (
  product_id           bigint primary key references products(id) on delete cascade,
  title                text,
  current_category_id  bigint,
  type_value           text,      -- eBay "Type" item specific, if any
  title_slug           text,      -- category slug suggested by the title rules
  type_slug            text,      -- category slug suggested by the Type specific
  proposed_slug        text,
  proposed_category_id bigint,
  proposed_part_type   text,
  proposed_universal   boolean not null default false,
  confidence           text not null,  -- high | medium | low | conflict | unmatched
  approved             boolean not null default false,
  created_at           timestamptz not null default now()
);

-- Keep it private: with RLS on and no policies, the public API cannot read it.
-- The SQL Editor (and the service role) still can.
alter table category_assignment_review enable row level security;

commit;
