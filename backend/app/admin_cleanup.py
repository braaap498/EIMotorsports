"""
Parts clean-up: merge duplicate products into ONE website product.

Mounted at /admin/cleanup/* (owner/admin only). The flow the page follows:

  1. GET  /admin/cleanup/search?q=tusk 1.8 bar      find candidate duplicates
  2. POST /admin/cleanup/preview                    read-only report + proposed fitment.
                                                    NOTHING is written. Cancel = just stop here.
  3. POST /admin/cleanup/merge                      the commit. One database transaction:
                                                    all of it happens or none of it does.
  4. GET  /admin/cleanup/log                        who merged what, and when

What a merge does (and does not do):
  * eBay links (channel_listings) are MOVED to the surviving product - never deleted.
  * Sales history, inventory ledger rows, purchase-order lines (and their receiving history), eBay and website
    order lines and live stock holds are MOVED too.
  * Fitment of the survivor is set to exactly the list the person verified.
  * Stock is NOT summed: every duplicate listing was counting the same shelf, so the person
    must type the real quantity. A ledger 'Adjustment' row records the change.
  * Every eBay listing involved (the kept product's and the duplicates') is first copied into
    ebay_listings (title, price, condition, description, item specifics, compatible vehicles,
    photo rows, custom label) keyed by eBay item id and store, so the listing can still be edited
    later. Those rows follow the part to the survivor.
  * If the kept product has no description, the fullest description among the duplicates is copied onto it (like photos).
  * (Later) /blank-descriptions and /fill-descriptions refill kept parts whose description is blank from their live eBay listings (read only).
  * The other products are deleted, after a full snapshot is saved in part_merge_log.
  * If anything unexpected is found (an unknown table pointing at products, changed data
    since the preview, a count that doesn't add up) the merge refuses or rolls back.
"""
import hashlib
import json
import math
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.admin_catalog import finish
from app.auth import AuthUser, require_admin
from app.database import get_db
from app.ebay_oauth import MAIN_STORE
from app.skus import PLACEHOLDER_SKU, clean_ebay_item_id

router = APIRouter(prefix="/cleanup", tags=["admin-cleanup"], dependencies=[Depends(require_admin)])

MAX_MERGE = 1500  # products merged in one go (the biggest duplicate family is ~900)

# Tables that point at products and whose rows must FOLLOW the part to the survivor.
MOVE_TABLES = {
    "channel_listings",
    "ebay_listings",          # each eBay listing's own saved data; follows the part to the survivor
    "inventory_transactions",
    "sales_transactions",
    "purchase_order_lines",
    "po_lines",               # the current purchase orders' lines (open, received or cancelled POs)
    "po_line_receipts",       # receiving history of those lines
    "po_receiving_issues",    # short / over / not-on-PO records
    "ebay_order_lines",       # eBay order lines (history, and ones still waiting to be picked)
    "order_items",            # website order lines
    "stock_reservations",     # live holds follow the part, so an order waiting to be picked keeps its hold
    "product_barcodes",       # scanning an old label still finds the kept part
    "inventory_batch_lines",  # receiving / shipping history follows the part
}
# Tables whose rows are only descriptions of the deleted duplicates; they go with the product.
DROP_TABLES = {
    "product_fitment",
    "product_photos",
    "product_item_specifics",
    "fitment_parse_review",
    "category_assignment_review",
}

# Where the part number lives (it is not in products.manufacturer_part_number).
_PN_NAMES = (
    "'Manufacturer Part Number','Wiseco Piston Part #','Wiseco Piston Part Number',"
    "'Wiseco Piston Kit Part #','Wiseco Part Number','HiFloFiltro Part Number'"
)
_PN_SUBQUERY = f"""
  (select (array_agg(s.value order by case s.name
            when 'Manufacturer Part Number' then 1 when 'Wiseco Piston Part #' then 2
            when 'Wiseco Piston Part Number' then 3 when 'Wiseco Piston Kit Part #' then 4
            when 'Wiseco Part Number' then 5 else 6 end))[1]
   from product_item_specifics s
   where s.product_id = p.id and s.name in ({_PN_NAMES}))
"""

