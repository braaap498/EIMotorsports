-- FITMENT 5b: ACCURACY CHECKS on ebay_compat_raw and on what stage 5 made of it. Read-only. Run each query on its own.
-- Goal: prove that nothing was lost or mangled between the raw eBay pull and ebay_compat_flat, before stage 7 changes anything.
-- Run after fitment_5_ebay_flatten.sql.

-- 1) What did the pull itself return? (anything other than 'ok' has no fitment to compare against)
select status, count(*) as listings, count(*) filter (where coalesce(compat_count,0) > 0) as with_compat,
       sum(compat_count) as compat_entries, min(error_code) as sample_error
from ebay_compat_raw group by status order by 2 desc;

-- 2) Pulled listings that never reached ebay_compat_flat, and why. Should be zero rows except "no_product_link"/"bad_json" you accept.
with parsed as (
  select r.channel_listing_id, r.ebay_item_id, r.compat_count,
         case when jsonb_typeof(to_jsonb(r.compat_list)) = 'string' then (to_jsonb(r.compat_list) #>> '{}')::jsonb
              else to_jsonb(r.compat_list) end as j
  from ebay_compat_raw r where r.status = 'ok' and coalesce(r.compat_count,0) > 0
)
select p.channel_listing_id, p.ebay_item_id, p.compat_count,
       case when cl.id is null then 'no_channel_listing_row'
            when cl.product_id is null then 'no_product_link'
            when jsonb_typeof(p.j) <> 'array' then 'not_a_json_array'
            when not exists (select 1 from ebay_compat_flat f where f.channel_listing_id = p.channel_listing_id) then 'all_entries_filtered'
       end as problem
from parsed p left join channel_listings cl on cl.id = p.channel_listing_id
where cl.id is null or cl.product_id is null or jsonb_typeof(p.j) <> 'array'
   or not exists (select 1 from ebay_compat_flat f where f.channel_listing_id = p.channel_listing_id)
limit 200;

-- 3) Entry-level reconciliation per listing: eBay's own compat_count vs entries in the JSON vs rows kept.
--    kept < entries means entries were filtered (missing make/model, bad year) or were exact duplicates.
with parsed as (
  select r.channel_listing_id, r.compat_count,
         case when jsonb_typeof(to_jsonb(r.compat_list)) = 'string' then (to_jsonb(r.compat_list) #>> '{}')::jsonb
              else to_jsonb(r.compat_list) end as j
  from ebay_compat_raw r where r.status = 'ok' and coalesce(r.compat_count,0) > 0
), counts as (
  select p.channel_listing_id, p.compat_count,
         jsonb_array_length(case when jsonb_typeof(p.j) = 'array' then p.j else '[]'::jsonb end) as entries,
         (select count(*) from ebay_compat_flat f where f.channel_listing_id = p.channel_listing_id) as kept
  from parsed p
)
select count(*) as listings,
       count(*) filter (where entries <> compat_count) as count_ne_entries,
       count(*) filter (where kept < entries)          as some_entries_dropped,
       sum(entries) as entries_total, sum(kept) as kept_total
from counts;
--    ...and the worst listings:
--   (replace the final select above with:)  select * from counts where entries <> compat_count or kept < entries order by entries - kept desc limit 50;

-- 4) Why entries were dropped, by reason (counted straight from the raw JSON)
with parsed as (
  select r.channel_listing_id,
         case when jsonb_typeof(to_jsonb(r.compat_list)) = 'string' then (to_jsonb(r.compat_list) #>> '{}')::jsonb
              else to_jsonb(r.compat_list) end as j
  from ebay_compat_raw r where r.status = 'ok' and coalesce(r.compat_count,0) > 0
)
select case when coalesce(btrim(e->'attrs'->>'Make'),'')  = '' then 'missing make'
            when coalesce(btrim(e->'attrs'->>'Model'),'') = '' then 'missing model'
            when (e->'attrs'->>'Year') is null                  then 'missing year'
            when (e->'attrs'->>'Year') !~ '^(19|20)[0-9]{2}$'   then 'bad year: ' || (e->'attrs'->>'Year')
            else 'kept' end as reason,
       count(*) as entries
from parsed p cross join lateral jsonb_array_elements(case when jsonb_typeof(p.j) = 'array' then p.j else '[]'::jsonb end) e
group by 1 order by 2 desc;

-- 5) Products fed by more than one eBay listing (their eBay data is the union; check the listings agree)
select product_id, count(distinct channel_listing_id) as listings, count(*) as rows,
       string_agg(distinct make_name || ' ' || model_name, ', ') as models
from ebay_compat_flat group by product_id having count(distinct channel_listing_id) > 1 limit 100;

-- 6) Does eBay's MAKE agree with the make your title parser found? Disagreement = one of the two is wrong for that product.
select f.product_id, r.title, r.make_name as title_make, string_agg(distinct f.make_name, ', ') as ebay_makes
from ebay_compat_flat f join fitment_parse_review r on r.product_id = f.product_id
where r.make_name is not null
group by f.product_id, r.title, r.make_name
having bool_and(fitment_key(f.make_name) <> fitment_key(r.make_name))
limit 100;

-- 7) Ambiguity in your own models: two models with the same make + normalised name (an exact match could pick the wrong one)
select vm.name as make, fitment_key(vmo.name) as model_key, count(*) as models, string_agg(vmo.name || ' #' || vmo.id, ' | ') as which
from vehicle_models vmo join vehicle_makes vm on vm.id = vmo.make_id
group by vm.name, fitment_key(vmo.name) having count(*) > 1;

-- 8) Year sanity on the eBay side: absurd spans or future years
select product_id, min(year) as first_year, max(year) as last_year, count(distinct year) as years
from ebay_compat_flat group by product_id
having max(year) > extract(year from now())::int + 1 or max(year) - min(year) > 45
limit 100;

-- 9) Coverage: how many products have eBay compat data at all, and how many of those are mapped to your models today
select count(*) as products,
       count(*) filter (where ebay_rows > 0) as with_ebay_data,
       count(*) filter (where ebay_rows > 0 and ebay_rows_unmapped = 0) as fully_mapped
from fitment_audit;   -- (run fitment_6_audit.sql first)

-- 10) Eyeball test: 15 random products, title vs eBay models and year spans
select a.product_id, a.title,
       (select string_agg(f.make_name || ' ' || f.model_name || ' ' || min_y || '-' || max_y, ' ; ')
          from (select make_name, model_name, min(year) as min_y, max(year) as max_y
                from ebay_compat_flat where product_id = a.product_id group by 1, 2) f) as ebay_says
from fitment_audit a where a.ebay_rows > 0 order by random() limit 15;
