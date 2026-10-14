-- FITMENT STAGE 5 (v2): same job as fitment_5_ebay_flatten.sql, split into 4 parts so no single run times out.
-- Changes NOTHING in products, vehicles or product_fitment. Safe to re-run; map decisions are kept.
--
-- HOW TO RUN: paste ONE PART at a time into the SQL editor and run it. Do the check query under each part
-- before moving on. (Running through psql/DBeaver with the same connection string avoids the editor's timeout;
-- there you can run the whole file at once.)
--
-- Differences from v1:
--   * the slow "scan the whole flat table once per eBay model" statements now use a small lookup table
--     (fitment_ebay_keys, a few hundred rows) instead of ebay_compat_flat
--   * the new tables get row-level security switched on and public access revoked
--   * the 0.8 "one name inside the other" suggestions are NOT meant to be bulk-accepted (see review notes)
-- Needs fitment_key() from the v2 parser migration.

-- =====================================================================================================
-- PART 1: flatten the raw eBay JSON into one row per product + vehicle.   (the heaviest part)
-- =====================================================================================================
create extension if not exists pg_trgm;

create table if not exists ebay_compat_flat (
  product_id         bigint not null,
  channel_listing_id bigint,
  ebay_item_id       text,
  make_name          text not null,
  model_name         text not null,
  submodel           text,
  year               int  not null
);
create index if not exists ebay_compat_flat_product_idx on ebay_compat_flat (product_id);
alter table ebay_compat_flat enable row level security;
revoke all on ebay_compat_flat from anon, authenticated;

truncate ebay_compat_flat;

