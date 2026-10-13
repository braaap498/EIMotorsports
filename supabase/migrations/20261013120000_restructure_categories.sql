-- Restructure categories into a 15-department tree (department > subcategory).
--
-- Safe by design: no products are touched. Your 12 real categories keep their
-- ids, so every product stays attached. They are renamed and moved under a
-- department. "Miscellaneous" (id 13) stays as a holding bin to empty out once
-- products are bulk-assigned.
--
-- Assumes categories.id auto-generates (it did for ids 1-13).

begin;

-- 1) Departments (top level) ------------------------------------------------
insert into categories (parent_id, name, slug, sort_order)
select null::int, v.name, v.slug, v.ord
from (values
  ('Engine',                'engine',             10),
  ('Brakes',                'brakes',             20),
  ('Suspension',            'suspension',         30),
  ('Drivetrain',            'drivetrain',         40),
  ('Wheels & Tires',        'wheels-tires',       50),
  ('Controls',              'controls',           60),
  ('Body & Frame',          'body-frame',         70),
  ('Crash Protection',      'crash-protection',   80),
  ('Exhaust',               'exhaust',            90),
  ('Intake',                'intake',            100),
  ('Fuel System',           'fuel-system',       110),
  ('Electrical & Batteries','electrical-batteries',120),
  ('Hardware & Bearings',   'hardware-bearings', 130),
  ('Tools & Maintenance',   'tools-maintenance', 140),
  ('Accessories',           'accessories',       150)
) as v(name, slug, ord)
where not exists (select 1 from categories c where c.slug = v.slug);

-- 2) Move + rename your existing categories (ids unchanged) -----------------
update categories c
set name       = m.new_name,
    slug       = m.new_slug,
    parent_id  = p.id,
    sort_order = m.ord
from (values
  (1,  'Levers',                       'levers',                     'controls',          20),
  (2,  'Gas Caps',                     'gas-caps',                   'fuel-system',       10),
  (3,  'Drive Belts',                  'drive-belts',                'drivetrain',        60),
  (4,  'Fork Parts',                   'fork-parts',                 'suspension',        10),
  (5,  'Gaskets & Seals',              'gaskets-seals',              'engine',            50),
  (6,  'Oil Filters',                  'oil-filters',                'engine',            70),
  (7,  'Pistons & Top-End Kits',       'pistons-top-end-kits',       'engine',            20),
  (8,  'Radiators & Cooling',          'radiators-cooling',          'engine',            60),
  (9,  'Tools',                        'tools',                      'tools-maintenance', 10),
  (10, 'Valve Train',                  'valve-train',                'engine',            45),
  (11, 'Carburetor & Injection Parts', 'carburetor-injection-parts', 'fuel-system',       20),
  (12, 'Starters & Recoils',           'starters-recoils',           'engine',            80)
) as m(id, new_name, new_slug, parent_slug, ord)
join categories p on p.slug = m.parent_slug and p.parent_id is null
where c.id = m.id;

-- Miscellaneous stays top-level, sorted last, until it is emptied.
update categories set sort_order = 999 where id = 13;

