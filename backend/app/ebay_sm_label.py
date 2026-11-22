"""Keep eBay's Selling Manager inventory records (Seller Hub > Listings > Inventory) in step with a product's label.

Why: sending a product's custom label to its live listings (kits.py sync) changes each listing's SKU only. Selling
Manager keeps its own inventory record per product, with its own custom label, and eBay does not rename it.

Two parts:
  1. A saved copy of the store's inventory records (table ebay_sm_inventory). The "eBay inventory labels" page fills
     it: every scan stores each record's id, name and label. Reading all ~17,500 records takes minutes, so the
     per-product action below never reads eBay for the list, it uses the copy.
  2. sync_inventory(): for one product, finds the saved records whose name is the product's title or the title of one
     of its linked listings (and no other product's), and revises their custom label to the product's label.
     kits.py calls it right after the listings are updated, and POST /ebay-edit/sm-label calls it on its own
     (apply=false only looks).

The bulk page (POST /ebay-edit/sm-scan and /sm-apply) fixes records that still have the OLD comma label.

NOT VERIFIED beyond what was tried by hand: ReviseSellingManagerProduct with the ProductID inside
SellingManagerProductDetails worked on the first live records. Register in main.py (already done):

    from app.ebay_sm_label import router as ebay_sm_label_router
    admin_router.include_router(ebay_sm_label_router)
"""
import logging
import re
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.database import get_db
from app.ebay_edit import NS, _errors, _failed, _first_message, _log, _store_row, _trading
from app.kits import _DIGITS, _linked_listings, _product_cls

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-edit", tags=["ebay-edit"], dependencies=[Depends(require_admin)])


class SmLabelIn(BaseModel):
    product_id: int
    apply: bool = False


def _revise(db: Session, store: str, pid: str, label: str) -> tuple[bool, list[dict], str]:
    """Try the documented shape first, then a flatter one; return (ok, errors, message)."""
    pid_x, lab_x = escape(pid), escape(label)
    shapes = [
        # eBay's own error said the ProductID belongs inside SellingManagerProductDetails
        f"<SellingManagerProductDetails><ProductID>{pid_x}</ProductID><CustomLabel>{lab_x}</CustomLabel></SellingManagerProductDetails>",
        f"<ProductID>{pid_x}</ProductID><SellingManagerProductDetails><ProductID>{pid_x}</ProductID><CustomLabel>{lab_x}</CustomLabel></SellingManagerProductDetails>",
    ]
    msgs: list[str] = []
    errs: list[dict] = []
    for inner in shapes:
        root = _trading(db, store, "ReviseSellingManagerProduct", inner)
        errs = _errors(root)
        if not _failed(root):
            return True, errs, ""
        msgs.append(_first_message(root))
    return False, errs, " | ".join(dict.fromkeys(msgs))[:500]


_TABLE_OK = False


def _ensure_table(db: Session) -> None:
    global _TABLE_OK
    if _TABLE_OK:
        return
    db.execute(text(
        "create table if not exists ebay_sm_inventory ("
        " store text not null, sm_product_id text not null, name text not null default '', norm_name text not null default '',"
        " label text not null default '', seen_at timestamptz not null default now(),"
        " primary key (store, sm_product_id))"
    ))
    db.execute(text("create index if not exists ebay_sm_inventory_name_idx on ebay_sm_inventory (store, norm_name)"))
    db.commit()
    _TABLE_OK = True


def _save_copy(db: Session, store: str, recs: list[dict]) -> None:
    """Keep what eBay just told us about these inventory records (every record, not only the ones to change)."""
    if not recs:
        return
    _ensure_table(db)
    db.execute(
        text(
            "insert into ebay_sm_inventory (store, sm_product_id, name, norm_name, label, seen_at) "
            "values (:s, :i, :n, :k, :l, now()) "
            "on conflict (store, sm_product_id) do update set name = excluded.name, norm_name = excluded.norm_name, "
            "label = excluded.label, seen_at = now()"
        ),
        [{"s": store, "i": r["product_id"], "n": r["name"], "k": _norm_title(r["name"]), "l": r["label"]} for r in recs],
    )
    db.commit()


