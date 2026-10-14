-- FITMENT STAGE 7: seed the catalog from the eBay compatibility data.
-- Run after fitment_5 (map settled) and fitment_6 (audit reviewed). Part 1 is one transaction: if anything errors, nothing is saved.
-- Safe to re-run. Part 1 only ADDS and labels; it never deletes. Deleting is parts 2 and 3, run separately and optional.
--
-- Part 1 does:
--   0) adds product_fitment.source ('ebay' = confirmed by eBay, 'title' = parsed from the title only,
--      'title_inferred' = the "possible fit" rows from stage 4) and labels the rows you already have
--   1) creates makes/models for eBay models you flagged create_new in the map (vehicle type left empty)
--   2) adds the missing vehicles (model + year)
--   3) links each product to every model+year eBay lists for it (eBay submodels go in notes)
--   4) marks rows that eBay confirms as source = 'ebay'

begin;

-- 0) provenance
alter table product_fitment add column if not exists source text;
update product_fitment
set source = case when notes like 'possible fit%' then 'title_inferred' else 'title' end
where source is null;

-- 1a) makes flagged create_new that don't exist yet
insert into vehicle_makes (name, slug)
select m.make_name, btrim(lower(regexp_replace(m.make_name, '[^A-Za-z0-9]+', '-', 'g')), '-')
from (select distinct make_key, make_name from fitment_ebay_model_map where create_new and model_id is null) m
where not exists (select 1 from vehicle_makes vm where fitment_key(vm.name) = m.make_key);

-- 1b) models flagged create_new
insert into vehicle_models (make_id, name, slug)
select vm.id, m.model_name,
       case when exists (select 1 from vehicle_models x where x.slug = btrim(lower(regexp_replace(m.model_name, '[^A-Za-z0-9]+', '-', 'g')), '-'))
            then btrim(lower(regexp_replace(m.model_name, '[^A-Za-z0-9]+', '-', 'g')), '-') || '-' || vm.slug
            else btrim(lower(regexp_replace(m.model_name, '[^A-Za-z0-9]+', '-', 'g')), '-')
       end
from fitment_ebay_model_map m
join vehicle_makes vm on fitment_key(vm.name) = m.make_key
where m.create_new and m.model_id is null
  and not exists (select 1 from vehicle_models x where x.make_id = vm.id and fitment_key(x.name) = m.model_key);

update fitment_ebay_model_map m
set model_id = vmo.id
from vehicle_makes vm
join vehicle_models vmo on vmo.make_id = vm.id
where m.model_id is null and m.create_new
  and fitment_key(vm.name) = m.make_key and fitment_key(vmo.name) = m.model_key;

-- eBay rows resolved to one of your models
create temp table ebay_pairs on commit drop as
select f.product_id, m.model_id, f.year::smallint as year,
       nullif(left(string_agg(distinct f.submodel, ', ' order by f.submodel), 400), '') as subs
from ebay_compat_flat f
join fitment_ebay_model_map m on m.make_key = fitment_key(f.make_name) and m.model_key = fitment_key(f.model_name)
where m.model_id is not null
group by f.product_id, m.model_id, f.year;

-- 2) vehicles (model + year)
insert into vehicles (model_id, year)
select distinct ep.model_id, ep.year
from ebay_pairs ep
where not exists (select 1 from vehicles v where v.model_id = ep.model_id and v.year = ep.year and v.submodel is null);

-- 3) product fitment
insert into product_fitment (product_id, vehicle_id, notes, source)
select distinct on (ep.product_id, v.id)
       ep.product_id, v.id,
       case when ep.subs is not null then 'ebay: ' || ep.subs end,
       'ebay'
from ebay_pairs ep
join vehicles v on v.model_id = ep.model_id and v.year = ep.year and v.submodel is null
where not exists (select 1 from product_fitment pf where pf.product_id = ep.product_id and pf.vehicle_id = v.id);

-- 4) rows that already existed and that eBay confirms
update product_fitment pf
set source = 'ebay'
from vehicles v, ebay_pairs ep
where v.id = pf.vehicle_id
  and ep.product_id = pf.product_id and ep.model_id = v.model_id and ep.year = v.year
  and pf.source is distinct from 'ebay';

commit;

-- AFTER: re-run fitment_6_audit.sql, then its query A. Expect match / title_only to dominate.
--   select source, count(*) from product_fitment group by 1;

-- =====================================================================================================
-- PART 2 (optional, run on its own): drop stage-4 "possible fit" guesses for products that now have real eBay fitment.
-- PREVIEW:
--   select count(*) from product_fitment pf
--   where pf.source = 'title_inferred'
--     and exists (select 1 from product_fitment x where x.product_id = pf.product_id and x.source = 'ebay');
-- APPLY:
--   delete from product_fitment pf
--   where pf.source = 'title_inferred'
--     and exists (select 1 from product_fitment x where x.product_id = pf.product_id and x.source = 'ebay');

-- =====================================================================================================
-- PART 3 (optional, run on its own, only AFTER re-running fitment_6): drop title-derived rows that eBay does not list,
-- for products whose eBay data is fully mapped. This is what removes wrong fits. Look at the preview and sample it by hand first.
-- PREVIEW (rows that would go, by audit status):
--   select a.status, count(*) as rows_to_delete, count(distinct pf.product_id) as products
--   from product_fitment pf
--   join fitment_audit a on a.product_id = pf.product_id
--   join vehicles v on v.id = pf.vehicle_id
--   where pf.source = 'title'
--     and a.status in ('wrong_model', 'extra_years', 'year_mismatch')
--     and not exists (select 1 from ebay_compat_flat f
--                     join fitment_ebay_model_map m on m.make_key = fitment_key(f.make_name) and m.model_key = fitment_key(f.model_name)
--                     where f.product_id = pf.product_id and m.model_id = v.model_id and f.year = v.year)
--   group by 1;
-- APPLY (same conditions):
--   delete from product_fitment pf
--   using fitment_audit a, vehicles v
--   where a.product_id = pf.product_id and v.id = pf.vehicle_id
--     and pf.source = 'title'
--     and a.status in ('wrong_model', 'extra_years', 'year_mismatch')
--     and not exists (select 1 from ebay_compat_flat f
--                     join fitment_ebay_model_map m on m.make_key = fitment_key(f.make_name) and m.model_key = fitment_key(f.model_name)
--                     where f.product_id = pf.product_id and m.model_id = v.model_id and f.year = v.year);
