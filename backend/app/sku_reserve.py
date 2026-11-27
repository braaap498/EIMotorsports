"""
Catalog-ordered SKUs with reserved numbers.

Order of numbers: Automatic (autodist) items, Rocky Mountain (rmatv) items, a block of 1000 for Niche, a block of 1000 for
Sinera, then every other product (kits, eBay-only, duplicates of a catalog part) by product id.

  * A catalog part that has a single product gives that product the part's number.
  * A catalog part with no product reserves its number (sku_reservations, product_id null). The product gets it when it is
    made (claim_for_item, called right after the insert in autodist.py, rmatv.py and kits.py).
  * "The product of a catalog part" = kits.single_conds(); never a kit that merely contains the part.

build_plan() and public_plan() are read only. start_apply() runs in the background: reservations written, counter moved
past the plan, every changing product moved to a temporary number (EI-9xxxxxx), then to its final number, in chunks.
It can be run again after an interruption.
"""
import re
import threading
import time
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.skus import SKU_PREFIX, SHELF_PREFIX

HAND_CATALOGS = [("niche", r"niche"), ("sinera", r"sinera")]  # (source, regex on po_distributors.name)
BLOCK_SIZE = 1000
SUPPLIER_ORDER = ["autodist", "rmatv"]
TEMP_BASE = 9_000_000          # temporary numbers: EI-9000001 ...; the plan must stay below this
MAX_PLAN = TEMP_BASE - 1
CHUNK = 400


def fmt(n: int) -> str:
    return f"{SKU_PREFIX}{n:07d}"


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------
def ensure_tables(db: Session) -> None:
    try:
        db.execute(text(
            "create table if not exists sku_reservations ("
            "number integer primary key, source text not null, supplier_item_id bigint, manual_item_id bigint, "
            "block_index integer, product_id bigint, claimed_at timestamptz, created_at timestamptz not null default now())"
        ))
        db.execute(text("create index if not exists sku_reservations_item_idx on sku_reservations (source, supplier_item_id)"))
        db.execute(text("create index if not exists sku_reservations_manual_idx on sku_reservations (source, manual_item_id)"))
        db.execute(text("create unique index if not exists sku_reservations_product_uq on sku_reservations (product_id) where product_id is not null"))
        db.execute(text(
            "create table if not exists sku_renumber_runs ("
            "id bigserial primary key, started_at timestamptz not null default now(), heartbeat timestamptz not null default now(), "
            "finished_at timestamptz, status text not null default 'running', scope text, phase text, total integer default 0, "
            "done integer default 0, message text)"
        ))
        db.commit()
    except Exception:
        db.rollback()


def ensure_tables_startup() -> None:
    """For main.py's start-up: opens its own session, like the other ensure_* helpers."""
    from app.database import get_db

    gen = get_db()
    db = next(gen)
    try:
        ensure_tables(db)
    finally:
        db.close()


