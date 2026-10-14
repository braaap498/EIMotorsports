-- Fitment parser v2. Run this INSTEAD of re-running the old parser migration; it replaces the function.
-- Improvements over v1:
--   * splits a title into model + submodel (variant), e.g. "RANCHER 420" + "4X4 ES", so one model is not
--     fragmented into a dozen near-duplicates
--   * reads two-digit years ("89-04", "95 96 97") when there is no four-digit year
--   * finds the model when it comes AFTER product words ("Arctic Cat Pull Starter Recoil ... ZR 8000")
--   * stops at a second make, so multi-make titles ("Emgo Oil Filter Honda Yamaha Kawasaki") get no model
--   * drops pack counts like "(3)" and repeated model codes ("KFX 450R KFX450R")

begin;

create or replace function fitment_key(p text)
returns text
language sql
immutable
as $$ select upper(regexp_replace(coalesce(p, ''), '[^A-Za-z0-9]', '', 'g')) $$;

drop function if exists parse_fitment_title(text);

create function parse_fitment_title(p_title text)
returns table (o_make text, o_model text, o_submodel text, o_years int[])
language plpgsql
immutable
as $fn$
declare
  t text;
  toks text[];
  n int;
  i int;
  j int;
  q int;
  idx int;
  mpos int;
  tok text;
  prev text;
  k text;
  acc text;
  mk text;
  dig text;
  first_tok text;
  cand text[];
  kept text[];
  core text[];
  subt text[];
  codes text[];
  m text[];
  yrs int[] := '{}';
  run int[];
  y1 int;
  y2 int;
  ok boolean;
  dup boolean;
  leading_skip boolean;
  code_re text := '^[A-Z]{1,5}-?[0-9]{2,}[A-Z0-9-]*$';
  makes_tok text[] := array[
    'HONDA','YAMAHA','KAWASAKI','SUZUKI','POLARIS','KTM','HUSQVARNA','BETA','GASGAS',
    'CANAM','SKIDOO','SEADOO','ARCTICCAT','HARLEYDAVIDSON','TRIUMPH','APRILIA','DUCATI',
    'BMW','INDIAN','VICTORY','HUSABERG','SHERCO','CFMOTO','KYMCO'];
  makes_name text[] := array[
    'Honda','Yamaha','Kawasaki','Suzuki','Polaris','KTM','Husqvarna','Beta','Gas Gas',
    'Can-Am','Ski-Doo','Sea-Doo','Arctic Cat','Harley-Davidson','Triumph','Aprilia','Ducati',
    'BMW','Indian','Victory','Husaberg','Sherco','CFMoto','Kymco'];
  -- trailing words that are a variant of a model rather than part of its name
  variants text[] := array['ABS','SE','LE','ES','EPS','DCT','AUTO','AUTOMATIC','4X4','2X4','6X6','2WD','4WD','LTD'];
  -- product words, brands and colors: skipped before the model, and they end the model once it has started
  stops text[] := array[
    'FITS','FIT','FOR','WITH','COMPATIBLE','AND','PACK',
    'TUSK','WISECO','NAMURA','VERTEX','PROX','HOT','CAMS','HIFLO','HI-FLO','HIFLOFILTRO','ALL','BALLS','MOOSE',
    'GATES','G-FORCE','SPI','COMP','SERIES','EMGO',
    'PISTON','PISTONS','RINGS','RING','GASKET','GASKETS','KIT','KITS','SET','SETS','TOP','END',
    'ENGINE','COMPLETE','FULL','CRANK','SEALS','SEAL','BEARING','BEARINGS',
    'CYLINDER','WRIST','PIN','CAGED','NEEDLE','REBUILD','REPAIR','REBUILT','STD','STOCK','BORE','OVERSIZE',
    'MOTOR','HEAD',
    'VALVE','SHIM','SHIMS','OIL','FILTER','FILTERS','RADIATOR','CAP','TEMP','TEMPERATURE','GAUGE',
    'HIGH','PRESSURE','CLUTCH','LEVER','ASSY','ASSEMBLY','QUICK','PERCH','SHIFT','SHIFTER','FOOT',
    'GEAR','PEDAL','TIP','DRIVE','BELT','CVT','STEERING','STEM','FORK','DUST','WIPERS',
    'CARBURETOR','CARB','BOOT','INTAKE','FLANGE','JET','COLD','START','MAIN','PILOT',
    'RECOIL','PULL','STARTER','REWIND','FRONT','REAR','BUMPER','NERF','BARS','BAR','WEBBING',
    'GRAPHICS','BAG','LUGGAGE','FUEL','GAS','TANK','BILLET','ALUMINUM','TEAM',
    'RED','BLUE','GREEN','ORANGE','BLACK','SILVER','WHITE','PURPLE','YELLOW','PINK',
    'UTV','ATV','TRANS','FAN-COOLED','EDITION','MOTORCYCLE','MX','DIRT','BIKE','QUAD'];