_STATE_SQL = f"""
select p.id, p.ebay_item_id, p.custom_label_sku, p.title, p.list_price, p.quantity_on_hand, p.is_published, p.is_active,
       p.is_universal, p.category_id, p.part_manufacturer_id, p.updated_at,
       (select count(*) from channel_listings cl where cl.product_id = p.id)       as listing_count,
       (select count(*) from product_fitment pf where pf.product_id = p.id)        as fitment_count,
       (select count(*) from product_photos pp where pp.product_id = p.id)         as photo_count,
       (select count(*) from sales_transactions s where s.product_id = p.id)       as sales_count,
       (select count(*) from inventory_transactions it where it.product_id = p.id) as inventory_tx_count,
       ((select count(*) from purchase_order_lines pol where pol.product_id = p.id)
        + (select count(*) from po_lines pl where pl.product_id = p.id))       as po_line_count,
       (select array_agg(cl.external_id order by cl.id) from channel_listings cl
         where cl.product_id = p.id)                                                as ebay_item_ids,
       (select coalesce(sum(l.merged_count), 0) from part_merge_log l
         where l.survivor_product_id = p.id)                                        as prior_merges,
       {_PN_SUBQUERY}                                                               as part_number
from products p
where p.id = any(cast(:ids as bigint[]))
order by p.id
"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _load(db: Session, ids: list[int]):
    return db.execute(text(_STATE_SQL), {"ids": ids}).all()


def _fingerprint(rows) -> str:
    """Changes if ANY involved product is edited, re-listed, re-stocked or re-fitted
    between the preview and the commit."""
    payload = [
        [
            r.id,
            r.updated_at.isoformat() if r.updated_at else None,
            r.quantity_on_hand,
            r.listing_count,
            r.fitment_count,
            r.sales_count,
            r.inventory_tx_count,
            r.po_line_count,
            r.photo_count,
        ]
        for r in rows
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def _norm_label(v) -> str:
    return (v or "").strip().upper()


def _label_mismatches(rows, survivor_id: int) -> list:
    """Ticked products whose custom label SKU is set and is NOT exactly the kept part's label.
    (A product with no label at all is not counted: it has nothing to disagree with.)"""
    keep = next((r for r in rows if r.id == survivor_id), None)
    keep_label = _norm_label(keep.custom_label_sku) if keep else ""
    return [r for r in rows if r.id != survivor_id and _norm_label(r.custom_label_sku) not in ("", keep_label)]


def _label_block_message(rows, survivor_id: int) -> Optional[str]:
    """HARD RULE: every ticked product must have the kept part's exact custom label SKU.
    Returns the reason the merge is refused, or None when it is fine."""
    bad = _label_mismatches(rows, survivor_id)
    if not bad:
        return None
    keep = next((r for r in rows if r.id == survivor_id), None)
    keep_label = (keep.custom_label_sku or "").strip() if keep else ""
    counts: dict[str, int] = {}
    for r in bad:
        k = (r.custom_label_sku or "").strip()
        counts[k] = counts.get(k, 0) + 1
    shown = ", ".join(f"{k} (x{n})" for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5])
    more = f" and {len(counts) - 5} more" if len(counts) > 5 else ""
    return (
        f"Only products with the SAME custom label SKU can be merged. The kept part's label is "
        f"{keep_label or 'empty'}, but {len(bad)} ticked product{'s' if len(bad) != 1 else ''} "
        f"{'has' if len(bad) == 1 else 'have'} a different one: {shown}{more}. "
        "Go back and untick them (the Exact match box helps), or make the labels match first."
    )


def _foreign_keys(db: Session) -> list[dict]:
    """Every table with a foreign key to products, found live from the database."""
    rows = db.execute(
        text(
            """
            select c.conrelid::regclass::text as tbl, a.attname as col, c.confdeltype as del_rule,
                   array_length(c.conkey, 1) as ncols
            from pg_constraint c
            join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]
            where c.contype = 'f' and c.confrelid = 'public.products'::regclass
            """
        )
    ).all()
    out = []
    for r in rows:
        tbl = r.tbl.replace('"', "").removeprefix("public.")
        out.append({"table": tbl, "col": r.col, "rule": r.del_rule, "ncols": r.ncols})
    return out


def _blockers(db: Session, merged_ids: list[int]) -> tuple[list[dict], list[str]]:
    """Returns (foreign keys to handle, reasons the merge must be refused)."""
    fks = _foreign_keys(db)
    problems: list[str] = []
    for fk in fks:
        name, col = fk["table"], fk["col"]
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", name) or not re.fullmatch(r"[a-z_][a-z0-9_]*", col):
            problems.append(f"Unrecognised table name '{name}' references products")
            continue
        if fk["ncols"] != 1:
            problems.append(f"Table '{name}' has a multi-column link to products")
            continue
        if name in MOVE_TABLES or name in DROP_TABLES:
            continue
        n = db.execute(
            text(f"select count(*) from {name} where {col} = any(cast(:ids as bigint[]))"),
            {"ids": merged_ids},
        ).scalar()
        if n:
            problems.append(
                f"Table '{name}' has {n} rows tied to these products and this tool doesn't know "
                "how to move them. Nothing was changed."
            )
    return fks, problems


def _models_with_years(rows) -> list[dict]:
    models: dict[int, dict] = {}
    for r in rows:
        m = models.setdefault(
            r.model_id,
            {
                "model_id": r.model_id,
                "make": r.make,
                "model": r.model,
                "vehicle_type": r.vehicle_type,
                "years": [],
            },
        )
        m["years"].append(
            {
                "vehicle_id": r.vehicle_id,
                "year": r.year,
                "submodel": r.submodel,
                "from_product_ids": list(r.from_products),
            }
        )
    return list(models.values())


def _category_path_fn(db: Session):
    """Returns a function: category id -> 'Parent › Child' text, for the warning details."""
    rows = db.execute(text("select id, parent_id, name from categories")).all()
    by = {r.id: r for r in rows}

    def path(cid) -> str:
        if cid is None or cid not in by:
            return "No category set"
        parts, seen, cur = [], set(), cid
        while cur is not None and cur in by and cur not in seen:
            seen.add(cur)
            parts.append(by[cur].name)
            cur = by[cur].parent_id
        return " \u203a ".join(reversed(parts))

    return path


def _brand_names(db: Session, ids: list) -> dict:
    """Brand id -> name. The brand table is found from the foreign key, and any problem just means no names."""
    ids = [i for i in ids if i is not None]
    if not ids:
        return {}
    try:
        with db.begin_nested():
            ref = db.execute(
                text(
                    """
                    select c.confrelid::regclass::text
                    from pg_constraint c
                    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]
                    where c.contype = 'f' and c.conrelid = 'public.products'::regclass
                      and a.attname = 'part_manufacturer_id'
                    """
                )
            ).scalar()
            if not ref or not re.fullmatch(r'[A-Za-z0-9_".]+', ref):
                return {}
            rows = db.execute(
                text(f"select id, name from {ref} where id = any(cast(:ids as bigint[]))"), {"ids": ids}
            ).all()
            return {r.id: r.name for r in rows}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# 1. search
# --------------------------------------------------------------------------
@router.get("/search")
def search(
    q: str = Query(..., min_length=2),
    limit: int = Query(400, le=1500),
    exact: bool = Query(False, description="match the custom label SKU (or eBay item id / SKU) EXACTLY, not as part of a longer one"),
    db: Session = Depends(get_db),
):
    if exact:
        # Whole-value match only: 1032020141 will NOT find CYL008xx4419M08300xx1032020141.
        term = q.strip()
        if not term:
            raise HTTPException(400, "Type something to search for")
        params: dict = {"exact": term.upper()}
        where = (
            "(upper(btrim(coalesce(p.custom_label_sku, ''))) = cast(:exact as text) "
            "or upper(btrim(coalesce(p.ebay_item_id, ''))) = cast(:exact as text) "
            "or upper(btrim(coalesce(p.sku, ''))) = cast(:exact as text))"
        )
    else:
        words = [w for w in re.split(r"\s+", q.strip()) if w][:8]
        if not words:
            raise HTTPException(400, "Type something to search for")
        params = {f"w{i}": f"%{w}%" for i, w in enumerate(words)}
        where = " and ".join(
            f"(p.title ilike cast(:w{i} as text) or p.ebay_item_id ilike cast(:w{i} as text) or p.custom_label_sku ilike cast(:w{i} as text) or p.sku ilike cast(:w{i} as text))"
            for i in range(len(words))
        )
    total = db.execute(text(f"select count(*) from products p where {where}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            select p.id, p.ebay_item_id, p.custom_label_sku, p.title, p.list_price, p.quantity_on_hand, p.is_published,
                   (select count(*) from channel_listings cl where cl.product_id = p.id) as listing_count,
                   (select count(*) from product_fitment pf where pf.product_id = p.id) as fitment_count,
                   (select coalesce(sum(l.merged_count), 0) from part_merge_log l
                     where l.survivor_product_id = p.id) as prior_merges,
                   {_PN_SUBQUERY} as part_number
            from products p
            where {where}
            order by p.title, p.id
            limit :limit
            """
        ),
        {**params, "limit": limit},
    ).all()
    return {"total": total, "shown": len(rows), "items": [dict(r._mapping) for r in rows]}