# --------------------------------------------------------------------------
# the number plan: pure function (testable without a database)
# --------------------------------------------------------------------------
def assign_numbers(supplier_items: dict, hand_items: dict, product_ids: list) -> dict:
    """
    supplier_items: {'autodist': [(item_id, [matching product ids]), ...], 'rmatv': [...]}   in catalog order
    hand_items:     {'niche': [(item_id, [matching product ids]), ...], 'sinera': [...]}      in manual_catalog_items.id order
    product_ids:    every product id.
    Returns {'rows': [reservation dicts], 'product_number': {pid: n}, 'duplicates': [...], 'shared': int, 'ranges': {...}}
    """
    n = 1
    rows, assigned, dups, shared, ranges = [], {}, [], 0, {}

    def take(item_matches, number):
        nonlocal shared
        owner = next((p for p in item_matches if p not in assigned), None)
        if owner is not None:
            assigned[owner] = number
        elif item_matches:
            shared += 1  # its product already took an earlier catalog's number: this number stays reserved
        return owner

    for src in SUPPLIER_ORDER:
        start = n
        for item_id, matches in supplier_items.get(src, []):
            owner = take(matches, n)
            if len(matches) > 1:
                dups.append({"source": src, "item_id": item_id, "keeps": owner, "number": n, "others": [m for m in matches if m != owner]})
            rows.append({"number": n, "source": src, "supplier_item_id": item_id, "manual_item_id": None, "block_index": None, "product_id": owner})
            n += 1
        ranges[src] = [start, n - 1] if n > start else None
    for src, _pat in HAND_CATALOGS:
        items = hand_items.get(src, [])
        if len(items) > BLOCK_SIZE:
            raise ValueError(f"{src} has {len(items)} saved parts, more than its block of {BLOCK_SIZE}.")
        start = n
        for i in range(BLOCK_SIZE):
            if i < len(items):
                item_id, matches = items[i]
                owner = take(matches, n)
                if len(matches) > 1:
                    dups.append({"source": src, "item_id": item_id, "keeps": owner, "number": n, "others": [m for m in matches if m != owner]})
                rows.append({"number": n, "source": src, "supplier_item_id": None, "manual_item_id": item_id, "block_index": i, "product_id": owner})
            else:
                rows.append({"number": n, "source": src, "supplier_item_id": None, "manual_item_id": None, "block_index": i, "product_id": None})
            n += 1
        ranges[src] = [start, n - 1]
    start = n
    for pid in sorted(product_ids):
        if pid not in assigned:
            assigned[pid] = n
            n += 1
    ranges["other"] = [start, n - 1] if n > start else None
    if n - 1 > MAX_PLAN:
        raise ValueError(f"The plan needs numbers up to {n - 1}; the limit is {MAX_PLAN} (EI-9xxxxxx is kept for the move). Use scope 'active'.")
    return {"rows": rows, "product_number": assigned, "duplicates": dups, "shared": shared, "ranges": ranges, "last": n - 1}


_SKU_RE = re.compile(r"^EI-(\d{7})$", re.I)


def _restrict_to_range(db: Session, plan: dict, lo: int, hi: int) -> None:
    """Partial run: only products whose planned number is in [lo, hi] change. A product that sits on a number in the range
    (or on a temporary number left by an interrupted run) but is planned outside it must make room: it takes a fresh number
    above everything in use, and the next full run gives it its proper one."""
    if lo < 1 or hi < lo:
        raise ValueError("The number range must start at 1 or more and end at or after its start.")
    if hi >= TEMP_BASE:
        raise ValueError(f"The range must stay below {TEMP_BASE}.")
    cur, pn = plan["current"], plan["product_number"]
    changes = {pid: fmt(n) for pid, n in pn.items() if lo <= n <= hi and (cur.get(pid) or "").upper() != fmt(n)}
    nums = {}
    for pid, sku in cur.items():
        m = _SKU_RE.match(sku or "")
        if m:
            nums[pid] = int(m.group(1))
    displaced = sorted(pid for pid, k in nums.items() if (lo <= k <= hi or k >= TEMP_BASE) and not (lo <= pn.get(pid, 0) <= hi))
    top = max([k for k in nums.values() if k < TEMP_BASE] + [hi])
    bc = db.execute(
        text("select max(case when code ~ '^EIB-[0-9]{1,7}$' and (length(substr(code, 5)) < 7 or substr(code, 5) < '9000000') "
             "then cast(substr(code, 5) as integer) end) from product_barcodes")
    ).scalar() or 0
    top = max(top, bc)
    first = top + 1
    for pid in displaced:
        top += 1
        changes[pid] = fmt(top)
    if top >= TEMP_BASE:
        raise ValueError("Not enough free numbers below the temporary range for the products that have to move.")
    plan.update(
        changes=changes, range=[lo, hi], displaced=len(displaced), fresh_from=first if displaced else None,
        upper=max(hi, top if displaced else 0),
        rows=[r for r in plan["rows"] if lo <= r["number"] <= hi],
        duplicates=[d for d in plan["duplicates"] if lo <= d["number"] <= hi],
        in_range=sum(1 for n in pn.values() if lo <= n <= hi),
    )


