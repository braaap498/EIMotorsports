"""
Convert compat to fitment: the back end of the "Convert to fitment" card on the admin eBay compat import tab.

Mounted at /admin/ebay-compat/fitment/* (owner/admin only).

  GET  /preview?include_existing=   dry run of the whole conversion (everything is rolled back); also lists the
                                    machines that are not in the year-make-model system yet
  POST /merge                       the same run, committed
  POST /decide                      settle machines from that list: map to an existing model, create it, or skip
  POST /decide-bulk                 accept all clear suggestions / create every open machine as a new model

It does the job of the manual SQL stages for the parts that are linked to eBay listings, using what the
compat import saved in ebay_compat_items (the eBay compatibility table AND the parsed_* columns that hold the
make / model / years read from the listing title):

  0. copies saved compat lists into ebay_compat_raw (new rows, and rows that are behind), so the SQL stages
     (fitment_5, 5b, 6) see these parts too                                              [like the import copy]
  1. eBay compatibility -> product_fitment, source 'ebay'                                [fitment_5 + 7]
  2. parts with NO usable eBay compatibility: title parse -> product_fitment, source 'title'  [fitment_1 + 3]
     with the same alias list as fitment_1; titles without years -> 'title_inferred', linked to the model's
     existing years only                                                                  [fitment_4]
  3. machine names are matched with fitment_key() through the SAME decisions table the SQL stages use
     (fitment_ebay_model_map), then by exact name. Years that a mapped model does not have yet are added
     (stage 3 / 7 do the same). Machines that are not in the system are NOT guessed: they are listed, and the
     /decide routes settle them by writing to the same map table, so the SQL stages stay in step.

Only ever ADDS: product_fitment rows, vehicles rows (model + year) and, through /decide only, makes and models.
Nothing is deleted. By default only parts that have no fitment yet are touched, so links removed by hand on
older parts are not put back. A run is one transaction; any error undoes all of it.
"""
import datetime
import logging
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.admin_catalog import finish
from app.admin_ebay_compat import _expire_stale
from app.auth import AuthUser, require_admin
from app.database import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-compat/fitment", tags=["admin-ebay-fitment"], dependencies=[Depends(require_admin)])

OPEN_LIST_LIMIT = 300
TIMEOUT = "set local statement_timeout = '600s'"