begin
  o_make := null;
  o_model := '';
  o_submodel := '';
  o_years := '{}';

  t := upper(coalesce(p_title, ''));
  t := regexp_replace(t, '[–—‒]', '-', 'g');           -- en/em dashes -> hyphen
  t := regexp_replace(t, '\(\s*\d+\s*\)', ' ', 'g');    -- "(3)" piston counts
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

  -- years: four-digit ranges, then four-digit singles
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

  toks := regexp_split_to_array(btrim(t), '\s+');
  n := coalesce(array_length(toks, 1), 0);

  -- no four-digit year: try "89-04" ranges, then a trailing run of consecutive two-digit years ("95 96 97")
  if coalesce(array_length(yrs, 1), 0) = 0 then
    for m in select regexp_matches(t, '\m(\d{2})-(\d{2})\M', 'g') loop
      y1 := case when m[1]::int >= 70 then 1900 + m[1]::int else 2000 + m[1]::int end;
      y2 := case when m[2]::int >= 70 then 1900 + m[2]::int else 2000 + m[2]::int end;
      if y2 >= y1 and y2 - y1 <= 45 then
        yrs := yrs || array(select generate_series(y1, y2));
      end if;
    end loop;

    if coalesce(array_length(yrs, 1), 0) = 0 then
      run := '{}';
      j := n;
      while j >= 1 and toks[j] ~ '^[0-9]{2}$' loop
        run := toks[j]::int || run;
        j := j - 1;
      end loop;
      if coalesce(array_length(run, 1), 0) >= 2 then
        ok := true;
        for q in 2..array_length(run, 1) loop
          if run[q] <> run[q - 1] + 1 then
            ok := false;
          end if;
        end loop;
        if ok then
          for q in 1..array_length(run, 1) loop
            yrs := yrs || (case when run[q] >= 70 then 1900 + run[q] else 2000 + run[q] end);
          end loop;
        end if;
      end if;
    end if;
  end if;

  select coalesce(array_agg(distinct x order by x), '{}'::int[]) into o_years from unnest(yrs) as x;

  -- make, model and submodel
  for i in 1..n loop
    mpos := array_position(makes_tok, toks[i]);
    if mpos is null then
      continue;
    end if;

    cand := '{}';
    leading_skip := true;
    for j in i + 1..n loop
      tok := toks[j];
      if tok = toks[i] then continue; end if;                                       -- make repeated
      if array_position(makes_tok, tok) is not null then exit; end if;             -- a second make: multi-make title
      if tok ~ '^(19[6-9][0-9]|20[0-3][0-9])(-(19[6-9][0-9]|20[0-3][0-9]))?$' then exit; end if;
      if tok = any(stops) or tok = '-' then
        if leading_skip then continue; end if;                                      -- product words before the model
        exit;
      end if;
      if tok ~ '[0-9]MM$' or tok ~ '^[0-9]+\.[0-9]+' or tok ~ '^[0-9]+:[0-9]+$' or tok ~ '^[0-9]{6,}$' then
        if leading_skip then continue; end if;                                      -- sizes, ratios, part numbers
        exit;
      end if;
      leading_skip := false;
      cand := cand || tok;
      if array_length(cand, 1) = 6 then exit; end if;
    end loop;

    if o_make is null then
      o_make := makes_name[mpos];
    end if;

    if coalesce(array_length(cand, 1), 0) > 0 then
      -- drop tokens that just repeat an earlier token or the sum of the earlier ones ("KFX 450R KFX450R")
      kept := '{}';
      acc := '';
      foreach tok in array cand loop
        k := fitment_key(tok);
        if k = '' then continue; end if;
        dup := (acc <> '' and k = acc);
        if not dup then
          foreach prev in array kept loop
            if fitment_key(prev) = k then
              dup := true;
              exit;
            end if;
          end loop;
        end if;
        if not dup then
          kept := kept || tok;
          acc := acc || k;
        end if;
      end loop;

      if coalesce(array_length(kept, 1), 0) = 0 then
        continue;
      end if;

      o_make := makes_name[mpos];
      first_tok := kept[1];
      core := '{}';
      subt := '{}';

      if first_tok ~ code_re then
        -- a model code like KX60 or CRF150R is the whole model
        core := array[first_tok];
      elsif first_tok ~ '^[0-9]+$' then
        -- numeric-first names like "350 BIG BEAR": codes (YFM350ER) become the submodel
        codes := '{}';
        foreach tok in array kept loop
          if tok ~ code_re then
            codes := codes || tok;
          else
            core := core || tok;
          end if;
        end loop;
        subt := codes;
        while coalesce(array_length(core, 1), 0) > 1 loop
          tok := core[array_length(core, 1)];
          dup := (tok = any(variants));
          if not dup and tok ~ '^[A-Z]{1,4}$' then
            foreach prev in array codes loop
              if left(prev, length(tok)) = tok then
                dup := true;
                exit;
              end if;
            end loop;
          end if;
          exit when not dup;
          subt := tok || subt;
          core := core[1:array_length(core, 1) - 1];
        end loop;
      else
        -- word names: the model ends at the first displacement number (RANCHER 420, OUTLANDER MAX 570);
        -- everything after it (4X4, ES, XT-P ...) is the submodel
        idx := null;
        for q in 2..array_length(kept, 1) loop
          if kept[q] ~ '^[0-9]{2,4}[A-Z]{0,2}$' then
            idx := q;
            exit;
          end if;
        end loop;
        if idx is not null then
          core := kept[1:idx];
          if idx < array_length(kept, 1) then
            subt := kept[idx + 1:array_length(kept, 1)];
          end if;
        else
          core := kept;
          while coalesce(array_length(core, 1), 0) > 1 and core[array_length(core, 1)] = any(variants) loop
            subt := core[array_length(core, 1)] || subt;
            core := core[1:array_length(core, 1) - 1];
          end loop;
        end if;
      end if;

      mk := fitment_key(array_to_string(core, ' '));
      dig := regexp_replace(mk, '[^0-9]', '', 'g');
      o_model := array_to_string(core, ' ');
      o_submodel := '';
      foreach tok in array subt loop
        if fitment_key(tok) <> mk and tok <> dig then
          o_submodel := btrim(o_submodel || ' ' || tok);
        end if;
      end loop;
      exit;
    end if;
  end loop;

  return next;
end;
$fn$;

-- The staging table gains a submodel column (kept as a note on each product's fitment).
alter table fitment_parse_review add column if not exists submodel_raw text;

commit;