# --------------------------------------------------------------------------
# the plan (read only)
# --------------------------------------------------------------------------
_N = "upper(regexp_replace(coalesce({}, ''), '[^A-Za-z0-9]', '', 'g'))"
# Single products (no part line, or exactly one at quantity 1) with their manufacturer part # and custom label normalised,
# and the part they already point at. "keys" has one row per usable text value (length >= 3) of a product.
_SINGLES = (
    "singles as (select p.id, "
    "nullif(" + _N.format("p.manufacturer_part_number") + ", '') as mpn_n, "
    "nullif(" + _N.format("p.custom_label_sku") + ", '') as lbl_n, "
    "(select z.supplier_item_id from product_kit_parts z where z.product_id = p.id limit 1) as si_id, "
    "(select z.manual_item_id from product_kit_parts z where z.product_id = p.id limit 1) as mi_id "
    "from products p where (select count(*) from product_kit_parts z where z.product_id = p.id) <= 1 "
    "and not exists (select 1 from product_kit_parts z where z.product_id = p.id and coalesce(z.quantity, 1) <> 1)), "
    "keys as (select x.id as pid, x.si_id, x.mi_id, v.k from singles x cross join lateral (values (x.mpn_n), (x.lbl_n)) as v(k) "
    "where v.k is not null and length(v.k) >= 3)"
)


def _group(items: list, pairs) -> list:
    got: dict = {}
    for item, pid in pairs:
        got.setdefault(item, set()).add(pid)
    return [(i, sorted(got.get(i, ()))) for i in items]


def build_plan(db: Session, scope: str = "all", lo: Optional[int] = None, hi: Optional[int] = None) -> dict:
    """A catalog part's product = the single product (never a kit) that
       - its one part line points at, or
       - has no pointer to a different part and whose manufacturer part # or custom label equals the part's SKU or manufacturer #.
    (Barcode / UPC is not used.)"""
    active = " and s.active" if scope == "active" else ""
    sup = {}
    for src in SUPPLIER_ORDER:
        items = [r.id for r in db.execute(text(f"select s.id from supplier_items s where s.supplier = :src{active} order by s.sku, s.id"), {"src": src}).all()]
        pairs = db.execute(
            text(
                f"with {_SINGLES} "
                f"select s.id as item, k.pid from supplier_items s join keys k on k.k = {_N.format('s.sku')} "
                f"where s.supplier = :src{active} and k.mi_id is null and (k.si_id is null or k.si_id = s.id) "
                f"union select s.id, k.pid from supplier_items s join keys k on k.k = s.mfr_number_norm "
                f"where s.supplier = :src{active} and k.mi_id is null and (k.si_id is null or k.si_id = s.id) "
                f"union select s.id, x.id from supplier_items s join singles x on x.si_id = s.id where s.supplier = :src{active}"
            ),
            {"src": src},
        ).all()
        linked = dict(
            db.execute(
                text(
                    "select s.id, min(l.product_id) from supplier_items s join supplier_product_links l on l.supplier = s.supplier and l.supplier_sku = s.sku "
                    f"where s.supplier = :src{active} group by s.id"
                ),
                {"src": src},
            ).all()
        )
        sup[src] = [(i, m or ([linked[i]] if linked.get(i) else [])) for i, m in _group(items, [(r.item, r.pid) for r in pairs])]
    hand = {}
    names = {}
    dists = db.execute(text("select id, name from po_distributors order by id")).all()
    for src, pat in HAND_CATALOGS:
        dist = [r for r in dists if re.search(pat, r.name or "", re.I)]
        names[src] = [d.name for d in dist]
        if not dist:
            hand[src] = []
            continue
        d = [x.id for x in dist]
        items = [r.id for r in db.execute(text("select m.id from manual_catalog_items m where m.distributor_id = any(:d) order by m.id"), {"d": d}).all()]
        pairs = db.execute(
            text(
                f"with {_SINGLES} "
                f"select m.id as item, k.pid from manual_catalog_items m join keys k on (k.k = {_N.format('m.part_number')} or k.k = {_N.format('m.mfr_number')}) "
                "where m.distributor_id = any(:d) and k.si_id is null and (k.mi_id is null or k.mi_id = m.id) "
                "union select m.id, x.id from manual_catalog_items m join singles x on x.mi_id = m.id where m.distributor_id = any(:d)"
            ),
            {"d": d},
        ).all()
        hand[src] = _group(items, [(r.item, r.pid) for r in pairs])
    products = db.execute(text("select id, sku from products order by id")).all()
    plan = assign_numbers(sup, hand, [p.id for p in products])
    cur = {p.id: p.sku for p in products}
    plan["current"] = cur
    plan["scope"] = scope
    if lo is not None and hi is not None:
        _restrict_to_range(db, plan, lo, hi)
    else:
        plan["changes"] = {pid: fmt(n) for pid, n in plan["product_number"].items() if (cur.get(pid) or "").upper() != fmt(n)}
        plan.update(range=None, displaced=0, fresh_from=None, in_range=len(plan["product_number"]))
    plan["distributors"] = names
    plan["counts"] = {s: len(v) for s, v in {**sup, **hand}.items()}
    return plan