# Same alias list as fitment_1_parse.sql: (make, model key to merge from, model name to keep, variant note).
ALIASES = [
    ('Honda', 'CR125', 'CR125R', ''),
    ('Honda', 'CR250', 'CR250R', ''),
    ('Honda', 'CR80', 'CR80R', ''),
    ('Honda', 'FOURTRAX300', 'TRX300', ''),
    ('Honda', 'FOURTRAXTRX300', 'TRX300', ''),
    ('Honda', 'FOURTRAX400EX', 'TRX400EX', ''),
    ('Honda', 'SPORTRAX400EX', 'TRX400EX', ''),
    ('Honda', 'SPORTRAX250', 'TRX250EX', ''),
    ('Honda', '450FOREMAN', 'FOREMAN 450', ''),
    ('Honda', '450FOREMANS', 'FOREMAN 450', 'S'),
    ('Honda', 'FOREMANTRX450', 'FOREMAN 450', ''),
    ('Honda', '350RANCHER', 'RANCHER 350', ''),
    ('Honda', 'TRX520', 'FOREMAN 520', ''),
    ('Honda', '1800GOLDWING', 'GOLDWING 1800', ''),
    ('Kawasaki', 'CONCOURS1400', 'CONCOURS 14', ''),
    ('Kawasaki', 'VULCANCLASSIC800', 'VULCAN 800', 'CLASSIC'),
    ('Kawasaki', 'VULCANDRIFTER800', 'VULCAN 800', 'DRIFTER'),
    ('Kawasaki', 'VULCANCLASSIC900', 'VULCAN 900', 'CLASSIC'),
    ('Kawasaki', 'VULCANCLASSICLT900', 'VULCAN 900', 'CLASSIC LT'),
    ('Kawasaki', 'VULCANCUSTOM900', 'VULCAN 900', 'CUSTOM'),
    ('Kawasaki', 'NINJAZX7RZX750750', 'NINJA ZX-7R', ''),
    ('Kawasaki', 'NINJAZX14ZX14R1400', 'ZX-14', ''),
    ('Suzuki', 'LTZZ400', 'LTZ400', ''),
    ('Suzuki', 'Z400', 'LTZ400', ''),
    ('Suzuki', 'GSXR750GSXR750', 'GSXR 750', ''),
    ('Suzuki', 'GSXR1100GSXR1100', 'GSXR 1100', ''),
    ('Suzuki', '700KINGQUAD', 'KING QUAD 700', ''),
    ('Yamaha', 'RAPTOR660', 'RAPTOR 660R', ''),
    ('Yamaha', 'RAPTOR700', 'RAPTOR 700R', ''),
    ('Yamaha', '350BIGBEAR', 'BIG BEAR 350', ''),
    ('Yamaha', '650VSTARCLASSIC', 'V-STAR 650', 'CLASSIC'),
    ('Yamaha', '650VSTARCLASSICXVS650A', 'V-STAR 650', 'CLASSIC'),
    ('Yamaha', '650VSTARCUSTOM', 'V-STAR 650', 'CUSTOM'),
    ('Yamaha', '650VSTARMIDNIGHTCUSTOM', 'V-STAR 650', 'MIDNIGHT CUSTOM'),
    ('Yamaha', '650VSTARMIDNIGHTCUSTOMXVS', 'V-STAR 650', 'MIDNIGHT CUSTOM'),
    ('Yamaha', '650VSTARSILVERADO', 'V-STAR 650', 'SILVERADO'),
    ('Yamaha', 'VIRAGO700C', 'VIRAGO 700', 'C'),
    ('Yamaha', 'VIRAGO700S', 'VIRAGO 700', 'S'),
    ('Arctic Cat', 'F6600', 'F6', '600'),
    ('Arctic Cat', 'F6F6600', 'F6', '600'),
    ('Arctic Cat', 'F6FIRECAT600', 'F6', 'FIRECAT 600'),
    ('Arctic Cat', 'F6LXR600', 'F6', 'LXR 600'),
    ('Arctic Cat', 'F6SNOPRO', 'F6', 'SNO PRO'),
    ('Arctic Cat', 'F6SNOPROFIRECAT600', 'F6', 'SNO PRO FIRECAT 600'),
    ('Arctic Cat', 'F7FIRECAT700', 'F7', 'FIRECAT 700'),
    ('Arctic Cat', 'F7SNOPRO', 'F7', 'SNO PRO'),
    ('Arctic Cat', 'F8LXR', 'F8', 'LXR'),
    ('Arctic Cat', 'F8NIGHTFIRESNOPRO', 'F8', 'NIGHTFIRE SNO PRO'),
    ('Arctic Cat', 'F8SNOPRO', 'F8', 'SNO PRO'),
    ('Arctic Cat', 'F8TONYSTEWART', 'F8', 'TONY STEWART'),
    ('Arctic Cat', 'M6M6600', 'M6', '600'),
    ('Arctic Cat', 'M7EFI', 'M7', 'EFI'),
    ('Arctic Cat', 'M8HCR', 'M8', 'HCR'),
    ('Arctic Cat', 'M8SNOPRO', 'M8', 'SNO PRO'),
    ('Arctic Cat', 'M8SNOPRONIGHTFIRE', 'M8', 'SNO PRO NIGHTFIRE'),
]