def _product_titles(db: Session, product_id: int) -> set[str]:
    """Normalised titles that belong to this product: its own and its linked eBay listings'. A title that another
    product also has is left out, so one product's label never lands on another product's inventory record."""
    ltitle = "coalesce(nullif(btrim(lr.title), ''), nullif(btrim(la.title), ''), nullif(btrim(lc.title), ''))"
    rows = db.execute(
        text(
            f"select p.title as t from products p where p.id = :p union all "
            f"select {ltitle} from channel_listings cl "
            f"left join ebay_report_rows lr on lr.item_id = {_DIGITS} "
            f"left join ebay_api_details la on la.item_id = {_DIGITS} "
            f"left join ebay_compat_items lc on lc.item_id = {_DIGITS} where cl.product_id = :p"
        ),
        {"p": product_id},
    ).all()
    keys = {_norm_title(r.t) for r in rows if r.t and _norm_title(r.t)}
    if not keys:
        return set()
    owners = _title_owners(db, list(keys))
    return {k for k in keys if owners.get(k, set()) <= {product_id}}


def sync_inventory(db: Session, user: AuthUser, product_id: int, apply: bool) -> list[dict]:
    """One result per inventory record found for the product (or a single note when none are)."""
    cls = _product_cls(db, product_id)
    if not cls:
        raise HTTPException(400, "This product has no custom label.")
    _ensure_table(db)
    stores = [r[0] for r in db.execute(text("select distinct store from channel_listings where product_id = :p"), {"p": product_id}).all() if r[0]]
    keys = list(_product_titles(db, product_id))
    out: list[dict] = []
    if not stores or not keys:
        return [{"store": "", "sm_product_id": "", "name": "", "before": "", "after": cls, "status": "none",
                 "detail": "Nothing to match: this product has no linked eBay listing or title."}]
    recs = db.execute(
        text("select store, sm_product_id, name, label from ebay_sm_inventory where lower(store) = any(:s) and norm_name = any(:k) order by store, name"),
        {"s": [x.lower() for x in stores], "k": keys},
    ).all()
    if not recs:
        have = db.execute(text("select count(*) from ebay_sm_inventory where lower(store) = any(:s)"), {"s": [x.lower() for x in stores]}).scalar() or 0
        why = ("No inventory record matches this product's titles." if have else
               "No saved copy of the inventory yet. Open Sales channels > Store API > eBay inventory labels and press Find records once.")
        return [{"store": stores[0], "sm_product_id": "", "name": "", "before": "", "after": cls, "status": "none", "detail": why}]
    for r in recs:
        res = {"store": r.store, "sm_product_id": r.sm_product_id, "name": r.name, "before": r.label, "after": cls, "status": "", "detail": ""}
        out.append(res)
        if r.label == cls:
            res["status"], res["detail"] = "already_current", "Already has this label."
            continue
        if not apply:
            res["status"], res["detail"] = "would_change", f"{r.label or '(none)'} -> {cls}"
            continue
        try:
            enabled = db.execute(text("select live_pushes_enabled from ebay_accounts where lower(store) = lower(:s)"), {"s": r.store}).scalar()
            if not enabled:
                res["status"], res["detail"] = "not_sent", f"Live pushes are OFF for {r.store}."
                continue
            ok, errs, msg = _revise(db, r.store, r.sm_product_id, cls)
            _log(db, user, r.store, r.sm_product_id, "sm_product_label", False,
                 {"sm_product_id": r.sm_product_id, "from": r.label, "to": cls}, {"errors": errs}, ok, None if ok else msg)
            if ok:
                db.execute(text("update ebay_sm_inventory set label = :l, seen_at = now() where store = :s and sm_product_id = :i"),
                           {"l": cls, "s": r.store, "i": r.sm_product_id})
                db.commit()
                res["status"], res["detail"] = "updated", f"{r.label or '(none)'} -> {cls}"
            else:
                res["status"], res["detail"] = "error", "eBay did not accept it: " + msg
        except HTTPException as e:
            db.rollback()
            res["status"], res["detail"] = "error", str(e.detail)
        except Exception as e:
            db.rollback()
            log.exception("inventory record %s label update failed", r.sm_product_id)
            res["status"], res["detail"] = "error", f"Something unexpected went wrong with this record ({type(e).__name__}: {str(e).strip().splitlines()[0][:200] if str(e).strip() else ''})."
    return out


