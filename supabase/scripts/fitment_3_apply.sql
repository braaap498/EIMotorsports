-- FITMENT STAGE 3 (v2): write the reviewed parse into your real tables.
-- Only run this after reviewing and fixing things with fitment_2_review.sql.
-- Run the whole block below in one go. It is a single transaction, so if anything errors, nothing is saved.
--
-- Only rows with status = 'ok' (make, model and at least one year found) get fitment.
-- What it does:
--   1) adds any makes that don't exist yet (matched ignoring spaces/punctuation, so "Can Am" = "Can-Am")
--   2) adds one vehicle_models row per make + model (vehicle type from the eBay specifics;
--      Ski-Doo and Sea-Doo default to Snowmobile and PWC)
--   3) adds one vehicles row per model + year
--   4) links each product to every vehicle it fits in product_fitment, with the variant text
--      (e.g. "4X4 ES") saved in the notes column
--   5) marks products with no make (generic shim kits, gear bag) and products titled "universal"
--      as is_universal
-- It never deletes or changes existing rows (other than turning is_universal on), and it is safe to re-run.

begin;

-- 1) Makes
insert into vehicle_makes (name, slug)
select m.make_name,
       btrim(lower(regexp_replace(m.make_name, '[^A-Za-z0-9]+', '-', 'g')), '-')
from (select distinct make_name from fitment_parse_review where status = 'ok') m
where not exists (
  select 1 from vehicle_makes vm
  where fitment_key(vm.name) = fitment_key(m.make_name)
);

-- Working copy: each parsed product with its make id
create temp table fitment_ok on commit drop as
select r.product_id, r.model_raw, r.model_key, r.submodel_raw, r.years, r.spec_vehicle_type_slug, vm.id as make_id
from fitment_parse_review r
join vehicle_makes vm on fitment_key(vm.name) = fitment_key(r.make_name)
where r.status = 'ok';

-- 2) Models
insert into vehicle_models (make_id, vehicle_type_id, name, slug)
select p.make_id,
       (select vt.id from vehicle_types vt where vt.slug = p.type_slug),
       p.model_name,
       case when p.rn > 1
              or exists (select 1 from vehicle_models x where x.slug = p.base_slug)
            then p.base_slug || '-' || p.make_slug
            else p.base_slug
       end
from (
  select mi.*,
         btrim(lower(regexp_replace(mi.model_name, '[^A-Za-z0-9]+', '-', 'g')), '-') as base_slug,
         row_number() over (
           partition by btrim(lower(regexp_replace(mi.model_name, '[^A-Za-z0-9]+', '-', 'g')), '-')
           order by mi.make_id
         ) as rn
  from (
    select o.make_id,
           o.model_key,
           mode() within group (order by o.model_raw) as model_name,
           coalesce(
             mode() within group (order by o.spec_vehicle_type_slug),
             case fitment_key(mk.name)
               when 'SKIDOO' then 'snowmobile'
               when 'SEADOO' then 'pwc'
             end
           ) as type_slug,
           mk.slug as make_slug
    from fitment_ok o
    join vehicle_makes mk on mk.id = o.make_id
    group by o.make_id, o.model_key, mk.name, mk.slug
  ) mi
) p
where not exists (
  select 1 from vehicle_models vmo
  where vmo.make_id = p.make_id
    and fitment_key(vmo.name) = p.model_key
);

-- Every (product, model, year) combination, carrying the variant note
create temp table fitment_pairs on commit drop as
select o.product_id, vmo.id as model_id, y::smallint as year, o.submodel_raw
from fitment_ok o
join vehicle_models vmo
  on vmo.make_id = o.make_id
 and fitment_key(vmo.name) = o.model_key
cross join lateral unnest(o.years) as y;

-- 3) Vehicles (model + year)
insert into vehicles (model_id, year)
select distinct fp.model_id, fp.year
from fitment_pairs fp
where not exists (
  select 1 from vehicles v
  where v.model_id = fp.model_id and v.year = fp.year and v.submodel is null
);

-- 4) Product fitment
insert into product_fitment (product_id, vehicle_id, notes)
select distinct on (fp.product_id, v.id) fp.product_id, v.id, fp.submodel_raw
from fitment_pairs fp
join vehicles v on v.model_id = fp.model_id and v.year = fp.year and v.submodel is null
where not exists (
  select 1 from product_fitment pf
  where pf.product_id = fp.product_id and pf.vehicle_id = v.id
);

-- 5) Universal: products with no make at all, or "universal" in the title
update products
set is_universal = true
where id in (
  select product_id from fitment_parse_review where status in ('no_make', 'universal')
);

commit;

-- AFTER: run these separately to check the result.
--
-- select 'makes' as tbl, count(*) from vehicle_makes
-- union all select 'models', count(*) from vehicle_models
-- union all select 'vehicles', count(*) from vehicles
-- union all select 'product_fitment', count(*) from product_fitment;
--
-- Products that now fit at least one vehicle:
-- select count(distinct product_id) from product_fitment;
--
-- Models still without a vehicle type:
-- select vm.name as make, m.name as model from vehicle_models m
-- join vehicle_makes vm on vm.id = m.make_id where m.vehicle_type_id is null order by 1, 2;
