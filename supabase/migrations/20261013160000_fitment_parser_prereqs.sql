-- Prerequisites for year-make-model fitment.
-- Uses your EXISTING tables: vehicle_types, vehicle_makes, vehicle_models, vehicles, product_fitment.
-- Adds: a title-parsing function, a private staging table, and indexes.

begin;

-- 1) Title parser: pulls make, model and model years out of a product title.
--    Handles year lists ("2008 2009"), ranges ("1984-1987", "2021–2026"), year-first and
--    year-last titles, and "KX60 KX 60" style repeats.
create or replace function parse_fitment_title(p_title text)
returns table (o_make text, o_model text, o_years int[])
language plpgsql
immutable
as $fn$
declare
  t text;
  toks text[];
  n int;
  i int;
  j int;
  mpos int;
  tok text;
  cand text[];
  m text[];
  y1 int;
  y2 int;
  yrs int[] := '{}';
  makes_tok text[] := array[
    'HONDA','YAMAHA','KAWASAKI','SUZUKI','POLARIS','KTM','HUSQVARNA','BETA','GASGAS',
    'CANAM','SKIDOO','SEADOO','ARCTICCAT','HARLEYDAVIDSON','TRIUMPH','APRILIA','DUCATI',
    'BMW','INDIAN','VICTORY','HUSABERG','SHERCO','CFMOTO','KYMCO'];
  makes_name text[] := array[
    'Honda','Yamaha','Kawasaki','Suzuki','Polaris','KTM','Husqvarna','Beta','Gas Gas',
    'Can-Am','Ski-Doo','Sea-Doo','Arctic Cat','Harley-Davidson','Triumph','Aprilia','Ducati',
    'BMW','Indian','Victory','Husaberg','Sherco','CFMoto','Kymco'];
  -- Words that end the model name (product words, brands, colors)
  stops text[] := array[
    'FITS','FIT','FOR','WITH','COMPATIBLE','AND','PACK',
    'TUSK','WISECO','NAMURA','VERTEX','PROX','HOT','CAMS','HIFLO','HI-FLO','ALL','BALLS','MOOSE',
    'GATES','G-FORCE','SPI','COMP','SERIES',
    'PISTON','PISTONS','RINGS','RING','GASKET','GASKETS','KIT','KITS','SET','SETS','TOP','END',
    'ENGINE','COMPLETE','FULL','CRANK','SEALS','SEAL','BEARING','BEARINGS',
    'CYLINDER','WRIST','PIN','REBUILD','REPAIR','REBUILT','STD','STOCK','BORE','OVERSIZE',
    'VALVE','SHIM','SHIMS','OIL','FILTER','FILTERS','RADIATOR','CAP','TEMP','TEMPERATURE','GAUGE',
    'HIGH','PRESSURE','CLUTCH','LEVER','ASSY','ASSEMBLY','QUICK','PERCH','SHIFT','SHIFTER','FOOT',
    'GEAR','PEDAL','TIP','DRIVE','BELT','CVT','STEERING','STEM','HEAD','FORK','DUST','WIPERS',
    'CARBURETOR','CARB','BOOT','INTAKE','FLANGE','JET','COLD','START','MAIN','PILOT',
    'RECOIL','PULL','STARTER','REWIND','FRONT','REAR','BUMPER','NERF','BARS','BAR','WEBBING',
    'GRAPHICS','BAG','LUGGAGE','FUEL','GAS','TANK','BILLET','ALUMINUM','TEAM',
    'RED','BLUE','GREEN','ORANGE','BLACK','SILVER','WHITE','PURPLE','YELLOW','PINK',
    'UTV','ATV','TRANS','FAN-COOLED'];
