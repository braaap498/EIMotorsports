-- FITMENT REVIEW QUERIES. Run each one SEPARATELY (the SQL Editor only shows the last result).
-- Read-only: nothing here changes any data.

-- A) How did the parse go overall?
select status, count(*) as products
from fitment_parse_review
group by status
order by 2 desc;

-- B) Makes found, with product counts.
select make_name, count(*) as products
from fitment_parse_review
where status = 'ok'
group by make_name
order by 2 desc;

-- C) Distinct models: THIS is the list to review (a few hundred rows, not 10,000 products).
--    Look for the same model spelled two ways, or junk words stuck on the end.
select make_name,
       model_key,
       mode() within group (order by model_raw) as model_name,
       count(*)                                  as products,
       count(distinct submodel_raw)              as variants,
       min(years[1])                             as first_year,
       max(years[array_length(years, 1)])        as last_year,
       min(title)                                as example_title
from fitment_parse_review
where status = 'ok'
group by make_name, model_key
order by make_name, model_key;

-- D) Titles with a make and model but no year (these get no fitment unless fixed).
select product_id, title, make_name, model_raw
from fitment_parse_review
where status = 'no_years'
order by random()
limit 100;

-- E) Titles where no make or no model was found.
select product_id, title, status
from fitment_parse_review
where status in ('no_make', 'no_model')
order by random()
limit 100;

-- F) Cross-check: parsed make disagrees with the eBay "Compatible Make" specific.
select product_id, title, make_name, spec_make
from fitment_parse_review
where status = 'ok'
  and spec_make is not null
  and lower(regexp_replace(spec_make, '[^a-z0-9]', '', 'gi')) <> lower(regexp_replace(make_name, '[^a-z0-9]', '', 'gi'))
limit 100;

-- G) Models that will have no vehicle type (the specifics didn't say, and the make doesn't imply one).
select make_name, model_key, mode() within group (order by model_raw) as model_name, count(*) as products
from fitment_parse_review
where status = 'ok'
group by make_name, model_key
having count(spec_vehicle_type_slug) = 0
   and upper(regexp_replace(make_name, '[^A-Za-z0-9]', '', 'g')) not in ('SKIDOO', 'SEADOO')
order by 4 desc
limit 200;

-- FIXING A MODEL: edit the staging table, then re-run query C. Example (merge "LTZ Z 400" into "LTZ400"):
--
-- update fitment_parse_review
-- set model_raw = 'LTZ400', model_key = 'LTZ400'
-- where make_name = 'Suzuki' and model_key = 'LTZZ400';
--
-- To drop a bad parse so it gets no fitment:
-- update fitment_parse_review set status = 'no_model' where product_id in (123, 456);