def _q(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


_ALIAS_VALUES = ",\n".join("(" + ", ".join(_q(x) for x in a) + ")" for a in ALIASES)

_DIGITS = "regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g')"
# the first import stored some lists as a JSON string; read both forms the same way
_LIST = ("(case when jsonb_typeof(cast({c}.compat_list as jsonb)) = 'string' "
         "then cast(cast({c}.compat_list as jsonb) #>> '{{}}' as jsonb) "
         "else cast({c}.compat_list as jsonb) end)")


def _sql(s: str):
    s = (s.replace("@D@", _DIGITS).replace("@LIST_c@", _LIST.format(c="c")).replace("@LIST_r@", _LIST.format(c="r"))
          .replace("@ALIASES@", _ALIAS_VALUES))
    return text(s)


# ------------------------------------------------------------------ step 0: raw table sync
_SYNC_INSERT = _sql("""
    insert into ebay_compat_raw (channel_listing_id, ebay_item_id, status, compat_count, compat_list,
                                 error_code, error_message, fetched_at)
    select cl.id, c.item_id, c.status, c.compat_count, @LIST_c@, c.error_code, c.error_message, c.fetched_at
    from ebay_compat_items c
    join channel_listings cl on @D@ = c.item_id
    where not exists (select 1 from ebay_compat_raw r where r.channel_listing_id = cl.id)
    on conflict (channel_listing_id) do nothing
""")

_SYNC_UPDATE = _sql("""
    update ebay_compat_raw r
    set status = c.status, compat_count = c.compat_count, compat_list = @LIST_c@,
        fetched_at = coalesce(c.changed_at, c.fetched_at, now())
    from ebay_compat_items c
    join channel_listings cl on @D@ = c.item_id
    where r.channel_listing_id = cl.id and (
            r.status is distinct from c.status
         or r.compat_count is distinct from c.compat_count
         or @LIST_r@ is distinct from @LIST_c@)
""")

# ------------------------------------------------------------------ the working tables (all temporary)
_SCOPE = _sql("""
    create temp table _it on commit drop as
    select cl.product_id, c.item_id, c.status, @LIST_c@ as lst,
           c.parsed_make, c.parsed_model, c.parsed_submodel, c.parsed_years, c.parsed_status
    from channel_listings cl
    join ebay_compat_items c on @D@ = c.item_id
    join products p on p.id = cl.product_id
    where not coalesce(p.is_universal, false)
      and not coalesce(p.fitment_exempt, false)
      and (cast(:inc as boolean)
           or not exists (select 1 from product_fitment pf where pf.product_id = cl.product_id))
""")

# stage 5 flatten: one row per part + make + model + submodel + year
_EBAY = _sql("""
    create temp table _eb on commit drop as
    select distinct i.product_id,
           btrim(x.e -> 'attrs' ->> 'Make')  as make,
           btrim(x.e -> 'attrs' ->> 'Model') as model,
           nullif(btrim(x.e -> 'attrs' ->> 'Submodel'), '') as sub,
           cast(x.e -> 'attrs' ->> 'Year' as int) as yr
    from _it i
    cross join lateral jsonb_array_elements(
        case when jsonb_typeof(i.lst) = 'array' then i.lst else cast('[]' as jsonb) end) as x(e)
    where i.status = 'ok'
      and coalesce(btrim(x.e -> 'attrs' ->> 'Make'), '')  <> ''
      and coalesce(btrim(x.e -> 'attrs' ->> 'Model'), '') <> ''
      and (x.e -> 'attrs' ->> 'Year') ~ '^(19|20)[0-9]{2}$'
""")

# stage 1 title parse (already done by the import tab into parsed_*), with fitment_1's aliases and DR-Z fix.
# Only for parts eBay gave no usable compatibility for: where eBay has data it is the reference (as in fitment_6).
_TITLE = _sql("""
    create temp table _alias (make_name text, from_key text, to_name text, new_sub text) on commit drop;
    insert into _alias values @ALIASES@;
    create temp table _tt on commit drop as
    select distinct t.product_id, t.make,
           case when fitment_key(t.make) = 'SUZUKI' and fitment_key(t.model) = 'DRZ400'
                     and t.sub in ('400E', '400S', '400SM') then 'DRZ' || t.sub else t.model end as model,
           case when fitment_key(t.make) = 'SUZUKI' and fitment_key(t.model) = 'DRZ400'
                     and t.sub in ('400E', '400S', '400SM') then null else t.sub end as sub,
           t.years
    from (
      select i.product_id, i.parsed_make as make,
             coalesce(a.to_name, i.parsed_model) as model,
             nullif(btrim(coalesce(a.new_sub, '') || ' ' || coalesce(i.parsed_submodel, '')), '') as sub,
             i.parsed_years as years
      from _it i
      left join _alias a on a.make_name = i.parsed_make and a.from_key = fitment_key(i.parsed_model)
      where i.parsed_status in ('ok', 'no_years')
        and coalesce(i.parsed_make, '') <> '' and coalesce(i.parsed_model, '') <> ''
        and not exists (select 1 from _eb e where e.product_id = i.product_id)
    ) t
""")

# every candidate: part + machine name + year + where it came from
_CAND = _sql("""
    create temp table _cand on commit drop as
    select product_id, make, model, sub, yr, src,
           fitment_key(make) as make_key, fitment_key(model) as model_key
    from (
      select product_id, make, model, sub, yr, 'ebay' as src from _eb
      union all
      select product_id, make, model, sub, y, 'title'
      from _tt cross join lateral unnest(years) as u(y) where coalesce(array_length(years, 1), 0) > 0
      union all
      select product_id, make, model, sub, null, 'title_inferred'
      from _tt where coalesce(array_length(years, 1), 0) = 0
    ) q
    where yr is null or (yr between 1900 and cast(:maxyear as int))
""")

_PAIRS = _sql("""
    create temp table _pairs on commit drop as
    select make_key, model_key,
           mode() within group (order by make)  as make_name,
           mode() within group (order by model) as model_name,
           count(distinct product_id) as products_n, count(*) as rows_n
    from _cand where make_key <> '' and model_key <> ''
    group by make_key, model_key;
    create temp table _res on commit drop as
    select p.*,
           case when m.skip then null
                when exists (select 1 from vehicle_models z where z.id = m.model_id) then m.model_id
                when ex.n = 1 then ex.model_id end as model_id,
           coalesce(m.skip, false) as skip,
           coalesce(ex.n, 0) as exact_n,
           exists (select 1 from vehicle_makes vm where fitment_key(vm.name) = p.make_key) as make_exists
    from _pairs p
    left join fitment_ebay_model_map m on m.make_key = p.make_key and m.model_key = p.model_key
    left join lateral (
        select min(vmo.id) as model_id, count(*) as n
        from vehicle_makes vm join vehicle_models vmo on vmo.make_id = vm.id
        where fitment_key(vm.name) = p.make_key and fitment_key(vmo.name) = p.model_key) ex on true
""")

_OPEN = _sql("""
    select r.make_key, r.model_key, r.make_name, r.model_name, r.products_n, r.rows_n, r.make_exists,
           r.exact_n > 1 as ambiguous, s.model_id as sug_id, s.name as sug_name, s.make as sug_make, s.score as sug_score
    from _res r
    left join lateral (
        select vmo.id as model_id, vmo.name, vm.name as make,
               greatest(
                 similarity(fitment_key(vmo.name), r.model_key),
                 case when length(fitment_key(vmo.name)) >= 3 and position(fitment_key(vmo.name) in r.model_key) > 0 then 0.8 else 0 end,
                 case when length(r.model_key) >= 3 and position(r.model_key in fitment_key(vmo.name)) > 0 then 0.8 else 0 end,
                 case when exists (select 1 from _cand c where c.make_key = r.make_key and c.model_key = r.model_key
                                   and c.sub is not null and fitment_key(c.sub) = fitment_key(vmo.name)) then 0.95 else 0 end
               ) as score
        from vehicle_makes vm join vehicle_models vmo on vmo.make_id = vm.id
        where fitment_key(vm.name) = r.make_key
        order by score desc limit 1) s on s.score >= 0.4
    where r.model_id is null and not r.skip
    order by r.products_n desc, r.make_name, r.model_name
""")

_FINAL = _sql("""
    create temp table _fin on commit drop as
    select k.product_id, r.model_id, k.yr, k.src,
           case k.src
             when 'ebay' then case when count(k.sub) > 0
                  then 'ebay: ' || left(string_agg(distinct k.sub, ', ' order by k.sub), 400) end
             when 'title' then left(string_agg(distinct k.sub, ', ' order by k.sub), 400)
             else btrim('possible fit: year not in title, linked to all known years '
                        || coalesce(left(string_agg(distinct k.sub, ', ' order by k.sub), 300), ''))
           end as notes
    from _cand k
    join _res r on r.make_key = k.make_key and r.model_key = k.model_key
    where r.model_id is not null
    group by k.product_id, r.model_id, k.yr, k.src
""")

_NEW_YEARS = _sql("""
    insert into vehicles (model_id, year)
    select distinct f.model_id, cast(f.yr as smallint) from _fin f
    where f.yr is not null
      and not exists (select 1 from vehicles v where v.model_id = f.model_id and v.year = f.yr and v.submodel is null)
    on conflict do nothing
    returning model_id, year
""")

_LINKS = _sql("""
    create temp table _links on commit drop as
    select distinct on (f.product_id, v.id) f.product_id, v.id as vehicle_id, f.src, f.notes
    from _fin f
    join vehicles v on v.model_id = f.model_id and v.submodel is null and (f.yr is null or v.year = f.yr)
    order by f.product_id, v.id, case f.src when 'ebay' then 1 when 'title' then 2 else 3 end
""")

_ADD = _sql("""
    with ins as (
      insert into product_fitment (product_id, vehicle_id, notes, source)
      select l.product_id, l.vehicle_id, l.notes, l.src from _links l
      where not exists (select 1 from product_fitment pf where pf.product_id = l.product_id and pf.vehicle_id = l.vehicle_id)
      returning product_id, source)
    select source, count(*) as n, count(distinct product_id) as products from ins group by source
""")


def _build(db: Session, include_existing: bool):
    params = {"inc": include_existing, "maxyear": datetime.date.today().year + 1}
    db.execute(_SCOPE, params)
    db.execute(_EBAY)
    for stmt in str(_TITLE).split(";"):  # two statements in one text(); run them one at a time
        if stmt.strip():
            db.execute(text(stmt))
    db.execute(_CAND, params)
    # the suggestion query looks machines up in _cand once per candidate model; without this it scans the table each time
    db.execute(text("create index on _cand (make_key, model_key)"))
    db.execute(text("analyze _cand"))
    for stmt in str(_PAIRS).split(";"):
        if stmt.strip():
            db.execute(text(stmt))


def _open_rows(db: Session, limit: Optional[int] = None) -> tuple:
    rows = db.execute(_OPEN).all()
    out = []
    for r in rows[:limit] if limit else rows:
        out.append({
            "make_key": r.make_key, "model_key": r.model_key, "make": r.make_name, "model": r.model_name,
            "products": r.products_n, "rows": r.rows_n, "make_exists": r.make_exists, "ambiguous": r.ambiguous,
            "suggestion": ({"model_id": r.sug_id, "name": r.sug_name, "make": r.sug_make,
                            "score": round(float(r.sug_score), 2)} if r.sug_id else None),
        })
    return out, len(rows)


def _scalar(db: Session, q: str) -> int:
    return db.execute(text(q)).scalar() or 0


def _run(db: Session, include_existing: bool) -> dict:
    """The whole conversion inside the open transaction. The caller commits (merge) or rolls back (preview)."""
    db.execute(text(TIMEOUT))
    copied = db.execute(_SYNC_INSERT).rowcount
    updated = db.execute(_SYNC_UPDATE).rowcount
    _build(db, include_existing)
    open_rows, open_total = _open_rows(db, OPEN_LIST_LIMIT)

    out = {
        "include_existing": include_existing,
        "raw_copied": copied, "raw_updated": updated,
        "parts_in_scope": _scalar(db, "select count(distinct product_id) from _it"),
        "parts_ebay": _scalar(db, "select count(distinct product_id) from _eb"),
        "parts_title": _scalar(db, "select count(distinct product_id) from _tt"),
        "parts_nothing": _scalar(db, "select count(*) from (select distinct product_id from _it) s where not exists "
                                     "(select 1 from _cand c where c.product_id = s.product_id)"),
        "open_total": open_total,
        "open_parts": _scalar(db, "select count(distinct c.product_id) from _cand c join _res r on r.make_key = c.make_key "
                                  "and r.model_key = c.model_key where r.model_id is null and not r.skip"),
        "skipped_rows": _scalar(db, "select coalesce(sum(rows_n), 0) from _res where skip"),
        "open": open_rows,
    }

    db.execute(_FINAL)
    new_years = db.execute(_NEW_YEARS).all()
    out["years_created"] = len(new_years)
    if new_years:
        by: dict[int, list[int]] = {}
        for r in new_years:
            by.setdefault(r.model_id, []).append(r.year)
        names = {r.id: f"{r.make} {r.name}" for r in db.execute(
            text("select vm.id, vm.name, mk.name as make from vehicle_models vm join vehicle_makes mk on mk.id = vm.make_id "
                 "where vm.id = any(cast(:ids as bigint[]))"), {"ids": list(by)[:2000]}).all()}
        top = sorted(by.items(), key=lambda kv: -len(kv[1]))[:15]
        out["years_created_sample"] = [{"model": names.get(m, str(m)), "years": sorted(ys)[:12], "count": len(ys)} for m, ys in top]
    else:
        out["years_created_sample"] = []

    db.execute(_LINKS)
    total = _scalar(db, "select count(*) from _links")
    out["added_parts"] = _scalar(
        db, "select count(distinct l.product_id) from _links l where not exists (select 1 from product_fitment pf "
            "where pf.product_id = l.product_id and pf.vehicle_id = l.vehicle_id)")
    added = {r.source: {"rows": r.n, "parts": r.products} for r in db.execute(_ADD).all()}
    out["links_total"] = total
    out["added"] = added
    out["added_rows"] = sum(v["rows"] for v in added.values())
    out["already_there"] = total - out["added_rows"]
    return out


def _refuse_if_running(db: Session):
    _expire_stale(db.execute)
    if db.execute(text("select 1 from ebay_compat_jobs where status in ('running','stopping') limit 1")).first():
        db.commit()
        raise HTTPException(409, "A compatibility import is running. Wait for it to finish or stop it first, "
                                 "so the conversion sees complete data.")


@router.get("/preview")
def preview(include_existing: bool = False, db: Session = Depends(get_db)):
    """Everything the conversion would do, worked out for real and then rolled back. Changes nothing."""
    try:
        out = _run(db, include_existing)
    except Exception as e:
        log.exception("compat -> fitment preview failed")
        raise HTTPException(500, f"The preview failed: {str(e)[:300]}")
    finally:
        db.rollback()
    return out


class MergeIn(BaseModel):
    include_existing: bool = False


@router.post("/merge")
def merge(body: MergeIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    _refuse_if_running(db)
    try:
        out = _run(db, body.include_existing)
        finish(db, refresh=True)
    except Exception as e:
        db.rollback()
        log.exception("compat -> fitment merge failed")
        raise HTTPException(500, f"The conversion failed and nothing was changed: {str(e)[:300]}")
    log.info("compat -> fitment by %s: %s", user.email, out["added"])
    return out


# ------------------------------------------------------------------ pickers for "Pick model"
@router.get("/makes")
def list_makes(db: Session = Depends(get_db)):
    rows = db.execute(text("select id, name from vehicle_makes order by name")).all()
    return {"items": [{"id": r.id, "name": r.name} for r in rows]}


@router.get("/models")
def list_models(q: str = "", make_id: Optional[int] = None, limit: int = 25, db: Session = Depends(get_db)):
    """Models for the picker: by make (browse) and/or a typed search that matches make or model name,
    ignoring spaces and punctuation (so 'raptor700' finds 'RAPTOR 700R')."""
    limit = max(1, min(limit, 500))
    q = q.strip()
    where, params = [], {"lim": limit}
    if make_id:
        where.append("vmo.make_id = :mid")
        params["mid"] = make_id
    if q:
        params["like"] = "%" + q.replace("%", "").replace("_", "") + "%"
        params["q"] = q
        where.append("(vmo.name ilike :like or vm.name || ' ' || vmo.name ilike :like "
                     "or (fitment_key(:q) <> '' and fitment_key(vm.name || vmo.name) like '%' || fitment_key(:q) || '%'))")
    sql = ("select vmo.id, vmo.name, vm.name as make from vehicle_models vmo join vehicle_makes vm on vm.id = vmo.make_id "
           + ("where " + " and ".join(where) + " " if where else "") + "order by vm.name, vmo.name limit :lim")
    rows = db.execute(text(sql), params).all()
    return {"items": [{"id": r.id, "name": r.name, "make": r.make} for r in rows]}


# ------------------------------------------------------------------ settle the machines that are not in the system
class Decision(BaseModel):
    make_key: str
    model_key: str
    make: str = ""
    model: str = ""
    action: Literal["map", "create", "skip"]
    model_id: Optional[int] = None  # map: the existing model to use
    type_id: Optional[int] = None  # create: optional vehicle type for the new model


def _slug(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", v.lower()).strip("-")


def _save_map(db: Session, d: Decision, model_id: Optional[int], skip: bool):
    db.execute(
        text("insert into fitment_ebay_model_map (make_key, model_key, make_name, model_name, model_id, skip, create_new) "
             "values (:mk, :ok, :mn, :on, :mid, :sk, false) "
             "on conflict (make_key, model_key) do update set model_id = excluded.model_id, skip = excluded.skip, "
             "create_new = false"),
        {"mk": d.make_key, "ok": d.model_key, "mn": d.make or None, "on": d.model or None, "mid": model_id, "sk": skip},
    )


def _create_model(db: Session, d: Decision) -> int:
    """Find or make the make and the model (never a duplicate by name ignoring spaces/punctuation)."""
    make = (d.make or "").strip()
    model = (d.model or "").strip()
    if not make or not model:
        raise HTTPException(400, "Make and model names are needed to create a model.")
    mk = db.execute(text("select id, slug from vehicle_makes where fitment_key(name) = :k order by id limit 1"),
                    {"k": d.make_key}).first()
    if not mk:
        slug = _slug(make) or "make"
        n = 1
        while db.execute(text("select 1 from vehicle_makes where slug = :s"), {"s": slug}).first():
            n += 1
            slug = f"{_slug(make)}-{n}"
        mk = db.execute(text("insert into vehicle_makes (name, slug) values (:n, :s) returning id, slug"),
                        {"n": make, "s": slug}).first()
    ex = db.execute(
        text("select id from vehicle_models where make_id = :m and fitment_key(name) = :k order by id limit 1"),
        {"m": mk.id, "k": d.model_key}).first()
    if ex:
        return ex.id
    base = _slug(model) or "model"
    slug = base
    n = 1
    while db.execute(text("select 1 from vehicle_models where make_id = :m and slug = :s"), {"m": mk.id, "s": slug}).first():
        n += 1
        slug = f"{base}-{n}"
    type_id = d.type_id
    if type_id is None:  # same defaults as fitment_3
        default = {"SKIDOO": "snowmobile", "SEADOO": "pwc"}.get(d.make_key)
        if default:
            type_id = db.execute(text("select id from vehicle_types where slug = :s"), {"s": default}).scalar()
    return db.execute(
        text("insert into vehicle_models (make_id, vehicle_type_id, name, slug) values (:m, :t, :n, :s) returning id"),
        {"m": mk.id, "t": type_id, "n": model, "s": slug}).scalar()


def _apply_decision(db: Session, d: Decision) -> str:
    chk = db.execute(text("select fitment_key(:m) as mk, fitment_key(:o) as ok"),
                     {"m": d.make or d.make_key, "o": d.model or d.model_key}).first()
    if d.action in ("create", "map") and d.make and d.model and (chk.mk != d.make_key or chk.ok != d.model_key):
        raise HTTPException(400, f"The names for {d.make_key}/{d.model_key} do not match their keys.")
    if d.action == "skip":
        _save_map(db, d, None, True)
        return "skipped"
    if d.action == "map":
        if not d.model_id or not db.execute(text("select 1 from vehicle_models where id = :i"), {"i": d.model_id}).first():
            raise HTTPException(404, "That model does not exist.")
        _save_map(db, d, d.model_id, False)
        return "mapped"
    mid = _create_model(db, d)
    _save_map(db, d, mid, False)
    return "created"


class DecideIn(BaseModel):
    decisions: list[Decision]


@router.post("/decide")
def decide(body: DecideIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    if not body.decisions or len(body.decisions) > 500:
        raise HTTPException(400, "Send between 1 and 500 decisions.")
    counts = {"mapped": 0, "created": 0, "skipped": 0}
    try:
        for d in body.decisions:
            counts[_apply_decision(db, d)] += 1
        finish(db, refresh=counts["created"] > 0)
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        log.exception("compat -> fitment decide failed")
        raise HTTPException(500, f"Nothing was saved: {str(e)[:300]}")
    log.info("compat -> fitment decisions by %s: %s", user.email, counts)
    return counts


class BulkIn(BaseModel):
    mode: Literal["accept_suggestions", "create_all"]
    min_score: float = 0.95
    include_existing: bool = False
    make_key: Optional[str] = None  # create_all: only this make (required for create_all)
    type_id: Optional[int] = None  # create_all: vehicle type for the new models (None = none, Ski-Doo / Sea-Doo defaults still apply)


@router.post("/decide-bulk")
def decide_bulk(body: BulkIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """accept_suggestions: map every open machine whose best guess scores at least min_score (default 0.95, the
    'eBay submodel equals your model' kind; lower scores are guesses and need a look).
    create_all: create every open machine of ONE make (make_key) as a new model from its own spelling, with the given
    type (not the ambiguous ones)."""
    if body.mode == "create_all" and not body.make_key:
        raise HTTPException(400, "Choose a make: new models are created one make at a time, so each gets the right type.")
    counts = {"mapped": 0, "created": 0, "skipped": 0, "left": 0}
    try:
        _build(db, body.include_existing)
        rows, _ = _open_rows(db)
        for r in rows:
            d = Decision(make_key=r["make_key"], model_key=r["model_key"], make=r["make"], model=r["model"], action="skip")
            if body.mode == "accept_suggestions":
                s = r["suggestion"]
                if s and s["score"] >= body.min_score:
                    d.action, d.model_id = "map", s["model_id"]
                else:
                    counts["left"] += 1
                    continue
            else:
                if r["ambiguous"] or r["make_key"] != body.make_key:
                    counts["left"] += 1
                    continue
                d.action = "create"
                d.type_id = body.type_id
            counts[_apply_decision(db, d)] += 1
        finish(db, refresh=counts["created"] > 0)
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        log.exception("compat -> fitment bulk decide failed")
        raise HTTPException(500, f"Nothing was saved: {str(e)[:300]}")
    log.info("compat -> fitment bulk %s by %s: %s", body.mode, user.email, counts)
    return counts