@router.get("/by-items")
def by_items(
    ids: str = Query(..., min_length=1, description="comma / space separated eBay item numbers, e.g. EBAY-178554199250"),
    db: Session = Depends(get_db),
):
    """Look up specific products by eBay item number (EBAY-178554199250 or just the number), matching the
    product's own eBay item ID or ANY eBay listing linked to it. An in-house SKU (EI-0000123) also works.
    Returns the products found plus the entries that matched nothing, so the page can say which one."""
    tokens: list[str] = []
    for t in re.split(r"[,\s;]+", ids.strip()):
        t = t.strip().upper()
        if t and t not in tokens:
            tokens.append(t)
    if not tokens:
        raise HTTPException(400, "Type at least one item number")
    if len(tokens) > 100:
        raise HTTPException(400, "Look up at most 100 item numbers at a time")

    def digits(t: str) -> str:
        return re.sub(r"[^0-9]", "", t)

    skus = [t for t in tokens if t.startswith("EI-") and t != PLACEHOLDER_SKU]
    nums = sorted({digits(t) for t in tokens if not t.startswith("EI-") and digits(t)})
    rows = db.execute(
        text(
            f"""
            select p.id, p.ebay_item_id, p.custom_label_sku, p.title, p.list_price, p.quantity_on_hand, p.is_published,
                   p.sku as in_house_sku,
                   (select count(*) from channel_listings cl where cl.product_id = p.id) as listing_count,
                   (select count(*) from product_fitment pf where pf.product_id = p.id) as fitment_count,
                   (select coalesce(sum(l.merged_count), 0) from part_merge_log l
                     where l.survivor_product_id = p.id) as prior_merges,
                   (select array_agg(cast(cl.external_id as text)) from channel_listings cl
                     where cl.product_id = p.id) as listing_ids,
                   {_PN_SUBQUERY} as part_number
            from products p
            where upper(p.sku) = any(cast(:skus as text[]))
               or regexp_replace(coalesce(p.ebay_item_id, ''), '[^0-9]', '', 'g') = any(cast(:nums as text[]))
               or exists (select 1 from channel_listings cl
                          where cl.product_id = p.id
                            and regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = any(cast(:nums as text[])))
            order by p.title, p.id
            """
        ),
        {"skus": skus, "nums": nums},
    ).all()

    items = []
    seen_nums: set[str] = set()
    seen_skus: set[str] = set()
    for r in rows:
        d = dict(r._mapping)
        seen_nums.add(digits(d["ebay_item_id"] or ""))
        seen_nums.update(digits(str(e)) for e in (d.pop("listing_ids", None) or []))
        seen_skus.add(str(d.pop("in_house_sku", "")).upper())
        items.append(d)
    not_found = [
        t
        for t in tokens
        if (t.startswith("EI-") and t not in seen_skus) or (not t.startswith("EI-") and digits(t) not in seen_nums)
    ]
    return {"items": items, "not_found": not_found}


# --------------------------------------------------------------------------
# 2. preview (read-only)
# --------------------------------------------------------------------------
class PreviewIn(BaseModel):
    survivor_id: int
    product_ids: list[int] = Field(..., description="every ticked product, including the survivor")


