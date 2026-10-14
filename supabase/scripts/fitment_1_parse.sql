-- FITMENT STAGE 1 (v2): parse every product title into make / model / submodel / years.
-- Changes NOTHING in products, vehicles or product_fitment; it only fills fitment_parse_review.
-- Safe to re-run: it clears the staging table first (which also discards manual fixes made there).
-- Run it ONE statement at a time if the editor complains: first the truncate, then the insert, then the aliases.

truncate fitment_parse_review;

with spec as (
  select
    product_id,
    -- vehicle type from the eBay specifics, mapped onto your vehicle_types slugs
    min(case
          when name in ('Machine Type', 'Vehicle Type') then
            case
              when value ~* '\msnowmobile'                              then 'snowmobile'
              when value ~* '(\mpwc\M|watercraft)'                      then 'pwc'
              when value ~* '(\mutv\M|\msxs\M|side.by.side)'            then 'utv'
              when value ~* '(\matv\M|\matc\M|quad)'                    then 'atv'
              when value ~* '(off.?road|dirt|\mmx\M|dual sport|supermoto)' then 'dirt-bike-mx'
              when value ~* '(street|motorcycle)'                       then 'street-sport-bike'
            end
        end) as vt_slug,
    min(case
          when name = 'Compatible Make' then regexp_replace(value, '^\s*for\s+', '', 'i')
        end) as spec_make
  from product_item_specifics
  where name in ('Machine Type', 'Vehicle Type', 'Compatible Make')
  group by product_id
)
insert into fitment_parse_review
  (product_id, title, make_name, model_raw, model_key, submodel_raw, years, spec_vehicle_type_slug, spec_make, status)
select
  p.id,
  p.title,
  f.o_make,
  nullif(f.o_model, ''),
  nullif(fitment_key(f.o_model), ''),
  nullif(f.o_submodel, ''),
  f.o_years,
  s.vt_slug,
  s.spec_make,
  case
    when p.title ~* '\muniversal\M'                     then 'universal'
    when f.o_make is null                               then 'no_make'
    when coalesce(f.o_model, '') = ''                   then 'no_model'
    when coalesce(array_length(f.o_years, 1), 0) = 0    then 'no_years'
    else 'ok'
  end
from products p
cross join lateral parse_fitment_title(p.title) f
left join spec s on s.product_id = p.id;

-- ALIASES: the same real model written two ways in your titles. Each line maps a model key to the name to keep.
-- (Run this as its own statement after the insert above.)
update fitment_parse_review r
set model_raw = a.to_name,
    model_key = fitment_key(a.to_name)
from (values
  ('Honda',  'CR125',          'CR125R'),
  ('Honda',  'CR250',          'CR250R'),
  ('Honda',  'CR80',           'CR80R'),
  ('Honda',  'FOURTRAX300',    'TRX300'),
  ('Honda',  'FOURTRAXTRX300', 'TRX300'),
  ('Honda',  'FOURTRAX400EX',  'TRX400EX'),
  ('Honda',  'SPORTRAX400EX',  'TRX400EX'),
  ('Honda',  'SPORTRAX250',    'TRX250EX'),
  ('Honda',  '450FOREMAN',     'FOREMAN 450'),
  ('Honda',  'FOREMANTRX450',  'FOREMAN 450'),
  ('Honda',  '350RANCHER',     'RANCHER 350'),
  ('Honda',  'TRX520',         'FOREMAN 520'),
  ('Suzuki', 'LTZZ400',        'LTZ400'),
  ('Suzuki', 'Z400',           'LTZ400'),
  ('Yamaha', 'RAPTOR660',      'RAPTOR 660R'),
  ('Yamaha', 'RAPTOR700',      'RAPTOR 700R'),
  ('Yamaha', '350BIGBEAR',     'BIG BEAR 350')
) as a(make_name, from_key, to_name)
where r.make_name = a.make_name
  and r.model_key = a.from_key;

-- Suzuki DR-Z: "DR-Z DRZ 400 400S" should be the DRZ400S, not the base DRZ400. (Own statement.)
update fitment_parse_review
set model_raw = 'DRZ' || submodel_raw,
    model_key = fitment_key('DRZ' || submodel_raw),
    submodel_raw = null
where make_name = 'Suzuki'
  and model_key = 'DRZ400'
  and submodel_raw in ('400E', '400S', '400SM');
