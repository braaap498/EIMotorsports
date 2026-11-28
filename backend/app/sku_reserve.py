"""
Catalog-ordered SKUs with reserved numbers.

Order of numbers: Automatic (autodist) items, Rocky Mountain (rmatv) items, a block of 1000 for Niche, a block of 1000 for
Sinera, then a block of 1000 for every other hand-made catalog (a distributor with saved parts and no loaded catalog), each new one
after the last. Everything below KIT_BASE (5,000,000) is for catalogs, so there is room for more distributors. From KIT_BASE up
come the kits and every other product that is not a catalog part (eBay-only, duplicates of a catalog part), by product id; the
database counter is moved to continue from there, so a new kit gets the next number.

  * A catalog part that has a single product gives that product the part's number.
  * A catalog part with no product reserves its number (sku_reservations, product_id null). The product gets it when it is
    made (claim_for_item, called right after the insert in autodist.py, rmatv.py and kits.py).
  * A part saved to a hand-made catalog takes the next free number of its block at once (reserve_manual_item); the first part of a
    new hand-made catalog opens a new block of 1000 after the last one. Nothing is reserved until the first run of this tool.
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

HAND_CATALOGS = [("niche", r"niche"), ("sinera", r"sinera")]  # (source, regex on po_distributors.name): the first two blocks, always there
HAND_PREFIX = "hand"           # source of every other hand-made catalog: "hand<distributor id>"
BLOCK_SIZE = 1000
KIT_BASE = 5_000_000           # kits and every other non-catalog product start here; below it are the catalogs, with room to add more
SUPPLIER_ORDER = ["autodist", "rmatv"]
TEMP_BASE = 9_000_000          # temporary numbers: EI-9000001 ...; the plan must stay below this
MAX_PLAN = TEMP_BASE - 1
CHUNK = 400


def fmt(n: int) -> str:
    return f"{SKU_PREFIX}{n:07d}"


_LOADED_CATALOG = re.compile(r"autodist|automatic distributors|rocky\s*mountain\s*atv|rmatv", re.I)  # same names as kits.supplier_for


def is_hand_source(source: str) -> bool:
    return source in {s for s, _ in HAND_CATALOGS} or (source or "").startswith(HAND_PREFIX)


def hand_source_for(name: Optional[str], distributor_id: int) -> Optional[str]:
    """The block a hand-made catalog's parts belong to: 'niche', 'sinera' (by name) or 'hand<id>'. None for a distributor whose
    catalog is loaded here (Automatic, Rocky Mountain): its parts come from the catalog, not from saved parts."""
    if _LOADED_CATALOG.search(name or ""):
        return None
    for src, pat in HAND_CATALOGS:
        if re.search(pat, name or "", re.I):
            return src
    return f"{HAND_PREFIX}{distributor_id}"


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
    hand_items:     {'niche': [(item_id, [matching product ids]), ...], 'sinera': [...], 'hand7': [...]}   in manual_catalog_items.id order;
                    one block of 1000 per entry, in the order of the dict
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
    for src, items in hand_items.items():  # block order = the order of the dict
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
    if n > KIT_BASE:
        raise ValueError(f"The catalogs reach {n - 1}, past the start of the kit range ({KIT_BASE}). Raise KIT_BASE in sku_reserve.py.")
    n = KIT_BASE  # everything that is not a catalog part (kits first of all) starts here, so there is room below for more catalogs
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


# False = a product that matches a part ONLY by manufacturer # is not given the part's number; it is listed for a manual check.
MFR_AUTO = False


def _tiered(items: list, pairs, manual: list, src: str, strong: dict) -> list:
    """pairs = (item, product id, tier): tier 0 = the product points at the part, 1 = SKU / part # equal, 2 = manufacturer # equal.
    The manufacturer # is only looked at when nothing matched before it. A part matched only that way goes to `manual`.
    Returns (item, product ids, tier of the match or None). `strong` collects {product id: {catalogs}} for tier 0/1 matches:
    a product held that way by one catalog must never be given to a different catalog by a weaker match (see _drop_cross_catalog)."""
    by: dict = {}
    for item, pid, tier in pairs:
        by.setdefault(item, {}).setdefault(tier, set()).add(pid)
    out = []
    for i in items:
        t = by.get(i, {})
        first = min(t) if t else None
        if first is not None and first <= 1:
            for pid in t[first]:
                strong.setdefault(pid, set()).add(src)
        if first == 2 and not MFR_AUTO:
            manual.append({"source": src, "item_id": i, "products": sorted(t[2])})
            out.append((i, [], None))
        else:
            out.append((i, sorted(t[first]) if first is not None else [], first))
    return out


def _drop_cross_catalog(rows: list, src: str, strong: dict) -> list:
    """rows = (item, product ids, tier). A weak match (manufacturer #, or a supplier_product_links row; tier 2 or 3) may not take a
    product that another catalog holds by a pointer or an equal SKU / part #. 09-601 (Sinera part #) and 09.601 (Automatic mfr #)
    look the same once punctuation is removed, but the product belongs to Sinera."""
    out = []
    for i, pids, tier in rows:
        if tier is not None and tier >= 2:
            pids = [p for p in pids if not (strong.get(p, set()) - {src})]
        out.append((i, pids))
    return out


def hand_sources(db: Session) -> list:
    """[(source, [po_distributors rows])] in block order: Niche, Sinera, then the other hand-made catalogs. A block that already
    exists keeps its place (read from sku_reservations), a new one goes after the last, by distributor id. A distributor is a
    hand-made catalog when it has saved parts and no catalog loaded here."""
    groups: dict = {src: [] for src, _ in HAND_CATALOGS}
    for d in db.execute(text("select d.id, d.name from po_distributors d where exists (select 1 from manual_catalog_items m where m.distributor_id = d.id) order by d.id")).all():
        src = hand_source_for(d.name, d.id)
        if src:
            groups.setdefault(src, []).append(d)
    # Niche/Sinera are matched by name even when they have no saved parts yet (their block is reserved empty)
    for d in db.execute(text("select id, name from po_distributors order by id")).all():
        src = hand_source_for(d.name, d.id)
        if src in {s for s, _ in HAND_CATALOGS} and d not in groups[src]:
            groups[src].append(d)
    try:
        placed = dict(db.execute(text("select source, min(number) from sku_reservations where source like :p group by source"), {"p": HAND_PREFIX + "%"}).all())
    except Exception:
        db.rollback()
        placed = {}
    for src in placed:
        groups.setdefault(src, [])  # a block that exists stays, even if its distributor has lost all saved parts
    fixed = [s for s, _ in HAND_CATALOGS]
    others = sorted((s for s in groups if s not in fixed), key=lambda s: (placed.get(s, 10**12), groups[s][0].id if groups[s] else 0, s))
    return [(s, groups[s]) for s in fixed + others]


def build_plan(db: Session, scope: str = "all", lo: Optional[int] = None, hi: Optional[int] = None) -> dict:
    """A catalog part's product = the single product (never a kit) that
       - its one part line points at, or
       - has no pointer to a different part and whose manufacturer part # or custom label equals the part's SKU, or
       - only if nothing matched the SKU: equals the part's manufacturer #. Such a part is NOT numbered automatically (MFR_AUTO);
         it is listed in plan["manual"] for a manual check.
    (Barcode / UPC is not used.)"""
    active = " and s.active" if scope == "active" else ""
    sup = {}
    strong: dict = {}  # product id -> catalogs that hold it by a pointer or an equal SKU / part #
    manual: list = []  # parts that matched a product only by manufacturer #: checked by hand
    for src in SUPPLIER_ORDER:
        items = [r.id for r in db.execute(text(f"select s.id from supplier_items s where s.supplier = :src{active} order by s.sku, s.id"), {"src": src}).all()]
        pairs = db.execute(
            text(
                f"with {_SINGLES} "
                f"select s.id as item, k.pid, 1 as tier from supplier_items s join keys k on k.k = {_N.format('s.sku')} "
                f"where s.supplier = :src{active} and k.mi_id is null and (k.si_id is null or k.si_id = s.id) "
                f"union select s.id, k.pid, 2 from supplier_items s join keys k on k.k = s.mfr_number_norm "
                f"where s.supplier = :src{active} and k.mi_id is null and (k.si_id is null or k.si_id = s.id) "
                f"union select s.id, x.id, 0 from supplier_items s join singles x on x.si_id = s.id where s.supplier = :src{active}"
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
        got = _tiered(items, [(r.item, r.pid, r.tier) for r in pairs], manual, src, strong)
        # a supplier_product_links row is the weakest match (tier 3): it is not checked against the part lines at all
        sup[src] = [(i, m, t) if m else (i, [linked[i]] if linked.get(i) else [], 3 if linked.get(i) else None) for i, m, t in got]
    hand = {}
    names = {}
    for src, dist in hand_sources(db):
        names[src] = [d.name for d in dist]
        if not dist:
            hand[src] = []
            continue
        d = [x.id for x in dist]
        items = [r.id for r in db.execute(text("select m.id from manual_catalog_items m where m.distributor_id = any(:d) order by m.id"), {"d": d}).all()]
        pairs = db.execute(
            text(
                f"with {_SINGLES} "
                f"select m.id as item, k.pid, 1 as tier from manual_catalog_items m join keys k on k.k = {_N.format('m.part_number')} "
                "where m.distributor_id = any(:d) and k.si_id is null and (k.mi_id is null or k.mi_id = m.id) "
                f"union select m.id, k.pid, 2 from manual_catalog_items m join keys k on k.k = {_N.format('m.mfr_number')} "
                "where m.distributor_id = any(:d) and k.si_id is null and (k.mi_id is null or k.mi_id = m.id) "
                "union select m.id, x.id, 0 from manual_catalog_items m join singles x on x.mi_id = m.id where m.distributor_id = any(:d)"
            ),
            {"d": d},
        ).all()
        hand[src] = _tiered(items, [(r.item, r.pid, r.tier) for r in pairs], manual, src, strong)
    sup = {k: _drop_cross_catalog(v, k, strong) for k, v in sup.items()}
    hand = {k: _drop_cross_catalog(v, k, strong) for k, v in hand.items()}
    manual = [{**m, "products": [p for p in m["products"] if not (strong.get(p, set()) - {m["source"]})]} for m in manual]
    manual = [m for m in manual if m["products"]]
    products = db.execute(text("select id, sku from products order by id")).all()
    plan = assign_numbers(sup, hand, [p.id for p in products])
    cur = {p.id: p.sku for p in products}
    plan["current"] = cur
    plan["scope"] = scope
    num_of = {(r["source"], r["supplier_item_id"] or r["manual_item_id"]): r["number"] for r in plan["rows"]}  # before any range cut
    plan["manual"] = [{**m, "number": num_of.get((m["source"], m["item_id"]))} for m in manual]
    if lo is not None and hi is not None:
        plan["manual"] = [m for m in plan["manual"] if m["number"] is not None and lo <= m["number"] <= hi]
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
    if plan["manual"]:
        warnings.append(f"{len(plan['manual'])} catalog parts match a product only by manufacturer #, not by SKU. They are not numbered automatically: their numbers stay reserved and each needs a manual check (listed below).")
    if plan["displaced"]:
        warnings.append(f"{plan['displaced']} products hold numbers in this range (or temporary numbers) but belong elsewhere in the plan. They move to fresh numbers from {fmt(plan['fresh_from'])} up and get their proper numbers in a later run; their shelf labels change twice.")
    if labels:
        warnings.append(f"{labels} products have an auto shelf barcode that changes (EIB-); printed labels for them must be reprinted.")
    slots = []
    if plan["range"]:
        rr = [r for r in plan["rows"]][:200]
        sup_ids = [r["supplier_item_id"] for r in rr if r["supplier_item_id"]]
        man_ids = [r["manual_item_id"] for r in rr if r["manual_item_id"]]
        sup, man = {}, {}
        if sup_ids:
            sup = {x.id: x for x in db.execute(text("select id, sku, mfr_number from supplier_items where id = any(cast(:i as bigint[]))"), {"i": sup_ids}).all()}
        if man_ids:
            man = {x.id: x for x in db.execute(text("select id, part_number as sku, mfr_number from manual_catalog_items where id = any(cast(:i as bigint[]))"), {"i": man_ids}).all()}
        for r in rr:
            part = sup.get(r["supplier_item_id"]) if r["supplier_item_id"] else man.get(r["manual_item_id"])
            slots.append({
                "number": fmt(r["number"]), "source": r["source"], "part_sku": part.sku if part else None,
                "part_mfr": part.mfr_number if part else None, "product_id": r["product_id"],
                "product_now": plan["current"].get(r["product_id"]) if r["product_id"] else None,
            })
    return {
        "slots": slots,
        "scope": plan["scope"], "products": plan["in_range"], "will_change": len(ch), "unchanged": plan["in_range"] - (len(ch) - plan["displaced"]),
        "range": plan["range"], "displaced": plan["displaced"],
        "reserved_numbers": reserved, "last_number": fmt(plan.get("upper", plan["last"])), "ranges": {k: ([fmt(v[0]), fmt(v[1])] if v else None) for k, v in plan["ranges"].items()},
        "catalog_counts": plan["counts"], "distributors": plan["distributors"], "labels_to_reprint": labels, "warnings": warnings,
        "manual": [{**m, "number": fmt(m["number"]) if m["number"] else None} for m in plan["manual"][:100]], "manual_total": len(plan["manual"]),
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
        col = "manual_item_id" if is_hand_source(source) else "supplier_item_id"
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


def _free_slot(db: Session, source: str):
    return db.execute(
        text("select number from sku_reservations where source = :s and manual_item_id is null and product_id is null order by number limit 1 for update"),
        {"s": source},
    ).first()


def reserve_manual_item(db: Session, item_id: int) -> Optional[str]:
    """A part was just saved to a hand-made catalog: it takes the next free number of its block right away, so its number follows
    the order the parts were saved and shows on the catalog before a product exists. The first part of a distributor that has no
    block yet opens one: the next 1000 numbers after the last block. Does nothing until the tool has been run once (no reservations
    yet), when the block is full, or when there is no room left below KIT_BASE. Never raises: saving a part must not fail over this."""
    try:
        with db.begin_nested():
            m = db.execute(
                text("select m.id, m.distributor_id, d.name from manual_catalog_items m join po_distributors d on d.id = m.distributor_id where m.id = :i"),
                {"i": item_id},
            ).first()
            src = hand_source_for(m.name, m.distributor_id) if m else None
            if not src:
                return None
            have = db.execute(text("select number from sku_reservations where manual_item_id = :i limit 1"), {"i": item_id}).first()
            if have:
                return fmt(have.number)
            db.execute(text("select pg_advisory_xact_lock(7240001)"))  # two parts saved at once must not open the same block
            r = _free_slot(db, src)
            if not r:
                if db.execute(text("select 1 from sku_reservations where source = :s limit 1"), {"s": src}).first():
                    return None  # its block is full
                top = db.execute(text("select max(number) from sku_reservations where number < :k"), {"k": KIT_BASE}).scalar()
                if not top or top + BLOCK_SIZE >= KIT_BASE:
                    return None  # numbers not assigned yet, or no room left below the kits
                db.execute(
                    text("insert into sku_reservations (number, source, block_index) select cast(:a as integer) + g, :s, g from generate_series(0, cast(:n as integer)) g"),
                    {"a": top + 1, "s": src, "n": BLOCK_SIZE - 1},
                )
                r = _free_slot(db, src)
            if not r:
                return None
            db.execute(text("update sku_reservations set manual_item_id = :i where number = :n"), {"i": item_id, "n": r.number})
            return fmt(r.number)
    except Exception:
        return None


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
        # the default may call a function (next_product_sku()) that itself calls nextval('public.some_seq'): look inside it
        f = re.search(r"([A-Za-z_][A-Za-z0-9_.]*)\(\)", d)
        if f:
            try:
                body = db.execute(text("select pg_get_functiondef(cast(:f as regprocedure))"), {"f": f.group(1) + "()"}).scalar() or ""
                m = re.search(r"nextval\('([^']+)'", body)
            except Exception:
                db.rollback()
                m = None
    if not m:
        return f"products.sku default is not a plain sequence ({d[:80] or 'none'}): the counter was NOT moved. Move it by hand past EI-{upto:07d}."
    seq = m.group(1)
    cur = db.execute(text("select last_value from " + re.sub(r"[^A-Za-z0-9_.\"]", "", seq))).scalar() or 0
    if cur < upto:
        db.execute(text("select setval(cast(:s as regclass), :v, true)"), {"s": seq, "v": upto})
    db.commit()
    return f"counter {seq} at {max(cur, upto)}"


def _counter_floor(plan: dict) -> int:
    """The database counter must always continue from the kit range, even after a partial run: a new kit gets the next number
    after KIT_BASE, never one inside a catalog's range."""
    return max(plan.get("upper", plan["last"]), KIT_BASE - 1)


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
                     "values (:number, :source, :supplier_item_id, :manual_item_id, :block_index, :product_id, case when cast(:product_id as bigint) is null then null else now() end)"),
                rows[i:i + 2000],
            )
        db.commit()
        note = _move_counter(db, _counter_floor(plan))
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
        # A SKU-shaped value (EI-0000123) is not an eBay item number. If one was left in ebay_item_id it is the product's old SKU,
        # and it would keep the product turning up when someone searches for that number. Clear it on the products just renumbered.
        try:
            for i in range(0, len(ids), CHUNK):
                db.execute(
                    text("update products set ebay_item_id = null where id = any(cast(:ids as bigint[])) and ebay_item_id ~* '^EI-[0-9]+$'"),
                    {"ids": ids[i:i + CHUNK]},
                )
            db.commit()
        except Exception as e:
            db.rollback()
            msg = f"done; old SKUs left in ebay_item_id were not cleared ({str(e)[:120]})"
        if update_snapshots:
            try:
                db.execute(text("update ebay_listings e set sku = p.sku from products p where e.product_id = p.id and e.sku is not null and e.sku <> p.sku"))
                db.commit()
            except Exception as e:
                db.rollback()
                msg = f"done; ebay_listings.sku was not updated ({str(e)[:120]})"
        note2 = _move_counter(db, _counter_floor(plan))
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