def public_plan(plan: dict, db: Session) -> dict:
    ch = plan["changes"]
    labels = 0
    if ch:
        labels = db.execute(
            text("select count(distinct product_id) from product_barcodes where kind = 'shelf' and is_auto and code like 'EIB-%' and product_id = any(:i)"),
            {"i": list(ch)},
        ).scalar() or 0
    reserved = sum(1 for r in plan["rows"] if r["product_id"] is None)
    warnings = []
    clash = db.execute(
        text("select count(*) from product_barcodes where code ~ '^EIB-[0-9]{1,7}$' and not (kind = 'shelf' and is_auto) "
             "and cast(substr(code, 5) as integer) between :lo and :n"),
        {"n": plan.get("upper", plan["last"]), "lo": (plan["range"] or [1])[0]},
    ).scalar() or 0
    if clash:
        warnings.append(f"{clash} barcodes that are not auto shelf codes already look like EIB- numbers inside the plan range; if one equals a new number the run will stop. Fix or remove them first.")
    for src, _ in HAND_CATALOGS:
        rg = plan["ranges"].get(src)
        if plan["range"] and not (rg and rg[0] <= plan["range"][1] and rg[1] >= plan["range"][0]):
            continue
        if not plan["distributors"].get(src):
            warnings.append(f"No distributor named like '{src}' was found; its block of {BLOCK_SIZE} is reserved with no parts.")
    if plan["shared"]:
        warnings.append(f"{plan['shared']} catalog parts match a product that already took an earlier catalog's number; their numbers stay reserved.")
    if plan["duplicates"]:
        warnings.append(f"{len(plan['duplicates'])} catalog parts have more than one single product; the lowest id keeps the number. Merge the others.")
    if plan["displaced"]:
        warnings.append(f"{plan['displaced']} products hold numbers in this range (or temporary numbers) but belong elsewhere in the plan. They move to fresh numbers from {fmt(plan['fresh_from'])} up and get their proper numbers in a later run; their shelf labels change twice.")
    if labels:
        warnings.append(f"{labels} products have an auto shelf barcode that changes (EIB-); printed labels for them must be reprinted.")
    return {
        "scope": plan["scope"], "products": plan["in_range"], "will_change": len(ch), "unchanged": plan["in_range"] - (len(ch) - plan["displaced"]),
        "range": plan["range"], "displaced": plan["displaced"],
        "reserved_numbers": reserved, "last_number": fmt(plan.get("upper", plan["last"])), "ranges": {k: ([fmt(v[0]), fmt(v[1])] if v else None) for k, v in plan["ranges"].items()},
        "catalog_counts": plan["counts"], "distributors": plan["distributors"], "labels_to_reprint": labels, "warnings": warnings,
        "duplicates": plan["duplicates"][:100], "duplicate_total": len(plan["duplicates"]),
        "sample": [{"product_id": p, "from": plan["current"].get(p), "to": s} for p, s in list(ch.items())[:25]],
    }


