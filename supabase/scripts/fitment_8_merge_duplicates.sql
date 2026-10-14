-- FITMENT 8: merge eBay-spelled duplicate models into your canonical model for the SAME vehicle.
-- Only pairs that are clearly one vehicle are here. Families (KTM "250", Polaris "XLT", Yamaha "Moto 4") are NOT merged.
--
-- BEFORE RUNNING, take one more backup (product_fitment_backup_20261014 already exists):
--   create table vehicles_backup_20261014 as select * from vehicles;
--   alter table vehicles_backup_20261014 enable row level security;
--   revoke all on vehicles_backup_20261014 from anon, authenticated;
--
-- Run STEP A by itself first (read-only). It shows which pairs were found. Then run STEP B (one transaction).
-- After STEP B: re-run fitment_6_audit.sql, lock down fitment_audit, send me query A.

-- =========================== STEP A: preview (read-only) ===========================
with merges(make_name, dup, canon) as (values
 ('Honda','Sportrax 300','TRX300EX'),
 ('Kawasaki','Mule PRODX','MULE PRO-DX 1000'),
 ('Kawasaki','Mule PRODXT','MULE PRO-DXT DIESEL'),
 ('Suzuki','Quadracer 450','LT-R 450'),
 ('Suzuki','Quadracer 250','LT250R'),
 ('Suzuki','Quadracer 500','LT500'),
 ('Suzuki','GSX1300R','HAYABUSA GSX1300R'),
 ('Suzuki','V-STORM 650','V-STROM 650'),
 ('Honda','XR400','XR400R'),
 ('Can-Am','Defender Max HD9','DEFENDER HD9 MAX'),
 ('Can-Am','Defender Max HD7','DEFENDER HD7 MAX'),
 ('Can-Am','Defender Max HD10','DEFENDER HD10 MAX')
)
select x.make_name, x.dup, x.canon,
       dm.id as dup_id, cm.id as canon_id,
       (select count(*) from product_fitment pf join vehicles v on v.id = pf.vehicle_id where v.model_id = dm.id) as fits_to_move
from merges x
left join vehicle_makes vm  on fitment_key(vm.name) = fitment_key(x.make_name)
left join vehicle_models dm on dm.make_id = vm.id and fitment_key(dm.name) = fitment_key(x.dup)
left join vehicle_models cm on cm.make_id = vm.id and fitment_key(cm.name) = fitment_key(x.canon)
order by 1, 2;
-- A row with a null dup_id or canon_id means that pair was not found by name; it is simply skipped in STEP B.

-- =========================== STEP B: apply (one transaction) ===========================
begin;

create temp table merges (make_name text, dup text, canon text) on commit drop;
insert into merges values
 ('Honda','Sportrax 300','TRX300EX'),
 ('Kawasaki','Mule PRODX','MULE PRO-DX 1000'),
 ('Kawasaki','Mule PRODXT','MULE PRO-DXT DIESEL'),
 ('Suzuki','Quadracer 450','LT-R 450'),
 ('Suzuki','Quadracer 250','LT250R'),
 ('Suzuki','Quadracer 500','LT500'),
 ('Suzuki','GSX1300R','HAYABUSA GSX1300R'),
 ('Suzuki','V-STORM 650','V-STROM 650'),
 ('Honda','XR400','XR400R'),
 ('Can-Am','Defender Max HD9','DEFENDER HD9 MAX'),
 ('Can-Am','Defender Max HD7','DEFENDER HD7 MAX'),
 ('Can-Am','Defender Max HD10','DEFENDER HD10 MAX');

create temp table m2 on commit drop as
select dm.id as dup_id, cm.id as canon_id
from merges x
join vehicle_makes vm  on fitment_key(vm.name) = fitment_key(x.make_name)
join vehicle_models dm on dm.make_id = vm.id and fitment_key(dm.name) = fitment_key(x.dup)
join vehicle_models cm on cm.make_id = vm.id and fitment_key(cm.name) = fitment_key(x.canon)
where dm.id <> cm.id;

-- 1) future eBay rows land on the canonical model
update fitment_ebay_model_map m set model_id = mm.canon_id
from m2 mm where m.model_id = mm.dup_id;

-- 2) make sure the canonical model has a vehicle for every year the duplicate had
insert into vehicles (model_id, year)
select distinct mm.canon_id, dv.year
from vehicles dv join m2 mm on mm.dup_id = dv.model_id
where dv.submodel is null
  and not exists (select 1 from vehicles c where c.model_id = mm.canon_id and c.year = dv.year and c.submodel is null);

-- 3) move the fits onto the canonical model
insert into product_fitment (product_id, vehicle_id, notes, source)
select distinct on (pf.product_id, cv.id) pf.product_id, cv.id, pf.notes, pf.source
from product_fitment pf
join vehicles dv on dv.id = pf.vehicle_id
join m2 mm on mm.dup_id = dv.model_id
join vehicles cv on cv.model_id = mm.canon_id and cv.year = dv.year and cv.submodel is null
where not exists (select 1 from product_fitment x where x.product_id = pf.product_id and x.vehicle_id = cv.id);

-- 4) fits that already existed on the canonical model and that eBay also lists become 'ebay'
update product_fitment x set source = 'ebay'
from product_fitment pf
join vehicles dv on dv.id = pf.vehicle_id
join m2 mm on mm.dup_id = dv.model_id
join vehicles cv on cv.model_id = mm.canon_id and cv.year = dv.year and cv.submodel is null
where pf.source = 'ebay' and x.product_id = pf.product_id and x.vehicle_id = cv.id
  and x.source is distinct from 'ebay';

-- 5) remove the duplicate's fits and its now-unused vehicles
delete from product_fitment pf using vehicles dv, m2 mm
where dv.id = pf.vehicle_id and dv.model_id = mm.dup_id;

delete from vehicles v using m2 mm
where v.model_id = mm.dup_id
  and not exists (select 1 from product_fitment pf where pf.vehicle_id = v.id);

commit;

-- The emptied duplicate models are left in vehicle_models on purpose. Deleting them is a separate step I will give you later.
