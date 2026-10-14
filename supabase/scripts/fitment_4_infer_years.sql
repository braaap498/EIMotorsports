-- FITMENT STAGE 4 (OPTIONAL): year-less products.
-- Run AFTER fitment_3_apply.sql.
--
-- Some titles name a make and model but no year at all ("Honda TRX400EX Top End Rebuild Kit ... 85mm").
-- These parts are normally sold for the model's whole production run, so this links each one to every year
-- that model already has in your vehicles table, and flags the link in product_fitment.notes so the site can
-- show it as a "possible fit" rather than an exact fit.
--
-- It only uses models that already exist (made from titles that DO have years), so it never invents a model.
-- Review first with the SELECT below, then run the INSERT. Safe to re-run.

-- PREVIEW (run on its own): how many year-less products would get linked, and to how many vehicles.
select count(distinct r.product_id) as products,
       count(*)                     as fitment_rows
from fitment_parse_review r
join vehicle_makes vm   on fitment_key(vm.name) = fitment_key(r.make_name)
join vehicle_models vmo on vmo.make_id = vm.id and fitment_key(vmo.name) = r.model_key
join vehicles v         on v.model_id = vmo.id and v.submodel is null
where r.status = 'no_years'
  and r.model_key is not null;

-- APPLY (run on its own after checking the preview):
begin;

insert into product_fitment (product_id, vehicle_id, notes)
select distinct on (r.product_id, v.id)
       r.product_id,
       v.id,
       btrim('possible fit: year not in title, linked to all known years ' || coalesce(r.submodel_raw, ''))
from fitment_parse_review r
join vehicle_makes vm   on fitment_key(vm.name) = fitment_key(r.make_name)
join vehicle_models vmo on vmo.make_id = vm.id and fitment_key(vmo.name) = r.model_key
join vehicles v         on v.model_id = vmo.id and v.submodel is null
where r.status = 'no_years'
  and r.model_key is not null
  and not exists (
    select 1 from product_fitment pf
    where pf.product_id = r.product_id and pf.vehicle_id = v.id
  );

commit;
