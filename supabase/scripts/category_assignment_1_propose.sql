-- STAGE 1: propose a category for every product. Changes NOTHING in `products`;
-- it only fills category_assignment_review. Safe to re-run (it clears the table first,
-- which also resets any approvals).
--
-- Assumes product_item_specifics links to products via a column named product_id.
-- If you get 'column "product_id" does not exist', paste the error and I'll adjust.
--
-- Rule priority matters: piston/top-end beats gasket (top-end kits contain "gasket"),
-- clutch lever and shift lever are matched before the generic "lever" rule.

truncate category_assignment_review;

with spec as (
  select product_id, min(value) as type_value
  from product_item_specifics
  where name = 'Type'
  group by product_id
),
base as (
  select p.id as product_id,
         p.title,
         p.category_id as current_category_id,
         s.type_value,
         lower(s.type_value) as tv,
         lower(p.title) as t
  from products p
  left join spec s on s.product_id = p.id
),
matched as (
  select b.*,
    -- Category suggested by the eBay "Type" item specific (structured, most reliable)
    case
      when tv = 'radiator cap' then 'radiators-cooling'
      when tv in ('valve shim kit', 'engine valve shims', 'valve shims') then 'valve-train'
      when tv in ('clutch lever assembly', 'clutch & hand brake lever kit', 'lever') then 'levers'
      when tv in ('shifter', 'shift lever', 'gear shift lever', 'gear shifter lever') then 'footpegs-foot-controls'
      when tv in ('top end engine rebuild kit', 'top end rebuild kit', 'engine rebuild kit',
                  'complete cylinder', 'top end kit') or tv like 'piston kit%' then 'pistons-top-end-kits'
      when tv = 'cvt drive belt' then 'drive-belts'
      when tv = 'steering bearing & seal kit' then 'steering-stem-bearings'
      when tv = 'fork & dust seal kit' then 'fork-parts'
      when tv = 'oil filter' then 'oil-filters'
      when tv = 'fuel tank cap' then 'gas-caps'
    end as type_slug,
    -- Category suggested by title keywords, in priority order
    case
      when t ~ 'radiator cap'                                   then 'radiators-cooling'
      when t ~ 'valve shim'                                     then 'valve-train'
      when t ~ 'clutch lever'                                   then 'levers'
      when t ~ '(shift lever|shifter|gear shift)'               then 'footpegs-foot-controls'
      when t ~ 'steering (stem|head)'                           then 'steering-stem-bearings'
      when t ~ '(fork seal|fork & dust|fork and dust)'          then 'fork-parts'
      when t ~ '(\mpiston|top[ -]end)'                          then 'pistons-top-end-kits'
      when t ~ '(drive belt|cvt belt)'                          then 'drive-belts'
      when t ~ 'oil filter'                                     then 'oil-filters'
      when t ~ '(gas cap|fuel cap|fuel tank cap)'               then 'gas-caps'
      when t ~ '(recoil|pull start|rewind)'                     then 'starters-recoils'
      when t ~ '(\mcarburetor\M|\mcarb\M)'                      then 'carburetor-injection-parts'
      when t ~ '\mlever\M'                                      then 'levers'            -- weak
      when t ~ 'gasket'                                         then 'gaskets-seals'     -- weak
    end as title_slug,
    (t ~ '\muniversal\M') as proposed_universal
  from base b
),
decided as (
  select m.*,
    coalesce(type_slug, title_slug) as proposed_slug,
    case
      when type_slug is null and title_slug is null then 'unmatched'
      when type_slug is not null and title_slug is not null and type_slug <> title_slug then 'conflict'
      when type_slug is not null then 'high'              -- Type alone, or Type and title agree
      when title_slug in ('levers', 'gaskets-seals') then 'low'   -- weak title-only rules
      else 'medium'                                       -- strong title-only match
    end as confidence
  from matched m
)
insert into category_assignment_review
  (product_id, title, current_category_id, type_value, title_slug, type_slug,
   proposed_slug, proposed_category_id, proposed_part_type, proposed_universal, confidence)
select d.product_id, d.title, d.current_category_id, d.type_value, d.title_slug, d.type_slug,
       coalesce(d.proposed_slug, 'miscellaneous'),
       coalesce(c.id, (select id from categories where name = 'Miscellaneous' and parent_id is null)),
       d.type_value,
       d.proposed_universal,
       d.confidence
from decided d
left join categories c on c.slug = d.proposed_slug;