insert into ebay_compat_flat (product_id, channel_listing_id, ebay_item_id, make_name, model_name, submodel, year)
with src as (
  select r.channel_listing_id, r.ebay_item_id,
         case when jsonb_typeof(to_jsonb(r.compat_list)) = 'string'
              then (to_jsonb(r.compat_list) #>> '{}')::jsonb
              else to_jsonb(r.compat_list)
         end as j
  from ebay_compat_raw r
  where r.status = 'ok' and coalesce(r.compat_count, 0) > 0
)
select distinct
       cl.product_id,
       s.channel_listing_id,
       s.ebay_item_id,
       btrim(e -> 'attrs' ->> 'Make'),
       btrim(e -> 'attrs' ->> 'Model'),
       nullif(btrim(e -> 'attrs' ->> 'Submodel'), ''),
       (e -> 'attrs' ->> 'Year')::int
from src s
join channel_listings cl on cl.id = s.channel_listing_id
cross join lateral jsonb_array_elements(case when jsonb_typeof(s.j) = 'array' then s.j else '[]'::jsonb end) e
where coalesce(btrim(e -> 'attrs' ->> 'Make'), '')  <> ''
  and coalesce(btrim(e -> 'attrs' ->> 'Model'), '') <> ''
  and (e -> 'attrs' ->> 'Year') ~ '^(19|20)[0-9]{2}$';

analyze ebay_compat_flat;

-- CHECK (run on its own): rows and products should both be in the hundreds of thousands / thousands.
--   select count(*) as rows, count(distinct product_id) as products from ebay_compat_flat;

-- =====================================================================================================
-- PART 2: the model map (every distinct eBay make + model).
-- =====================================================================================================
create table if not exists fitment_ebay_model_map (
  make_key            text not null,
  model_key           text not null,
  make_name           text,
  model_name          text,
  rows_n              int,
  products_n          int,
  model_id            bigint,            -- your vehicle_models.id (filled automatically on an exact match)
  suggested_model_id  bigint,            -- best guess when there is no exact match (NOT applied automatically)
  suggestion_score    real,
  create_new          boolean not null default false,   -- set true: stage 7 creates this model from eBay's spelling
  skip                boolean not null default false,   -- set true: ignore this eBay model completely
  primary key (make_key, model_key)
);
alter table fitment_ebay_model_map enable row level security;
revoke all on fitment_ebay_model_map from anon, authenticated;

insert into fitment_ebay_model_map (make_key, model_key, make_name, model_name, rows_n, products_n)
select fitment_key(make_name), fitment_key(model_name),
       mode() within group (order by make_name),
       mode() within group (order by model_name),
       count(*), count(distinct product_id)
from ebay_compat_flat
group by 1, 2
having fitment_key(make_name) <> '' and fitment_key(model_name) <> ''
on conflict (make_key, model_key) do update
  set rows_n = excluded.rows_n, products_n = excluded.products_n;

-- CHECK (run on its own): a few hundred rows is normal.
--   select count(*) from fitment_ebay_model_map;

-- =====================================================================================================
-- PART 3: small key table, drop stale map rows, exact matches.
-- =====================================================================================================
drop table if exists fitment_ebay_keys;
create table fitment_ebay_keys as
select distinct fitment_key(make_name)  as make_key,
                fitment_key(model_name) as model_key,
                fitment_key(submodel)   as sub_key
from ebay_compat_flat;
create index on fitment_ebay_keys (make_key, model_key, sub_key);
alter table fitment_ebay_keys enable row level security;
revoke all on fitment_ebay_keys from anon, authenticated;

-- drop map rows that no longer appear in the eBay data and that you never touched
delete from fitment_ebay_model_map m
where not exists (select 1 from fitment_ebay_keys k
                  where k.make_key = m.make_key and k.model_key = m.model_key)
  and m.model_id is null and not m.create_new and not m.skip;

-- exact matches (same make, same model ignoring spaces/punctuation/case)
update fitment_ebay_model_map m
set model_id = vmo.id
from vehicle_makes vm
join vehicle_models vmo on vmo.make_id = vm.id
where m.model_id is null and not m.skip and not m.create_new
  and fitment_key(vm.name)  = m.make_key
  and fitment_key(vmo.name) = m.model_key;

-- CHECK (run on its own):
--   select count(*) filter (where model_id is not null) as mapped,
--          count(*) filter (where model_id is null and not skip and not create_new) as still_open,
--          sum(rows_n) filter (where model_id is null and not skip and not create_new) as open_rows
--   from fitment_ebay_model_map;

-- =====================================================================================================
-- PART 4: suggestions for the rest (a guess, never applied automatically).
-- =====================================================================================================
update fitment_ebay_model_map m
set suggested_model_id = s.id, suggestion_score = s.score
from (
  select distinct on (m2.make_key, m2.model_key)
         m2.make_key, m2.model_key, vmo.id,
         greatest(
           similarity(fitment_key(vmo.name), m2.model_key),
           case when length(fitment_key(vmo.name)) >= 3 and position(fitment_key(vmo.name) in m2.model_key) > 0 then 0.8 else 0 end,
           case when length(m2.model_key) >= 3 and position(m2.model_key in fitment_key(vmo.name)) > 0 then 0.8 else 0 end,
           case when exists (select 1 from fitment_ebay_keys k
                             where k.make_key = m2.make_key and k.model_key = m2.model_key
                               and k.sub_key = fitment_key(vmo.name)) then 0.95 else 0 end
         ) as score
  from fitment_ebay_model_map m2
  join vehicle_makes vm   on fitment_key(vm.name) = m2.make_key
  join vehicle_models vmo on vmo.make_id = vm.id
  where m2.model_id is null and not m2.skip and not m2.create_new
  order by m2.make_key, m2.model_key, 4 desc
) s
where m.make_key = s.make_key and m.model_key = s.model_key and s.score >= 0.4;

-- =====================================================================================================
-- REVIEW (run each on its own, after all four parts)
-- =====================================================================================================

-- B) THE LIST TO WORK THROUGH: eBay models with no exact match, biggest first, with the best guess.
--   select m.make_name, m.model_name as ebay_model, m.products_n, m.rows_n,
--          vmo.name as suggested_model, round(m.suggestion_score::numeric, 2) as score
--   from fitment_ebay_model_map m
--   left join vehicle_models vmo on vmo.id = m.suggested_model_id
--   where m.model_id is null and not m.skip and not m.create_new
--   order by m.products_n desc, m.make_name, m.model_name;

-- C) DECIDING (examples). Bulk-accept ONLY the 0.95 matches (eBay's submodel equals your model); the 0.8 matches are
--    "one name contains the other" and can point RZR at RZR570, so check those by eye.
--     update fitment_ebay_model_map set model_id = suggested_model_id where model_id is null and suggestion_score >= 0.95;
--   point one eBay model at one of yours:
--     update fitment_ebay_model_map set model_id = (select id from vehicle_models where name = 'RANCHER 420' limit 1)
--     where make_key = 'HONDA' and model_key = 'TRX420FA';
--   it is a real model you don't have yet (stage 7 will create it):
--     update fitment_ebay_model_map set create_new = true where make_key = 'YAMAHA' and model_key = 'BLASTER200';
--   ignore it:
--     update fitment_ebay_model_map set skip = true where make_key = 'HONDA' and model_key = 'XYZ';
-- Re-running Parts 1-4 keeps all of those decisions.

-- D) eBay makes you don't have at all:
--   select distinct m.make_name from fitment_ebay_model_map m
--   where not exists (select 1 from vehicle_makes vm where fitment_key(vm.name) = m.make_key);