begin
  o_make := null;
  o_model := '';
  o_years := '{}';

  t := upper(coalesce(p_title, ''));
  t := regexp_replace(t, '[–—‒]', '-', 'g');          -- en/em dashes -> hyphen
  t := regexp_replace(t, '[*(),;"#/]', ' ', 'g');
  -- multiword makes become one token
  t := regexp_replace(t, '\mCAN[ -]AM\M', 'CANAM', 'g');
  t := regexp_replace(t, '\mSKI[ -]DOO\M', 'SKIDOO', 'g');
  t := regexp_replace(t, '\mSEA[ -]DOO\M', 'SEADOO', 'g');
  t := regexp_replace(t, '\mARCTIC[ -]CAT\M', 'ARCTICCAT', 'g');
  t := regexp_replace(t, '\mGAS[ -]GAS\M', 'GASGAS', 'g');
  t := regexp_replace(t, '\mHARLEY[ -]DAVIDSON\M', 'HARLEYDAVIDSON', 'g');
  t := regexp_replace(t, '\mHUSKY\M', 'HUSQVARNA', 'g');
  -- pack counts and shim counts are not part of the model
  t := regexp_replace(t, '\m\d{1,2}[ -]?PACK\M', ' ', 'g');
  t := regexp_replace(t, '\mPACK OF \d+\M', ' ', 'g');
  t := regexp_replace(t, '\m\d{1,3} SHIMS?\M', ' ', 'g');

  -- years: ranges first, then single years (duplicates are removed below)
  for m in select regexp_matches(t, '\m(19[6-9][0-9]|20[0-3][0-9])\s*-\s*(19[6-9][0-9]|20[0-3][0-9])\M', 'g') loop
    y1 := m[1]::int;
    y2 := m[2]::int;
    if y2 >= y1 and y2 - y1 <= 45 then
      yrs := yrs || array(select generate_series(y1, y2));
    end if;
  end loop;
  for m in select regexp_matches(t, '\m(19[6-9][0-9]|20[0-3][0-9])\M', 'g') loop
    yrs := yrs || m[1]::int;
  end loop;
  select coalesce(array_agg(distinct x order by x), '{}'::int[]) into o_years from unnest(yrs) as x;

  -- make and model
  toks := regexp_split_to_array(btrim(t), '\s+');
  n := coalesce(array_length(toks, 1), 0);

  for i in 1..n loop
    mpos := array_position(makes_tok, toks[i]);
    if mpos is null then
      continue;
    end if;

    cand := '{}';
    for j in i + 1..n loop
      tok := toks[j];
      if tok = toks[i] then continue; end if;                                   -- make repeated
      if tok ~ '^(19[6-9][0-9]|20[0-3][0-9])(-(19[6-9][0-9]|20[0-3][0-9]))?$' then exit; end if;
      if tok = any(stops) or tok = '-' then exit; end if;
      if tok ~ '[0-9]MM$' or tok ~ '^[0-9]+\.[0-9]+' or tok ~ '^[0-9]+:[0-9]+$' or tok ~ '^[0-9]{6,}$' then
        exit;                                                                   -- sizes, ratios, part numbers
      end if;
      cand := cand || tok;
      if array_length(cand, 1) = 4 then exit; end if;
    end loop;

    if o_make is null then
      o_make := makes_name[mpos];
    end if;

    if coalesce(array_length(cand, 1), 0) > 0 then
      o_make := makes_name[mpos];
      if cand[1] ~ '[A-Z]' and cand[1] ~ '[0-9]' then
        o_model := cand[1];                       -- a code like KX60 or CRF150R is the whole model
      else
        o_model := array_to_string(cand, ' ');    -- multiword names like OUTLANDER MAX 570
      end if;
      exit;
    end if;
  end loop;

  return next;
end;
$fn$;

-- 2) Staging table: one parsed proposal per product. Nothing in your real tables changes
--    until the apply script runs.
create table if not exists fitment_parse_review (
  product_id             bigint primary key references products(id) on delete cascade,
  title                  text,
  make_name              text,
  model_raw              text,
  model_key              text,       -- model with spaces/punctuation stripped, used to merge spellings
  years                  int[],
  spec_vehicle_type_slug text,       -- from the eBay Machine Type / Vehicle Type specifics, if any
  spec_make              text,       -- from the eBay Compatible Make specific, if any
  status                 text not null,   -- ok | universal | no_make | no_model | no_years
  created_at             timestamptz not null default now()
);
alter table fitment_parse_review enable row level security;   -- keep it private

-- 3) Indexes and uniqueness so duplicates cannot sneak in (all four tables are currently empty).
create unique index if not exists vehicle_models_make_slug_uidx on vehicle_models (make_id, slug);
create unique index if not exists vehicles_model_year_sub_uidx on vehicles (model_id, year, coalesce(submodel, ''));
create unique index if not exists product_fitment_product_vehicle_uidx on product_fitment (product_id, vehicle_id);
create index if not exists product_fitment_vehicle_idx on product_fitment (vehicle_id);

commit;
