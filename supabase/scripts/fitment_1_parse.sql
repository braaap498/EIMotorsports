-- FITMENT STAGE 1 (v3): parse every product title into make / model / submodel / years.
-- Changes NOTHING in products, vehicles or product_fitment; it only fills fitment_parse_review.
-- Safe to re-run: it clears the staging table first (which also discards manual fixes made there).
-- Run the whole file at once. It has four statements: truncate, insert, alias merge, DR-Z fix.
-- Needs the v2 parser migration (20261013170000_fitment_parser_v2.sql) to be run first.

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

-- ALIASES: the same real model written several ways in your titles.
-- Each line: make, the model key to merge FROM, the model name to keep, and a variant note to record.
update fitment_parse_review r
set model_raw    = a.to_name,
    model_key    = fitment_key(a.to_name),
    submodel_raw = nullif(btrim(coalesce(a.new_sub, '') || ' ' || coalesce(r.submodel_raw, '')), '')
from (values
  -- Honda
  ('Honda',  'CR125',                'CR125R',        ''),
  ('Honda',  'CR250',                'CR250R',        ''),
  ('Honda',  'CR80',                 'CR80R',         ''),
  ('Honda',  'FOURTRAX300',          'TRX300',        ''),
  ('Honda',  'FOURTRAXTRX300',       'TRX300',        ''),
  ('Honda',  'FOURTRAX400EX',        'TRX400EX',      ''),
  ('Honda',  'SPORTRAX400EX',        'TRX400EX',      ''),
  ('Honda',  'SPORTRAX250',          'TRX250EX',      ''),
  ('Honda',  '450FOREMAN',           'FOREMAN 450',   ''),
  ('Honda',  '450FOREMANS',          'FOREMAN 450',   'S'),
  ('Honda',  'FOREMANTRX450',        'FOREMAN 450',   ''),
  ('Honda',  '350RANCHER',           'RANCHER 350',   ''),
  ('Honda',  'TRX520',               'FOREMAN 520',   ''),
  ('Honda',  '1800GOLDWING',         'GOLDWING 1800', ''),
  -- Kawasaki
  ('Kawasaki', 'CONCOURS1400',       'CONCOURS 14',   ''),
  ('Kawasaki', 'VULCANCLASSIC800',   'VULCAN 800',    'CLASSIC'),
  ('Kawasaki', 'VULCANDRIFTER800',   'VULCAN 800',    'DRIFTER'),
  ('Kawasaki', 'VULCANCLASSIC900',   'VULCAN 900',    'CLASSIC'),
  ('Kawasaki', 'VULCANCLASSICLT900', 'VULCAN 900',    'CLASSIC LT'),
  ('Kawasaki', 'VULCANCUSTOM900',    'VULCAN 900',    'CUSTOM'),
  ('Kawasaki', 'NINJAZX7RZX750750',  'NINJA ZX-7R',   ''),
  ('Kawasaki', 'NINJAZX14ZX14R1400', 'ZX-14',         ''),
  -- Suzuki
  ('Suzuki', 'LTZZ400',              'LTZ400',        ''),
  ('Suzuki', 'Z400',                 'LTZ400',        ''),
  ('Suzuki', 'GSXR750GSXR750',       'GSXR 750',      ''),
  ('Suzuki', 'GSXR1100GSXR1100',     'GSXR 1100',     ''),
  ('Suzuki', '700KINGQUAD',          'KING QUAD 700', ''),
  -- Yamaha
  ('Yamaha', 'RAPTOR660',            'RAPTOR 660R',   ''),
  ('Yamaha', 'RAPTOR700',            'RAPTOR 700R',   ''),
  ('Yamaha', '350BIGBEAR',           'BIG BEAR 350',  ''),
  ('Yamaha', '650VSTARCLASSIC',      'V-STAR 650',    'CLASSIC'),
  ('Yamaha', '650VSTARCLASSICXVS650A','V-STAR 650',   'CLASSIC'),
  ('Yamaha', '650VSTARCUSTOM',       'V-STAR 650',    'CUSTOM'),
  ('Yamaha', '650VSTARMIDNIGHTCUSTOM','V-STAR 650',   'MIDNIGHT CUSTOM'),
  ('Yamaha', '650VSTARMIDNIGHTCUSTOMXVS','V-STAR 650','MIDNIGHT CUSTOM'),
  ('Yamaha', '650VSTARSILVERADO',    'V-STAR 650',    'SILVERADO'),
  ('Yamaha', 'VIRAGO700C',           'VIRAGO 700',    'C'),
  ('Yamaha', 'VIRAGO700S',           'VIRAGO 700',    'S'),
  -- Arctic Cat snowmobiles: F, M and Crossfire series
  ('Arctic Cat', 'F6600',                'F6', '600'),
  ('Arctic Cat', 'F6F6600',              'F6', '600'),
  ('Arctic Cat', 'F6FIRECAT600',         'F6', 'FIRECAT 600'),
  ('Arctic Cat', 'F6LXR600',             'F6', 'LXR 600'),
  ('Arctic Cat', 'F6SNOPRO',             'F6', 'SNO PRO'),
  ('Arctic Cat', 'F6SNOPROFIRECAT600',   'F6', 'SNO PRO FIRECAT 600'),
  ('Arctic Cat', 'F7FIRECAT700',         'F7', 'FIRECAT 700'),
  ('Arctic Cat', 'F7SNOPRO',             'F7', 'SNO PRO'),
  ('Arctic Cat', 'F8LXR',                'F8', 'LXR'),
  ('Arctic Cat', 'F8NIGHTFIRESNOPRO',    'F8', 'NIGHTFIRE SNO PRO'),
  ('Arctic Cat', 'F8SNOPRO',             'F8', 'SNO PRO'),
  ('Arctic Cat', 'F8TONYSTEWART',        'F8', 'TONY STEWART'),
  ('Arctic Cat', 'M6M6600',              'M6', '600'),
  ('Arctic Cat', 'M7EFI',                'M7', 'EFI'),
  ('Arctic Cat', 'M8HCR',                'M8', 'HCR'),
  ('Arctic Cat', 'M8SNOPRO',             'M8', 'SNO PRO'),
  ('Arctic Cat', 'M8SNOPRONIGHTFIRE',    'M8', 'SNO PRO NIGHTFIRE')
) as a(make_name, from_key, to_name, new_sub)
where r.make_name = a.make_name
  and r.model_key = a.from_key;

-- Suzuki DR-Z: "DR-Z DRZ 400 400S" should be the DRZ400S, not the base DRZ400.
update fitment_parse_review
set model_raw = 'DRZ' || submodel_raw,
    model_key = fitment_key('DRZ' || submodel_raw),
    submodel_raw = null
where make_name = 'Suzuki'
  and model_key = 'DRZ400'
  and submodel_raw in ('400E', '400S', '400SM');
