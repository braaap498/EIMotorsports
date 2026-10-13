-- FITMENT STAGE 1: parse every product title into make / model / years.
-- Changes NOTHING in products, vehicles or product_fitment; it only fills fitment_parse_review.
-- Safe to re-run: it clears the staging table first (which also discards any manual fixes you made there).

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
  (product_id, title, make_name, model_raw, model_key, years, spec_vehicle_type_slug, spec_make, status)
select
  p.id,
  p.title,
  f.o_make,
  nullif(f.o_model, ''),
  nullif(upper(regexp_replace(f.o_model, '[^A-Za-z0-9]', '', 'g')), ''),
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