# --------------------------------------------------------------------------
# claiming a reserved number when a catalog product is made
# --------------------------------------------------------------------------
def _set_sku(db: Session, pid: int, sku: str) -> bool:
    """Same text moves as admin_catalog._change_sku, but never raises: on a clash the product keeps the database's number."""
    code = SHELF_PREFIX + sku[len(SKU_PREFIX):]
    try:
        with db.begin_nested():
            db.execute(text("update products set sku = :s, updated_at = now() where id = :p"), {"s": sku, "p": pid})
            db.execute(
                text("update product_barcodes set code = :c where id = (select id from product_barcodes where product_id = :p and kind = 'shelf' and is_auto and code like 'EIB-%' order by id limit 1)"),
                {"c": code, "p": pid},
            )
            db.execute(text("update po_receiving_issues set sku = :s where product_id = :p"), {"s": sku, "p": pid})
        return True
    except IntegrityError:
        return False


def claim_for_item(db: Session, product_id: int, source: str, item_id: int) -> Optional[str]:
    """Called right after a catalog product is inserted. Gives it the number reserved for that catalog part (None = no
    reservation, the database's number stays). For Niche/Sinera a part saved after the plan takes the next free number of the block."""
    try:
        col = "manual_item_id" if source in {s for s, _ in HAND_CATALOGS} else "supplier_item_id"
        r = db.execute(
            text(f"select number from sku_reservations where source = :s and {col} = :i and product_id is null order by number limit 1 for update"),
            {"s": source, "i": item_id},
        ).first()
        if not r and col == "manual_item_id":
            r = db.execute(
                text("select number from sku_reservations where source = :s and manual_item_id is null and product_id is null order by number limit 1 for update"),
                {"s": source},
            ).first()
        if not r:
            return None
        sku = fmt(r.number)
        if not _set_sku(db, product_id, sku):
            return None
        db.execute(
            text(f"update sku_reservations set product_id = :p, claimed_at = now(), {col} = :i where number = :n"),
            {"p": product_id, "i": item_id, "n": r.number},
        )
        return sku
    except Exception:
        return None  # a reservation problem must never stop a product from being made


def reserved_numbers(db: Session, source: str, item_ids: list) -> dict:
    """{catalog part id: 'EI-0000123'} for the parts in item_ids whose number is reserved and has no product yet.
    source = 'autodist' / 'rmatv' (supplier_items ids) or 'manual' (manual_catalog_items ids, any hand catalog).
    Read only; any problem (for example the table not made yet) gives {} so a catalog page never breaks over it."""
    ids = [int(i) for i in item_ids if i]
    if not ids:
        return {}
    try:
        if source == "manual":
            r = db.execute(
                text("select manual_item_id as item, min(number) as number from sku_reservations "
                     "where manual_item_id = any(cast(:i as bigint[])) and product_id is null group by 1"),
                {"i": ids},
            ).all()
        else:
            r = db.execute(
                text("select supplier_item_id as item, min(number) as number from sku_reservations "
                     "where source = :s and supplier_item_id = any(cast(:i as bigint[])) and product_id is null group by 1"),
                {"s": source, "i": ids},
            ).all()
        return {x.item: fmt(x.number) for x in r}
    except Exception:
        db.rollback()
        return {}


# --------------------------------------------------------------------------
# applying (background)
# --------------------------------------------------------------------------
_state = {"running": False, "run_id": None, "phase": None, "done": 0, "total": 0, "message": "idle", "error": None}
_mem = threading.Lock()


