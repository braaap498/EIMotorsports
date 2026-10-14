-- FITMENT STAGE 6: audit every product's current fitment against what eBay lists for it.
-- Changes NOTHING in your catalog; it rebuilds one table, fitment_audit (one row per product). Safe to re-run any time.
-- Run fitment_5_ebay_flatten.sql first and settle the model map as far as you want to.
--
-- Rule used: where eBay has compatibility data for a listing, eBay is the reference (you curated it and eBay validates it);
-- title-parsed fitment is the thing being checked.
--
-- STATUS meanings
--   match                 same model+years as eBay
--   missing_years         eBay lists years the product lacks (nothing extra)
--   extra_years           product has years eBay does not list (same models)
--   year_mismatch         both of the above
--   wrong_model           product is linked to a model eBay does not list for it at all  <- the serious one
--   needs_model_mapping   some eBay models are not mapped yet (finish the map, re-run)
--   title_only            eBay has no compatibility data for it; title-parsed fitment stands, unverified
--   universal             flagged universal, no vehicle-specific fitment
--   no_fitment            no eBay data and nothing from titles

drop table if exists fitment_audit;

create table fitment_audit as
with exp as (   -- what eBay says: product x model x year
  select distinct f.product_id, m.model_id, f.year
  from ebay_compat_flat f
  join fitment_ebay_model_map m
    on m.make_key = fitment_key(f.make_name) and m.model_key = fitment_key(f.model_name)
  where m.model_id is not null
),
cur as (        -- what the catalog says now
  select distinct pf.product_id, v.model_id, v.year
  from product_fitment pf
  join vehicles v on v.id = pf.vehicle_id
),
ebay_n as (
  select product_id, count(*) as n from ebay_compat_flat group by 1
),
unmapped as (   -- eBay rows we can't place yet (skipped models don't count)
  select f.product_id, count(*) as n
  from ebay_compat_flat f
  left join fitment_ebay_model_map m
    on m.make_key = fitment_key(f.make_name) and m.model_key = fitment_key(f.model_name)
  where m.model_id is null and not coalesce(m.skip, false)
  group by 1
),
cmp as (
  select coalesce(e.product_id, c.product_id) as product_id,
         count(*) filter (where e.product_id is not null and c.product_id is not null) as matched,
         count(*) filter (where c.product_id is null) as missing,
         count(*) filter (where e.product_id is null) as extra
  from exp e
  full join cur c on c.product_id = e.product_id and c.model_id = e.model_id and c.year = e.year
  group by 1
),
bad_models as ( -- linked models eBay never lists for this product
  select c.product_id, count(distinct c.model_id) as n
  from cur c
  where not exists (select 1 from exp e where e.product_id = c.product_id and e.model_id = c.model_id)
  group by 1
),
cur_n as (
  select product_id, count(*) as n from cur group by 1
)
select p.id as product_id,
       p.title,
       coalesce(en.n, 0)  as ebay_rows,
       coalesce(um.n, 0)  as ebay_rows_unmapped,
       coalesce(cn.n, 0)  as current_rows,
       coalesce(cm.matched, 0) as matched,
       coalesce(cm.missing, 0) as missing,
       coalesce(cm.extra, 0)   as extra,
       coalesce(bm.n, 0)  as wrong_models,
       case
         when coalesce(en.n, 0) = 0 then
           case when coalesce(cn.n, 0) > 0 then 'title_only'
                when p.is_universal        then 'universal'
                else 'no_fitment' end
         when coalesce(um.n, 0) > 0 then 'needs_model_mapping'
         when coalesce(cm.missing, 0) = 0 and coalesce(cm.extra, 0) = 0 then 'match'
         when coalesce(bm.n, 0) > 0 then 'wrong_model'
         when coalesce(cm.extra, 0) = 0 then 'missing_years'
         when coalesce(cm.missing, 0) = 0 then 'extra_years'
         else 'year_mismatch'
       end as status
from products p
left join ebay_n   en on en.product_id = p.id
left join unmapped um on um.product_id = p.id
left join cur_n    cn on cn.product_id = p.id
left join cmp      cm on cm.product_id = p.id
left join bad_models bm on bm.product_id = p.id;

create index on fitment_audit (status);

-- ===== REVIEW (run each on its own) =====

-- A) The scoreboard
--   select status, count(*) as products from fitment_audit group by 1 order by 2 desc;

-- B) Wrong model: the product is attached to a model eBay doesn't list for it. Fix these first.
--   select a.product_id, a.title,
--          (select string_agg(distinct vm.name || ' ' || mo.name, ', ')
--             from product_fitment pf join vehicles v on v.id = pf.vehicle_id
--             join vehicle_models mo on mo.id = v.model_id join vehicle_makes vm on vm.id = mo.make_id
--            where pf.product_id = a.product_id) as current_models,
--          (select string_agg(distinct f.make_name || ' ' || f.model_name, ', ')
--             from ebay_compat_flat f where f.product_id = a.product_id) as ebay_models
--   from fitment_audit a where a.status = 'wrong_model' order by a.product_id limit 100;

-- C) Year differences for one product (replace 12345): what is missing and what is extra
--   with exp as (select distinct f.product_id, m.model_id, f.year from ebay_compat_flat f
--                join fitment_ebay_model_map m on m.make_key = fitment_key(f.make_name) and m.model_key = fitment_key(f.model_name)
--                where f.product_id = 12345 and m.model_id is not null),
--        cur as (select distinct pf.product_id, v.model_id, v.year from product_fitment pf join vehicles v on v.id = pf.vehicle_id
--                where pf.product_id = 12345)
--   select mo.name as model, coalesce(e.year, c.year) as year,
--          case when c.year is null then 'MISSING (eBay only)' when e.year is null then 'EXTRA (title only)' else 'both' end as where_found
--   from exp e full join cur c on c.model_id = e.model_id and c.year = e.year
--   join vehicle_models mo on mo.id = coalesce(e.model_id, c.model_id)
--   order by 1, 2;

-- D) Title-only products that look doubtful (no eBay data to check them against): none/very few years, or a huge span
--   select a.product_id, a.title, a.current_rows from fitment_audit a
--   where a.status = 'title_only' and (a.current_rows <= 1 or a.current_rows > 40) order by a.current_rows desc limit 100;

-- E) Products with no fitment and not universal
--   select product_id, title from fitment_audit where status = 'no_fitment' order by random() limit 100;