@router.post("/preview")
def preview(body: PreviewIn, db: Session = Depends(get_db)):
    ids = sorted(set(body.product_ids))
    if body.survivor_id not in ids:
        raise HTTPException(400, "The product you keep must be one of the ticked products")
    if len(ids) < 2:
        raise HTTPException(400, "Tick at least two products to merge")
    if len(ids) - 1 > MAX_MERGE:
        raise HTTPException(400, f"Merge at most {MAX_MERGE} products at a time")
    merged_ids = [i for i in ids if i != body.survivor_id]

    rows = _load(db, ids)
    if len(rows) != len(ids):
        found = {r.id for r in rows}
        raise HTTPException(409, f"Some products no longer exist (already merged?): {sorted(set(ids) - found)[:10]}")

    _, problems = _blockers(db, merged_ids)
    label_problem = _label_block_message(rows, body.survivor_id)
    if label_problem:
        problems = [label_problem, *problems]

    fitment_rows = db.execute(
        text(
            """
            select vm.id as model_id, mk.name as make, vm.name as model, vt.slug as vehicle_type,
                   v.id as vehicle_id, v.year, v.submodel,
                   array_agg(distinct pf.product_id) as from_products
            from product_fitment pf
            join vehicles v on v.id = pf.vehicle_id
            join vehicle_models vm on vm.id = v.model_id
            join vehicle_makes mk on mk.id = vm.make_id
            left join vehicle_types vt on vt.id = vm.vehicle_type_id
            where pf.product_id = any(cast(:ids as bigint[]))
            group by vm.id, mk.name, vm.name, vt.slug, v.id, v.year, v.submodel
            order by mk.name, vm.name, v.year
            """
        ),
        {"ids": ids},
    ).all()

    by_id = {r.id: r for r in rows}
    survivor = by_id[body.survivor_id]

    cat_path = _category_path_fn(db)
    brand_name = _brand_names(db, [r.part_manufacturer_id for r in rows])

    def ref(r) -> dict:
        return {
            "id": r.id,
            "ebay_item_id": r.ebay_item_id,
            "title": r.title,
            "kept": r.id == body.survivor_id,
            "part_number": r.part_number,
            "list_price": float(r.list_price) if r.list_price is not None else None,
            "category": cat_path(r.category_id),
        }

    def groups(rs, keyfn) -> list[dict]:
        g: dict[str, list] = {}
        for r in rs:
            g.setdefault(keyfn(r), []).append(ref(r))
        # biggest group first, so the odd one out is easy to spot at the bottom
        return [{"label": k, "products": v} for k, v in sorted(g.items(), key=lambda kv: (-len(kv[1]), kv[0]))]

    details: list[dict] = []

    def warn(kind: str, text_: str, explain: str, grps: Optional[list] = None) -> None:
        details.append({"kind": kind, "text": text_, "explain": explain, "groups": grps or []})

    if len({r.category_id for r in rows}) > 1:
        warn(
            "category",
            "These products are in DIFFERENT categories. Make sure they are really the same part.",
            "Every product sits in one category on the website (for example Engine \u203a Pistons). These "
            "products are not all in the same one. Either one of them was put in the wrong category, or "
            "they are really different parts. The kept product keeps its own category: "
            f"{cat_path(survivor.category_id)}.",
            groups(rows, lambda r: cat_path(r.category_id)),
        )
    if len({r.part_manufacturer_id for r in rows}) > 1:
        warn(
            "brand",
            "These products have DIFFERENT brands set.",
            "The brand (Tusk, Wiseco, HiFlo and so on) is not the same on every product. The kept product "
            "keeps its own brand.",
            groups(rows, lambda r: brand_name.get(r.part_manufacturer_id) or ("No brand set" if r.part_manufacturer_id is None else f"Brand #{r.part_manufacturer_id}")),
        )
    prices = [float(r.list_price) for r in rows if r.list_price is not None]
    if prices and max(prices) - min(prices) > 0.005:
        warn(
            "price",
            f"Prices differ (from {min(prices):.2f} to {max(prices):.2f}). The survivor's price is kept.",
            f"The kept product's price ({float(survivor.list_price):.2f}) stays as it is and the other prices "
            "are thrown away. A big gap usually means different sizes, pack counts or kits."
            if survivor.list_price is not None
            else "The kept product has no price, so check it after the merge. Other prices are thrown away.",
            groups(rows, lambda r: f"{float(r.list_price):.2f}" if r.list_price is not None else "No price"),
        )
    def family_of(r) -> Optional[str]:
        m = re.match(r"[A-Za-z]+[0-9]+", r.part_number.upper()) if r.part_number else None
        return m.group(0) if m else None

    families = sorted({f for r in rows if (f := family_of(r))})
    if len(families) > 1:
        warn(
            "family",
            "The part numbers come from "
            f"{len(families)} different families ({', '.join(families[:6])}). "
            "Confirm these are the same physical part (e.g. same size/fit) before merging.",
            "A family is the first letters and digits of a part number (HF303 and HF204 would be HF3 and HF2). "
            "Manufacturers usually use a different family for a different size or fit, for example a different "
            "oil filter for a different engine. Open a few of the products below and compare the part numbers and "
            "titles. If they are truly the same part sold under several numbers, tick the box and carry on. If not, "
            "go back and untick the odd ones out.",
            groups(rows, lambda r: family_of(r) or "No part number on file"),
        )
    other_masters = [r for r in rows if r.id != body.survivor_id and r.prior_merges]
    if other_masters and not survivor.prior_merges:
        top = max(other_masters, key=lambda r: r.listing_count)
        warn(
            "master",
            f"Product #{top.id} is already a merged master ({top.listing_count} eBay listings). "
            "You are keeping a product that has never been merged. Usually you want to keep the master.",
            "A master is a product that earlier merges were already folded into. Keeping a different product "
            "works, but everything on the master moves across.",
            [{"label": "Merged masters", "products": [ref(r) for r in other_masters]}],
        )
    elif other_masters:
        warn(
            "master",
            f"{len(other_masters)} of the ticked products are themselves merged masters. "
            "All of their eBay listings, sales and fitment will move onto the one you keep.",
            "A master is a product that earlier merges were already folded into.",
            [{"label": "Merged masters", "products": [ref(r) for r in other_masters]}],
        )
    unpub = [r.id for r in rows if not r.is_published]
    if unpub and survivor.is_published is False:
        warn(
            "unpublished",
            "The product you are keeping is not published on the website.",
            "Customers cannot see the kept product on the website until it is published.",
            [{"label": "Kept product", "products": [ref(survivor)]}],
        )
    if any(r.is_universal for r in rows):
        warn(
            "universal",
            "At least one of these is marked universal-fit.",
            "Universal-fit means the part fits every machine. You will be asked about this on the compatible "
            "vehicles step.",
            [{"label": "Marked universal-fit", "products": [ref(r) for r in rows if r.is_universal]}],
        )
    warnings = [d["text"] for d in details]

    qtys = [r.quantity_on_hand or 0 for r in rows]
    return {
        "fingerprint": _fingerprint(rows),
        "survivor": dict(survivor._mapping),
        "survivor_is_master": bool(survivor.prior_merges),
        "merging_away": [dict(by_id[i]._mapping) for i in merged_ids],
        "totals": {
            "products_deleted": len(merged_ids),
            "ebay_listings_total": sum(r.listing_count for r in rows),
            "ebay_listings_moving": sum(by_id[i].listing_count for i in merged_ids),
            "sales_rows_moving": sum(by_id[i].sales_count for i in merged_ids),
            "inventory_rows_moving": sum(by_id[i].inventory_tx_count for i in merged_ids),
            "po_lines_moving": sum(by_id[i].po_line_count for i in merged_ids),
        },
        # NOT summed on purpose - see module docstring. The person types the real number.
        "quantity": {
            "survivor": survivor.quantity_on_hand or 0,
            "largest_single": max(qtys) if qtys else 0,
            "sum_of_all": sum(qtys),
        },
        "proposed_fitment": _models_with_years(fitment_rows),
        "proposed_is_universal": any(r.is_universal for r in rows),
        "warnings": warnings,
        "warning_details": details,
        "blockers": problems,
    }