-- 3) New subcategories ------------------------------------------------------
insert into categories (parent_id, name, slug, sort_order)
select p.id, v.name, v.slug, v.ord
from (values
  -- Engine
  ('engine', 'Clutch Kits',                  'clutch-kits',                10),
  ('engine', 'Big-Bore Kits',                'big-bore-kits',              30),
  ('engine', 'Cranks & Rods',                'cranks-rods',                40),
  -- Brakes
  ('brakes', 'Brake Pads',                   'brake-pads',                 10),
  ('brakes', 'Rotors',                       'brake-rotors',               20),
  ('brakes', 'Calipers & Parts',             'calipers-parts',             30),
  ('brakes', 'Master Cylinders & Rebuild Kits','master-cylinders-rebuild-kits',40),
  ('brakes', 'Brake Lines',                  'brake-lines',                50),
  -- Suspension
  ('suspension', 'Shock Parts',              'shock-parts',                20),
  ('suspension', 'Linkage & Swingarm Bearings','linkage-swingarm-bearings',30),
  ('suspension', 'Triple Clamps',            'triple-clamps',              40),
  ('suspension', 'Suspension Tools',         'suspension-tools',           50),
  -- Drivetrain
  ('drivetrain', 'Chain & Sprocket Kits',    'chain-sprocket-kits',        10),
  ('drivetrain', 'Chains',                   'chains',                     20),
  ('drivetrain', 'Sprockets',                'sprockets',                  30),
  ('drivetrain', 'Chain Guides & Rollers',   'chain-guides-rollers',       40),
  ('drivetrain', 'Axles',                    'axles',                      50),
  ('drivetrain', 'Wheel Spacers',            'wheel-spacers',              70),
  -- Wheels & Tires
  ('wheels-tires', 'Tires',                  'tires',                      10),
  ('wheels-tires', 'Tubes & Rim Strips',    'tubes-rim-strips',           20),
  ('wheels-tires', 'Wheels & Rims',          'wheels-rims',                30),
  ('wheels-tires', 'Spokes',                 'spokes',                     40),
  ('wheels-tires', 'Wheel Bearings & Seals', 'wheel-bearings-seals',       50),
  -- Controls
  ('controls', 'Handlebars',                 'handlebars',                 10),
  ('controls', 'Grips',                      'grips',                      30),
  ('controls', 'Cables',                     'cables',                     40),
  ('controls', 'Hand Guards',                'hand-guards',                50),
  ('controls', 'Footpegs & Foot Controls',   'footpegs-foot-controls',     60),
  -- Body & Frame
  ('body-frame', 'Plastics',                 'plastics',                   10),
  ('body-frame', 'Seats',                    'seats',                      20),
  ('body-frame', 'Fenders',                  'fenders',                    30),
  ('body-frame', 'Fuel Tanks',               'fuel-tanks',                 40),
  ('body-frame', 'Body Hardware',            'body-hardware',              50),
  -- Crash Protection
  ('crash-protection', 'Skid Plates',        'skid-plates',                10),
  ('crash-protection', 'Frame Guards',       'frame-guards',               20),
  ('crash-protection', 'Engine Guards',      'engine-guards',              30),
  ('crash-protection', 'Axle Sliders',       'axle-sliders',               40),
  ('crash-protection', 'Disc Guards',        'disc-guards',                50),
  -- Exhaust
  ('exhaust', 'Exhaust Systems',             'exhaust-systems',            10),
  ('exhaust', 'Mufflers & Slip-Ons',         'mufflers-slip-ons',          20),
  ('exhaust', 'Head Pipes',                  'head-pipes',                 30),
  ('exhaust', 'End Caps',                    'exhaust-end-caps',           40),
  ('exhaust', 'Exhaust Gaskets & Hardware',  'exhaust-gaskets-hardware',   50),
  ('exhaust', 'Spark Arrestors',             'spark-arrestors',            60),
  -- Intake
  ('intake', 'Air Filters',                  'air-filters',                10),
  ('intake', 'Reed Valves',                  'reed-valves',                20),
  ('intake', 'Intake Accessories',           'intake-accessories',         30),
  -- Fuel System
  ('fuel-system', 'Fuel Pumps',              'fuel-pumps',                 30),
  ('fuel-system', 'Petcocks & Lines',        'petcocks-lines',             40),
  ('fuel-system', 'Fuel Filters',            'fuel-filters',               50),
  -- Electrical & Batteries
  ('electrical-batteries', 'Batteries',      'batteries',                  10),
  ('electrical-batteries', 'Chargers',       'chargers',                   20),
  ('electrical-batteries', 'Lighting',       'lighting',                   30),
  ('electrical-batteries', 'Ignition & Switches','ignition-switches',      40),
  -- Hardware & Bearings
  ('hardware-bearings', 'Bolt Kits',         'bolt-kits',                  10),
  ('hardware-bearings', 'Fasteners',         'fasteners',                  20),
  ('hardware-bearings', 'General Bearings & Seals','general-bearings-seals',30),
  -- Tools & Maintenance
  ('tools-maintenance', 'Oils & Fluids',     'oils-fluids',                20),
  ('tools-maintenance', 'Chemicals & Lube',  'chemicals-lube',             30),
  ('tools-maintenance', 'Cleaning',          'cleaning',                   40),
  ('tools-maintenance', 'Stands & Ramps',    'stands-ramps',               50),
  ('tools-maintenance', 'Manuals',           'manuals',                    60),
  ('tools-maintenance', 'Sealants',          'sealants',                   70),
  -- Accessories
  ('accessories', 'Graphics',                'graphics',                   10),
  ('accessories', 'Luggage',                 'luggage',                    20),
  ('accessories', 'Windshields',             'windshields',                30),
  ('accessories', 'Winch & Plow',            'winch-plow',                 40)
) as v(parent_slug, name, slug, ord)
join categories p on p.slug = v.parent_slug and p.parent_id is null
where not exists (select 1 from categories c where c.slug = v.slug);

commit;

-- 4) Check the result: shows each department followed by its subcategories.
select coalesce(p.name, c.name) as department,
       case when p.id is null then '(department)' else c.name end as subcategory,
       c.id, c.slug
from categories c
left join categories p on p.id = c.parent_id
order by coalesce(p.sort_order, c.sort_order),
         (c.parent_id is not null),
         c.sort_order;