def get_state() -> dict:
    s = dict(_state)
    try:
        from app.database import get_db

        gen = get_db()
        db = next(gen)
        try:
            r = db.execute(text("select id, status, phase, total, done, message, started_at, finished_at from sku_renumber_runs order by id desc limit 1")).first()
            if r:
                s["last_run"] = {k: (str(v) if v is not None and k.endswith("_at") else v) for k, v in r._mapping.items()}
        finally:
            db.close()
    except Exception:
        pass
    return s


def start_apply(scope: str, update_snapshots: bool, lo: Optional[int] = None, hi: Optional[int] = None) -> dict:
    from fastapi import HTTPException
    from app.database import get_db

    with _mem:
        if _state["running"]:
            raise HTTPException(409, "A renumbering is already running here.")
        gen = get_db()
        db = next(gen)
        try:
            ensure_tables(db)
            busy = db.execute(text("select id from sku_renumber_runs where status = 'running' and heartbeat > now() - interval '3 minutes' limit 1")).first()
            if busy:
                raise HTTPException(409, f"Run {busy.id} is still active (its heartbeat is recent). Wait for it to finish or go stale.")
            db.execute(text("update sku_renumber_runs set status = 'stale' where status = 'running'"))
            run_id = db.execute(text("insert into sku_renumber_runs (scope, phase, message) values (:s, 'planning', 'starting') returning id"), {"s": scope if hi is None else f"{scope} {lo}-{hi}"}).scalar()
            db.commit()
        finally:
            db.close()
        _state.update(running=True, run_id=run_id, phase="planning", done=0, total=0, message="starting", error=None)
    threading.Thread(target=_run, args=(run_id, scope, update_snapshots, lo, hi), daemon=True).start()
    return {"started": True, "run_id": run_id}


def _beat(db: Session, run_id: int, phase=None, done=None, total=None, message=None, status=None):
    if phase is not None:
        _state["phase"] = phase
    if done is not None:
        _state["done"] = done
    if total is not None:
        _state["total"] = total
    if message is not None:
        _state["message"] = message
    db.execute(
        text("update sku_renumber_runs set heartbeat = now(), phase = coalesce(:ph, phase), done = coalesce(:d, done), total = coalesce(:t, total), "
             "message = coalesce(:m, message), status = coalesce(:st, status), finished_at = case when :st is not null and :st <> 'running' then now() else finished_at end where id = :id"),
        {"ph": phase, "d": done, "t": total, "m": message, "st": status, "id": run_id},
    )
    db.commit()


_SHELF_FIRST = ("select distinct on (product_id) id, product_id from product_barcodes where product_id = any(:ids) and kind = 'shelf' and is_auto "
                "and code ~ '^EIB-[0-9]+$' order by product_id, id")


def _move_counter(db: Session, upto: int) -> str:
    """Move the database's number counter past the plan so new products can't land inside it."""
    d = db.execute(text("select column_default from information_schema.columns where table_name = 'products' and column_name = 'sku' limit 1")).scalar() or ""
    m = re.search(r"nextval\('([^']+)'", d)
    if not m:
        return f"products.sku default is not a plain sequence ({d[:80] or 'none'}): the counter was NOT moved. Move it by hand past EI-{upto:07d}."
    seq = m.group(1)
    cur = db.execute(text("select last_value from " + re.sub(r"[^A-Za-z0-9_.\"]", "", seq))).scalar() or 0
    if cur < upto:
        db.execute(text("select setval(cast(:s as regclass), :v, true)"), {"s": seq, "v": upto})
    db.commit()
    return f"counter {seq} at {max(cur, upto)}"