# --------------------------------------------------------------------------
# 3. commit
# --------------------------------------------------------------------------
class MergeIn(BaseModel):
    survivor_id: int
    product_ids: list[int]
    fingerprint: str                     # from /preview; proves nothing changed since
    vehicle_ids: list[int]               # the verified final fitment of the kept product
    is_universal: bool = False
    quantity_on_hand: int = Field(..., ge=0)   # must be typed by the person
    confirm_count: int                   # must equal the number of products being deleted
    note: Optional[str] = None


@router.post("/merge")
def merge(body: MergeIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    ids = sorted(set(body.product_ids))
    sid = body.survivor_id
    if sid not in ids:
        raise HTTPException(400, "The product you keep must be one of the ticked products")
    merged = [i for i in ids if i != sid]
    if not merged:
        raise HTTPException(400, "Nothing to merge")
    if len(merged) > MAX_MERGE:
        raise HTTPException(400, f"Merge at most {MAX_MERGE} products at a time")
    if body.confirm_count != len(merged):
        raise HTTPException(400, "Confirmation count doesn't match the number of products being merged")
    vids = sorted(set(body.vehicle_ids))
    if not vids and not body.is_universal:
        raise HTTPException(400, "The kept product needs at least one vehicle, or must be marked universal")

    try:
        # Lock every involved row so two people can't merge overlapping families at once.
        locked = db.execute(
            text("select id from products where id = any(cast(:ids as bigint[])) order by id for update"),
            {"ids": ids},
        ).all()
        if len(locked) != len(ids):
            db.rollback()
            raise HTTPException(409, "Some of these products no longer exist - someone may have just merged them. Start again.")

        rows = _load(db, ids)
        if _fingerprint(rows) != body.fingerprint:
            db.rollback()
            raise HTTPException(409, "These products changed since you previewed them. Nothing was merged - open the preview again.")

        label_problem = _label_block_message(rows, sid)
        if label_problem:
            db.rollback()
            raise HTTPException(409, label_problem)

        fks, problems = _blockers(db, merged)
        if problems:
            db.rollback()
            raise HTTPException(409, " ".join(problems))

        if vids:
            n = db.execute(
                text("select count(*) from vehicles where id = any(cast(:v as bigint[]))"), {"v": vids}
            ).scalar()
            if n != len(vids):
                db.rollback()
                raise HTTPException(400, "The fitment list contains a vehicle that doesn't exist")

        by_id = {r.id: r for r in rows}
        survivor = by_id[sid]
        qty_before = survivor.quantity_on_hand or 0

        # ---- snapshot everything that is about to be moved or deleted -------------------
        snap = db.execute(
            text(
                """
                select jsonb_build_object(
                  'products',  (select coalesce(jsonb_agg(to_jsonb(p)), jsonb_build_array())
                                  from products p where p.id = any(cast(:ids as bigint[]))),
                  'fitment',   (select coalesce(jsonb_agg(to_jsonb(pf)), jsonb_build_array())
                                  from product_fitment pf where pf.product_id = any(cast(:ids as bigint[]))),
                  'photos',    (select coalesce(jsonb_agg(to_jsonb(pp)), jsonb_build_array())
                                  from product_photos pp where pp.product_id = any(cast(:ids as bigint[]))),
                  'specifics', (select coalesce(jsonb_agg(to_jsonb(s)), jsonb_build_array())
                                  from product_item_specifics s where s.product_id = any(cast(:ids as bigint[]))),
                  'listings',  (select coalesce(jsonb_agg(jsonb_build_object(
                                    'id', cl.id, 'product_id', cl.product_id,
                                    'channel_id', cl.channel_id, 'external_id', cl.external_id)),
                                  jsonb_build_array())
                                  from channel_listings cl where cl.product_id = any(cast(:ids as bigint[])))
                )
                """
            ),
            {"ids": ids},
        ).scalar()
        snap_json = json.dumps(snap if isinstance(snap, (dict, list)) else json.loads(snap))

        log_id = db.execute(
            text(
                """
                insert into part_merge_log
                  (merged_by_user_id, merged_by_email, survivor_product_id, survivor_title,
                   merged_product_ids, merged_count, qty_before, qty_after, note, snapshot)
                values (:uid, :email, :sid, :title, cast(:merged as bigint[]), :cnt, :qb, :qa, :note,
                        cast(:snap as jsonb))
                returning id
                """
            ),
            {
                "uid": user.user_id,
                "email": user.email,
                "sid": sid,
                "title": survivor.title,
                "merged": merged,
                "cnt": len(merged),
                "qb": qty_before,
                "qa": body.quantity_on_hand,
                "note": body.note,
                "snap": snap_json,
            },
        ).scalar()

        # ---- keep each eBay listing's own data (title, price, description ...) -----------
        # The duplicates are about to be deleted; this is what keeps their listings editable.
        item_digits = sorted({re.sub(r"\D", "", r.ebay_item_id or "") for r in rows} - {""})
        db.execute(
            text(
                """
                insert into ebay_listings
                  (ebay_item_id, store, product_id, original_product_id, custom_label_sku, sku, title,
                   list_price, quantity_on_hand, condition, description, item_specifics,
                   fitment_vehicle_ids, photos, product_data, merge_log_id, source)
                select d.digits, coalesce(st.store, :main), :sid, p.id, p.custom_label_sku, sv.sku_display,
                       p.title, p.list_price, p.quantity_on_hand,
                       to_jsonb(p)->>'condition', to_jsonb(p)->>'description',
                       (select coalesce(jsonb_agg(to_jsonb(s)), '[]'::jsonb)
                          from product_item_specifics s where s.product_id = p.id),
                       (select coalesce(array_agg(pf.vehicle_id order by pf.vehicle_id), '{}'::bigint[])
                          from product_fitment pf where pf.product_id = p.id),
                       (select coalesce(jsonb_agg(to_jsonb(pp)), '[]'::jsonb)
                          from product_photos pp where pp.product_id = p.id),
                       to_jsonb(p), :log, 'merge'
                from products p
                cross join lateral (
                  select regexp_replace(coalesce(p.ebay_item_id, ''), '[^0-9]', '', 'g') as digits) d
                left join lateral (
                  select cl.store from channel_listings cl
                  where regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = d.digits
                  limit 1) st on true
                join products sv on sv.id = :sid
                where p.id = any(cast(:ids as bigint[])) and d.digits <> ''
                on conflict (ebay_item_id) do update
                  set product_id = excluded.product_id, sku = excluded.sku,
                      -- a saved copy that has no description yet gets the product's, never the other way round
                      description = coalesce(nullif(btrim(ebay_listings.description), ''), excluded.description),
                      updated_at = now()
                """
            ),
            {"main": MAIN_STORE, "sid": sid, "log": log_id, "ids": ids},
        )
        saved = db.execute(
            text("select count(*) from ebay_listings where ebay_item_id = any(cast(:d as text[]))"),
            {"d": item_digits},
        ).scalar()
        if saved != len(item_digits):
            db.rollback()
            raise HTTPException(
                500,
                f"Safety check failed: only {saved} of {len(item_digits)} eBay listings could be saved "
                "before merging. Nothing was changed.",
            )

        # ---- move eBay links, sales history, inventory ledger, PO lines -----------------
        for fk in fks:
            if fk["table"] in MOVE_TABLES:
                db.execute(
                    text(
                        f"update {fk['table']} set {fk['col']} = :sid "
                        f"where {fk['col']} = any(cast(:m as bigint[]))"
                    ),
                    {"sid": sid, "m": merged},
                )

        # ---- stock: the person typed the real quantity ----------------------------------
        db.execute(
            text(
                "update products set quantity_on_hand = :q, is_universal = :u, updated_at = now() "
                "where id = :id"
            ),
            {"q": body.quantity_on_hand, "u": body.is_universal, "id": sid},
        )
        delta = body.quantity_on_hand - qty_before
        adjusted = 0
        if delta != 0:
            db.execute(
                text(
                    "insert into inventory_transactions "
                    "(product_id, transaction_type, quantity_change, reference, legacy_created_by) "
                    "values (:p, 'Adjustment', :d, :ref, :by)"
                ),
                {"p": sid, "d": delta, "ref": f"Parts clean-up merge #{log_id}", "by": user.email},
            )
            adjusted = 1

        # ---- fitment: survivor ends with exactly the verified list ----------------------
        db.execute(
            text(
                "delete from product_fitment where product_id = :pid "
                "and not (vehicle_id = any(cast(:v as bigint[])))"
            ),
            {"pid": sid, "v": vids},
        )
        if vids:
            db.execute(
                text(
                    "insert into product_fitment (product_id, vehicle_id, source) "
                    "select :pid, v.id, 'admin' from vehicles v "
                    "where v.id = any(cast(:v as bigint[])) "
                    "and not exists (select 1 from product_fitment pf "
                    "                where pf.product_id = :pid and pf.vehicle_id = v.id)"
                ),
                {"pid": sid, "v": vids},
            )

        # ---- photos: if the kept product has none, adopt one duplicate's set ------------
        if survivor.photo_count == 0:
            donor = next((r.id for r in rows if r.id != sid and r.photo_count > 0), None)
            if donor:
                db.execute(
                    text("update product_photos set product_id = :sid where product_id = :d"),
                    {"sid": sid, "d": donor},
                )

        # ---- description: if the kept product has none, adopt the fullest duplicate's ----
        # (like photos above; the duplicates are deleted next, so this is the only chance to keep it)
        kept_has_desc = db.execute(
            text("select nullif(btrim(coalesce(description, '')), '') is not null from products where id = :s"),
            {"s": sid},
        ).scalar()
        if not kept_has_desc:
            donor_desc = db.execute(
                text(
                    "select id from products where id = any(cast(:m as bigint[])) "
                    "and nullif(btrim(coalesce(description, '')), '') is not null "
                    "order by length(description) desc, id limit 1"
                ),
                {"m": merged},
            ).scalar()
            if donor_desc:
                db.execute(
                    text(
                        "update products set description = (select description from products where id = :d), "
                        "updated_at = now() where id = :s"
                    ),
                    {"d": donor_desc, "s": sid},
                )

        # ---- delete the duplicates (descriptive child rows cascade) ---------------------
        db.execute(text("delete from products where id = any(cast(:m as bigint[]))"), {"m": merged})

        # ---- verify nothing was lost BEFORE committing ----------------------------------
        def count(table: str) -> int:
            return db.execute(text(f"select count(*) from {table} where product_id = :s"), {"s": sid}).scalar()

        expected = {
            "channel_listings": sum(r.listing_count for r in rows),
            "sales_transactions": sum(r.sales_count for r in rows),
            "inventory_transactions": sum(r.inventory_tx_count for r in rows) + adjusted,
            "purchase_order_lines": sum(r.po_line_count for r in rows),
        }
        for table, want in expected.items():
            got = count(table)
            if got != want:
                db.rollback()
                raise HTTPException(
                    500,
                    f"Safety check failed on {table} (expected {want}, found {got}). "
                    "Nothing was changed.",
                )
        left = db.execute(
            text("select count(*) from products where id = any(cast(:m as bigint[]))"), {"m": merged}
        ).scalar()
        if left:
            db.rollback()
            raise HTTPException(500, "Safety check failed: duplicates still exist. Nothing was changed.")

        finish(db, refresh=True)
        return {
            "log_id": log_id,
            "survivor_id": sid,
            "products_deleted": len(merged),
            "ebay_listings_now_on_part": expected["channel_listings"],
            "listing_records_saved": saved,
            "fitment_vehicles": len(vids),
            "quantity_on_hand": body.quantity_on_hand,
        }
    except HTTPException:
        raise
    except Exception as exc:  # any surprise = full rollback
        db.rollback()
        raise HTTPException(500, f"Merge failed and was rolled back; nothing changed. ({type(exc).__name__})")


# --------------------------------------------------------------------------
# 6. refill descriptions that a merge lost, by reading them from the live eBay listings
#    Reads eBay only (GetItem). Nothing is ever sent to eBay. Only blank descriptions are written.
# --------------------------------------------------------------------------
_BLANK_DESC = "nullif(btrim(coalesce(p.description, '')), '') is null"
_MERGED_BLANK = (
    f"from products p where {_BLANK_DESC} "
    "and exists (select 1 from part_merge_log l where l.survivor_product_id = p.id) "
    "and exists (select 1 from channel_listings cl where cl.product_id = p.id)"
)


@router.get("/blank-descriptions")
def blank_descriptions(db: Session = Depends(get_db)):
    """Kept parts of earlier merges that have no description and have an eBay listing to read it from."""
    total = db.execute(text(f"select count(*) {_MERGED_BLANK}")).scalar()
    return {"total": total}


class FillDescIn(BaseModel):
    after_id: int = 0
    limit: int = Field(5, ge=1, le=10)


@router.post("/fill-descriptions")
def fill_descriptions(body: FillDescIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """One batch: the next few kept parts (by id, after `after_id`) with a blank description. For each, the part's
    eBay listings are read (the kept part's own listing first, active ones first) and the first real description is
    cleaned and saved. The page calls this again with `next_after_id` until `done`."""
    from app.ebay_edit import _read_item
    from app.sanitize import clean_description

    ids = [
        r[0]
        for r in db.execute(
            text(f"select p.id {_MERGED_BLANK} and p.id > :a order by p.id limit :n"),
            {"a": body.after_id, "n": body.limit},
        ).all()
    ]
    results = []
    for pid in ids:
        listings = db.execute(
            text(
                "select coalesce(nullif(btrim(cl.store), ''), :main) as store, "
                "       regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') as item "
                "from channel_listings cl join products p on p.id = cl.product_id "
                "where cl.product_id = :p "
                "order by (regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') "
                "          = regexp_replace(coalesce(p.ebay_item_id, ''), '[^0-9]', '', 'g')) desc, "
                "         (lower(coalesce(cl.status, '')) = 'active') desc, cl.id limit 6"
            ),
            {"p": pid, "main": MAIN_STORE},
        ).all()
        status, why = "none", "no eBay listing had a description"
        for lst in listings:
            if not lst.item:
                continue
            try:
                item = _read_item(db, lst.store, lst.item)
            except HTTPException as e:
                why = f"eBay item {lst.item}: {e.detail}"
                continue
            except Exception as e:  # never let one listing stop the batch
                db.rollback()
                why = f"eBay item {lst.item}: {type(e).__name__}"
                continue
            cleaned = clean_description(item.get("description"))
            if not cleaned:
                continue
            done = db.execute(
                text(
                    "update products p set description = :d, updated_at = now() "
                    f"where p.id = :p and {_BLANK_DESC}"
                ),
                {"d": cleaned, "p": pid},
            ).rowcount
            db.commit()
            status, why = ("filled", f"from eBay item {lst.item}") if done else ("skipped", "it was filled in meanwhile")
            break
        results.append({"id": pid, "status": status, "note": why})
    return {
        "results": results,
        "next_after_id": ids[-1] if ids else body.after_id,
        "done": len(ids) < body.limit,
    }


# --------------------------------------------------------------------------
# 5. "does a product like this already exist?"  (used by the Add product form)
#    Read-only. It only WARNS; it never blocks creating a product.
# --------------------------------------------------------------------------
_STOPWORDS = {"the", "and", "for", "with", "fits", "fit", "new", "from", "your", "you"}


def _title_tokens(title: str) -> list[str]:
    """The distinctive words of a title: no filler words, no model years."""
    out: list[str] = []
    for tok in re.findall(r"[a-z0-9][a-z0-9.\-]*", title.lower()):
        tok = tok.rstrip(".-")
        if len(tok) < 3 or tok in _STOPWORDS or re.fullmatch(r"(19|20)\d\d(-(19|20)?\d\d)?", tok):
            continue
        if tok not in out:
            out.append(tok)
    return sorted(out, key=len, reverse=True)[:8]


@router.get("/similar")
def similar(
    title: str = Query("", max_length=300),
    ebay_item_id: Optional[str] = Query(None, max_length=100),
    part_number: Optional[str] = Query(None, max_length=100),
    exclude_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    sku = clean_ebay_item_id(ebay_item_id)
    pn = (part_number or "").strip() or None
    tokens = _title_tokens(title)
    use_title = len(tokens) >= 2
    if not use_title and not sku and not pn:
        return {"matches": []}

    words = tokens if use_title else []
    params: dict = {f"t{i}": f"%{t}%" for i, t in enumerate(words)}
    params.update({"sku": sku, "pn": pn, "x": exclude_id})
    score = " + ".join(
        f"(case when p.title ilike cast(:t{i} as text) then 1 else 0 end)" for i in range(len(words))
    ) or "0"
    need = max(2, math.ceil(len(tokens) * 0.7)) if use_title else 1000
    params["need"] = need

    rows = db.execute(
        text(
            f"""
            select * from (
              select p.id, p.ebay_item_id, p.title, p.list_price, p.quantity_on_hand, p.is_published,
                     (select count(*) from channel_listings cl where cl.product_id = p.id) as listing_count,
                     (select coalesce(sum(l.merged_count), 0) from part_merge_log l
                       where l.survivor_product_id = p.id) as prior_merges,
                     ({score}) as score,
                     (cast(:sku as text) is not null and lower(p.ebay_item_id) = lower(cast(:sku as text))) as same_sku,
                     (cast(:pn as text) is not null and (
                        lower(p.manufacturer_part_number) = lower(cast(:pn as text))
                        or exists (select 1 from product_item_specifics s
                                    where s.product_id = p.id and s.name in ({_PN_NAMES})
                                      and lower(s.value) = lower(cast(:pn as text)))
                     )) as same_pn
              from products p
              where p.id <> coalesce(cast(:x as bigint), -1)
            ) m
            where m.same_sku or m.same_pn or m.score >= :need
            order by m.same_pn desc, m.same_sku desc, m.score desc, m.listing_count desc, m.id
            limit 8
            """
        ),
        params,
    ).all()

    matches = []
    for r in rows:
        d = dict(r._mapping)
        if d["same_pn"]:
            reason = "same part number"
        elif d["same_sku"]:
            reason = "same eBay item ID"
        else:
            reason = f"title shares {d['score']} of {len(tokens)} key words"
        matches.append(
            {
                "id": d["id"],
                "ebay_item_id": d["ebay_item_id"],
                "title": d["title"],
                "list_price": d["list_price"],
                "quantity_on_hand": d["quantity_on_hand"],
                "is_published": d["is_published"],
                "listing_count": d["listing_count"],
                "prior_merges": d["prior_merges"],
                "reason": reason,
            }
        )
    return {"matches": matches}


# --------------------------------------------------------------------------
# 4. history
# --------------------------------------------------------------------------
@router.get("/log")
def merge_log(limit: int = Query(50, le=500), db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "select id, created_at, merged_by_email, survivor_product_id, survivor_title, "
            "merged_count, qty_before, qty_after, note "
            "from part_merge_log order by id desc limit :limit"
        ),
        {"limit": limit},
    ).all()
    return [dict(r._mapping) for r in rows]