@router.post("/sm-label")
def sm_label(body: SmLabelIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    return {"cls": _product_cls(db, body.product_id), "applied": body.apply, "results": sync_inventory(db, user, body.product_id, body.apply)}


# --------------------------------------------------------------------------
# bulk: find inventory records that still have the OLD label (with commas) and give them the new one
# --------------------------------------------------------------------------
# The kit label used to join its parts with "," (sometimes ", ") and now joins them with "xx". The new label of an old
# record is NOT just a swap: it is the label a product really has. The old label is split into its part numbers, each
# is cleaned the way the kit builder cleans it, they are joined with "xx", and the result must equal (any letter case)
# the custom label of one of your products. The product's own spelling is what gets written.
OLD_JOIN, NEW_JOIN = ",", "xx"
ENTRIES_PER_PAGE = 200


def _candidate(old: str) -> str:
    toks = [re.sub(r"[^A-Za-z0-9\-_.]", "", t) for t in re.split(r"[,\s]+", old)]
    return NEW_JOIN.join(t for t in toks if t)


def _resolve(db: Session, olds: list[str]) -> dict[str, Optional[str]]:
    """old label -> the product's custom label, or None when no product has it."""
    cands = {o: _candidate(o) for o in olds}
    want = list({c.lower() for c in cands.values() if c})
    have: dict[str, str] = {}
    if want:
        for lab in db.execute(
            text("select custom_label_sku from products where lower(custom_label_sku) = any(:l)"), {"l": want}
        ).scalars():
            have[lab.lower()] = lab
    return {o: (have.get(c.lower()) if c else None) for o, c in cands.items()}


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _title_hits(db: Session, keys: list[str]) -> dict[str, set]:
    """normalised title -> {(product id, product label)} for products whose own title, or whose linked eBay listing's
    title, equals it."""
    hit: dict[str, set] = {}
    if not keys:
        return hit
    norm = "btrim(regexp_replace(lower({c}), '[^a-z0-9]+', ' ', 'g'))"
    ltitle = "coalesce(nullif(btrim(lr.title), ''), nullif(btrim(la.title), ''), nullif(btrim(lc.title), ''), '')"
    rows = db.execute(
        text(
            f"select k, id, label from ("
            f"  select {norm.format(c='p.title')} as k, p.id, p.custom_label_sku as label from products p "
            f"  union all "
            f"  select {norm.format(c=ltitle)} as k, p.id, p.custom_label_sku as label "
            f"  from channel_listings cl join products p on p.id = cl.product_id "
            f"  left join ebay_report_rows lr on lr.item_id = {_DIGITS} "
            f"  left join ebay_api_details la on la.item_id = {_DIGITS} "
            f"  left join ebay_compat_items lc on lc.item_id = {_DIGITS} "
            f") t where k = any(:k)"
        ),
        {"k": keys},
    ).all()
    for r in rows:
        hit.setdefault(r.k, set()).add((r.id, (r.label or "").strip()))
    return hit


def _title_owners(db: Session, keys: list[str]) -> dict[str, set]:
    return {k: {i for i, _ in v} for k, v in _title_hits(db, keys).items()}


def _resolve_by_title(db: Session, names: list[str]) -> dict[str, Optional[str]]:
    """inventory record name -> the custom label of the ONE product it belongs to by name, or None.
    A name belongs to a product when it equals (letters and digits only, any case) the product's own title or the title
    of one of that product's linked eBay listings. None when no product, or more than one product, matches, or when
    the product has no label."""
    hit = _title_hits(db, list({_norm_title(n) for n in names if _norm_title(n)}))
    out: dict[str, Optional[str]] = {}
    for n in names:
        found = hit.get(_norm_title(n), set())
        ids = {i for i, _ in found}
        labels = {lab for _, lab in found}
        out[n] = next(iter(labels)) if len(ids) == 1 and len(labels) == 1 and next(iter(labels)) else None
    return out


class SmScanIn(BaseModel):
    store: str
    page: int = Field(1, ge=1)
    pages: int = Field(6, ge=1, le=12)


@router.post("/sm-scan")
def sm_scan(body: SmScanIn, db: Session = Depends(get_db)):
    """Reads a few pages of the store's Selling Manager inventory (nothing is written) and returns the records whose
    custom label still has a comma. The page asks again with `next_page` until it is null."""
    row = _store_row(db, body.store)
    scanned, total_pages = 0, None
    found: list[dict] = []
    page = body.page
    last = body.page + body.pages
    while page < last and (total_pages is None or page <= total_pages):
        inner = f"<Pagination><EntriesPerPage>{ENTRIES_PER_PAGE}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>"
        root = _trading(db, row.store, "GetSellingManagerInventory", inner)
        if _failed(root):
            raise HTTPException(502, "eBay would not list the inventory records: " + _first_message(root))
        tp = (root.findtext(f".//{NS}PaginationResult/{NS}TotalNumberOfPages") or "").strip()
        if tp.isdigit():
            total_pages = int(tp)
        page_recs: list[dict] = []
        for n in root.iter(f"{NS}SellingManagerProduct"):
            pid = (n.findtext(f".//{NS}ProductID") or "").strip()
            label = (n.findtext(f".//{NS}CustomLabel") or "").strip()
            if not pid:
                continue
            scanned += 1
            page_recs.append({"product_id": pid, "name": (n.findtext(f".//{NS}ProductName") or "").strip(), "label": label})
            if OLD_JOIN in label:
                found.append({"product_id": pid, "name": (n.findtext(f".//{NS}ProductName") or "").strip(), "before": label})
        _save_copy(db, row.store, page_recs)
        page += 1
        if total_pages is None:  # eBay did not say how many pages there are: stop at an empty page
            if not list(root.iter(f"{NS}SellingManagerProduct")):
                break
    mapping = _resolve(db, list({f["before"] for f in found})) if found else {}
    by_title = _resolve_by_title(db, list({f["name"] for f in found if not mapping.get(f["before"])})) if found else {}
    ready, nomatch = [], []
    for f in found:
        after, via = mapping.get(f["before"]), "label"
        if not after:
            after, via = by_title.get(f["name"]), "name"
        if after and after != f["before"] and "," not in after:
            ready.append({**f, "after": after, "via": via})
        else:
            nomatch.append({**f, "after": "", "via": ""})
    more = total_pages is not None and page <= total_pages
    return {
        "store": row.store,
        "total_pages": total_pages,
        "scanned": scanned,
        "next_page": page if more else None,
        "ready": ready,
        "no_match": nomatch,
    }


class SmItem(BaseModel):
    product_id: str = Field(..., max_length=40)
    before: str = Field(..., max_length=200)
    after: str = Field(..., max_length=200)
    name: str = Field("", max_length=300)
    via: str = Field("label", pattern="^(label|name)$")


class SmApplyIn(BaseModel):
    store: str
    items: list[SmItem] = Field(..., min_length=1, max_length=25)


@router.post("/sm-apply")
def sm_apply(body: SmApplyIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    """Changes the listed inventory records. The server re-checks each one: the new label must be the custom label of the
    product that matches the record, by its old part numbers or by its exact name. Anything else is refused, whatever the page sent."""
    row = _store_row(db, body.store)
    enabled = db.execute(text("select live_pushes_enabled from ebay_accounts where store = :s"), {"s": row.store}).scalar()
    if not enabled:
        raise HTTPException(403, f"Live pushes are OFF for {row.store}. Turn them on in the Stores box first.")
    out: list[dict] = []
    for it in body.items:
        res = {"product_id": it.product_id, "before": it.before, "after": it.after, "status": "", "detail": ""}
        out.append(res)
        if it.via == "name":
            good = bool(it.name) and _resolve_by_title(db, [it.name]).get(it.name) == it.after
        else:
            good = _resolve(db, [it.before]).get(it.before) == it.after
        if OLD_JOIN not in it.before or not good or "," in it.after:
            res["status"], res["detail"] = "refused", "That is not the custom label a product has for this record."
            continue
        try:
            ok, errs, msg = _revise(db, row.store, it.product_id, it.after)
            _log(db, user, row.store, it.product_id, "sm_product_label", False,
                 {"sm_product_id": it.product_id, "from": it.before, "to": it.after}, {"errors": errs}, ok, None if ok else msg)
            res["status"], res["detail"] = ("updated", "") if ok else ("error", "eBay did not accept it: " + msg)
        except HTTPException as e:
            db.rollback()
            res["status"], res["detail"] = "error", str(e.detail)
    return {"results": out}