def _run(run_id: int, scope: str, update_snapshots: bool, lo: Optional[int] = None, hi: Optional[int] = None) -> None:
    from app.database import get_db

    gen = get_db()
    db = next(gen)
    try:
        _beat(db, run_id, "planning", message="building the plan")
        plan = build_plan(db, scope, lo, hi)
        db.rollback()
        rows, changes = plan["rows"], plan["changes"]
        # 1. reservations: replaced wholesale; matched products are already claimed
        if plan["range"]:
            db.execute(text("delete from sku_reservations where number between :lo and :hi"), {"lo": plan["range"][0], "hi": plan["range"][1]})
        else:
            db.execute(text("delete from sku_reservations"))
        for i in range(0, len(rows), 2000):
            db.execute(
                text("insert into sku_reservations (number, source, supplier_item_id, manual_item_id, block_index, product_id, claimed_at) "
                     "values (:number, :source, :supplier_item_id, :manual_item_id, :block_index, :product_id, case when :product_id is null then null else now() end)"),
                rows[i:i + 2000],
            )
        db.commit()
        note = _move_counter(db, plan.get("upper", plan["last"]))
        ids = sorted(changes)
        total = len(ids) * 2
        _beat(db, run_id, "moving", 0, total, note)
        # 2. temporary numbers (EI-9xxxxxx by row number over all products, so a resumed run picks the same ones)
        done = 0
        for i in range(0, len(ids), CHUNK):
            part = ids[i:i + CHUNK]
            db.execute(
                text("with r as (select id, row_number() over (order by id) rn from products) "
                     "update products p set sku = 'EI-' || lpad((:b + r.rn)::text, 7, '0'), updated_at = now() from r where r.id = p.id and p.id = any(:ids)"),
                {"b": TEMP_BASE, "ids": part},
            )
            db.execute(
                text("with r as (select id, row_number() over (order by id) rn from products), f as (" + _SHELF_FIRST + ") "
                     "update product_barcodes b set code = 'EIB-' || lpad((:b + r.rn)::text, 7, '0') from f join r on r.id = f.product_id where b.id = f.id"),
                {"b": TEMP_BASE, "ids": part},
            )
            db.commit()
            done += len(part)
            _beat(db, run_id, "moving", done, total, "temporary numbers")
        # 3. final numbers
        for i in range(0, len(ids), CHUNK):
            part = ids[i:i + CHUNK]
            skus = [changes[p] for p in part]
            args = {"ids": part, "skus": skus}
            db.execute(text("update products p set sku = v.s, updated_at = now() from unnest(cast(:ids as bigint[]), cast(:skus as text[])) as v(id, s) where p.id = v.id"), args)
            db.execute(
                text("with f as (" + _SHELF_FIRST.replace("code ~ '^EIB-[0-9]+$'", "code ~ '^EIB-[0-9]+$'") + ") "
                     "update product_barcodes b set code = 'EIB-' || substr(v.s, 4) from f join unnest(cast(:ids as bigint[]), cast(:skus as text[])) as v(id, s) on v.id = f.product_id where b.id = f.id"),
                args,
            )
            db.execute(text("update po_receiving_issues q set sku = v.s from unnest(cast(:ids as bigint[]), cast(:skus as text[])) as v(id, s) where q.product_id = v.id"), args)
            db.commit()
            done += len(part)
            _beat(db, run_id, "moving", done, total, "final numbers")
        msg = "done"
        if update_snapshots:
            try:
                db.execute(text("update ebay_listings e set sku = p.sku from products p where e.product_id = p.id and e.sku is not null and e.sku <> p.sku"))
                db.commit()
            except Exception as e:
                db.rollback()
                msg = f"done; ebay_listings.sku was not updated ({str(e)[:120]})"
        note2 = _move_counter(db, plan.get("upper", plan["last"]))
        _beat(db, run_id, "done", message=f"{msg}; {note2}", status="done")
        _state["running"] = False
    except Exception as e:
        db.rollback()
        _state.update(running=False, error=str(e)[:300])
        try:
            _beat(db, run_id, "failed", message=str(e)[:300], status="failed")
        except Exception:
            db.rollback()
    finally:
        _state["running"] = False
        db.close()
