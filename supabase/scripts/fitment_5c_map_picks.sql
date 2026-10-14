-- FITMENT 5c: map the clear eBay models to your existing models (step 1), then count what is left (step 2 count).
-- Run PART 1 first by itself. It prints "UPDATE n". Then run PART 2 by itself.
-- Lines whose target model does not exist in vehicle_models simply do not apply; they fall through to create_new later.

-- =========================== PART 1: the picks ===========================
with picks(make, ebay_model, target_make, target) as (values
 ('Honda','CRF450RWE','Honda','CRF450R'),('Honda','CRF250RWE','Honda','CRF250R'),
 ('Honda','CRF250LE','Honda','CRF250L'),('Honda','CBR600SE','Honda','CBR600'),
 ('Honda','CBR929RE','Honda','CBR929RR'),
 ('Honda','ST1100AL','Honda','ST 1100'),('Honda','ST1100P','Honda','ST 1100'),
 ('Honda','ST1300A','Honda','ST 1300'),('Honda','ST1300PA','Honda','ST 1300'),('Honda','ST1300P','Honda','ST 1300'),
 ('Honda','CRF1100L','Honda','AFRICA TWIN CRF1100L'),('Honda','CRF1000L','Honda','AFRICA TWIN CRF1000L'),
 ('Honda','FourTrax Foreman 400','Honda','FOREMAN 400'),
 ('Honda','Talon 1000R-4','Honda','TALON 1000R-4 FOX LIVE'),
 ('Honda','Interceptor 800','Honda','VFR 800'),('Honda','Super Hawk 1000','Honda','VTR1000F'),
 ('Honda','V45 Magna','Honda','VF750C'),
 ('Honda','Sportrax 400','Honda','TRX400EX'),('Honda','Sportrax 250','Honda','TRX250EX'),
 ('Honda','FourTrax 300','Honda','TRX300'),
 ('Suzuki','Hayabusa','Suzuki','GSX1300R'),('Suzuki','Quadsport Z400','Suzuki','LTZ400'),
 ('Suzuki','GSXR750W','Suzuki','GSXR 750'),('Suzuki','GSXR750X','Suzuki','GSXR 750'),
 ('Suzuki','GSXR600W','Suzuki','GSXR 600'),('Suzuki','GSXR600X','Suzuki','GSXR 600'),('Suzuki','GSXR600Z','Suzuki','GSXR 600'),
 ('Suzuki','GSXR1100W','Suzuki','GSXR 1100'),
 ('Suzuki','RMZ250L','Suzuki','RMZ250'),('Suzuki','RMZ450Z','Suzuki','RMZ450'),
 ('Suzuki','SV650A','Suzuki','SV650'),('Suzuki','GSXS750Z','Suzuki','GSXS 750'),
 ('Suzuki','SFV650','Suzuki','GLADIUS SFV650'),
 ('Kawasaki','KLX230R S','Kawasaki','KLX230'),('Kawasaki','KLX110R L','Kawasaki','KLX110'),
 ('Kawasaki','KZ550A','Kawasaki','KZ550'),
 ('Kawasaki','Ninja ZX12R','Kawasaki','ZX-12R'),
 ('Kawasaki','Ninja ZX14','Kawasaki','ZX-14'),('Kawasaki','Ninja ZX14R','Kawasaki','ZX-14'),
 ('Kawasaki','Ninja ZX11','Kawasaki','NINJA ZX-11 1100'),
 ('Kawasaki','Ninja ZX6RR','Kawasaki','NINJA ZX-6RR ZX600 600'),
 ('Kawasaki','Mule 4010 Trans','Kawasaki','MULE 4010'),('Kawasaki','Mule 4000 Trans','Kawasaki','MULE 4000'),
 ('Kawasaki','Mule PROFXR','Kawasaki','MULE PRO-FXR 820'),
 ('Kawasaki','RIDGE','Kawasaki','RIDGE 1000'),
 ('Yamaha','Raptor 700','Yamaha','RAPTOR 700R'),
 ('Yamaha','Bravo','Yamaha','BRAVO 250'),('Yamaha','Bravo LT','Yamaha','BRAVO 250'),
 ('Yamaha','Exciter II ST','Yamaha','EXCITER II ST 570'),('Yamaha','Exciter II SX','Yamaha','EXCITER II SX 570'),
 ('Yamaha','Exciter II LE','Yamaha','EXCITER II LE 570'),
 ('Yamaha','FJR1300A','Yamaha','FJR1300'),('Yamaha','Virago 535S','Yamaha','VIRAGO 535'),
 ('Yamaha','Wolverine RMAX4 1000 Compact','Yamaha','WOLVERINE RMAX4 1000'),
 ('Can-Am','Commander 800','Can-Am','COMMANDER 800R'),
 ('Can-Am','Maverick Trail 800R','Can-Am','MAVERICK TRAIL 800'),
 ('Bombardier','DS650','Can-Am','DS650'),
 ('Ski-Doo','GSX 380F','Ski-Doo','GSX 380'),('Ski-Doo','GTX 380F','Ski-Doo','GTX 380'),
 ('Ski-Doo','MXZ 380F','Ski-Doo','MXZ 380'),('Ski-Doo','Touring 380F','Ski-Doo','TOURING 380'),
 ('Ski-Doo','Legend 380F','Ski-Doo','LEGEND 380'),('Ski-Doo','Grand Touring 380F','Ski-Doo','GRAND TOURING 380'),
 ('Ski-Doo','MXZ 550F','Ski-Doo','MXZ 550'),('Ski-Doo','Legend 500F','Ski-Doo','LEGEND 500'),
 ('Ski-Doo','MXZ 500F','Ski-Doo','MXZ 500'),('Ski-Doo','Summit 550F','Ski-Doo','SUMMIT 550'),
 ('Ski-Doo','Tundra 550F','Ski-Doo','TUNDRA 550'),('Ski-Doo','Grand Touring 550F','Ski-Doo','GRAND TOURING 550'),
 ('Ski-Doo','GSX 800R','Ski-Doo','GSX 800'),('Ski-Doo','Mach Z 800R','Ski-Doo','MACH Z 800'),
 ('Ski-Doo','Formula III 600R','Ski-Doo','FORMULA III 600'),
 ('Polaris','Triumph 600','Polaris','INDY TRIUMPH 600')
)
update fitment_ebay_model_map m
set model_id = t.id
from picks p
join vehicle_makes vm  on fitment_key(vm.name) = fitment_key(p.target_make)
join vehicle_models t  on t.make_id = vm.id and fitment_key(t.name) = fitment_key(p.target)
where m.make_key = fitment_key(p.make)
  and m.model_key = fitment_key(p.ebay_model)
  and m.model_id is null;

-- =========================== PART 2: count what is left (run by itself) ===========================
-- select count(*) as models, sum(products_n) as product_links
-- from fitment_ebay_model_map where model_id is null and not skip and not create_new;
