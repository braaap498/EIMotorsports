"""Keep eBay's Selling Manager inventory records in step with a product's custom label.

POST /ebay-edit/sm-label   {product_id, apply: false, limit: null}

Why: sending a new custom label to a live listing (kits.py sync) changes the listing's SKU only. Seller Hub's
Listings > Inventory page shows Selling Manager "products" (folder, template, custom label) that eBay keeps
separately, so those rows keep the OLD label. This reads each linked listing's Selling Manager product and, when
asked, revises its custom label to the product's current one.

  apply=false  only reads from eBay and says what would change. Nothing is written anywhere.
  apply=true   revises the records (needs the store's live-pushes switch ON). `limit` = how many records to change
               in this request, so one can be tried first.

NOT VERIFIED AGAINST A LIVE ACCOUNT. The calls used are GetItem (SellingManagerProductDetails) and
ReviseSellingManagerProduct. If eBay refuses, its message is returned for that record and nothing else is touched.
Register next to the other eBay routers in main.py:

    from app.ebay_sm_label import router as ebay_sm_label_router
    admin_router.include_router(ebay_sm_label_router)
"""
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
from app.kits import _linked_listings, _product_cls

router = APIRouter(prefix="/ebay-edit", tags=["ebay-edit"], dependencies=[Depends(require_admin)])


class SmLabelIn(BaseModel):
    product_id: int
    apply: bool = False
    limit: Optional[int] = Field(None, ge=1, le=500)


def _sm_details(db: Session, store: str, item_id: str) -> Optional[dict]:
    """The Selling Manager product behind a listing, or None when the listing was not made from one."""
    root = _trading(db, store, "GetItem", f"<ItemID>{escape(item_id)}</ItemID><DetailLevel>ReturnAll</DetailLevel>")
    if _failed(root):
        raise HTTPException(502, _first_message(root))
    node = root.find(f".//{NS}SellingManagerProductDetails")
    if node is None:
        return None
    pid = (node.findtext(f"{NS}ProductID") or "").strip()
    if not pid:
        return None
    return {
        "product_id": pid,
        "name": (node.findtext(f"{NS}ProductName") or "").strip(),
        "label": (node.findtext(f"{NS}CustomLabel") or "").strip(),
    }


def _revise(db: Session, store: str, pid: str, label: str) -> tuple[bool, list[dict], str]:
    """Try the documented shape first, then a flatter one; return (ok, errors, message)."""
    shapes = [
        f"<ProductID>{escape(pid)}</ProductID><SellingManagerProductDetails><CustomLabel>{escape(label)}</CustomLabel></SellingManagerProductDetails>",
        f"<ProductID>{escape(pid)}</ProductID><CustomLabel>{escape(label)}</CustomLabel>",
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


@router.post("/sm-label")
def sm_label(body: SmLabelIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    cls = _product_cls(db, body.product_id)
    if not cls:
        raise HTTPException(400, "This product has no custom label.")
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    changed = 0
    for l in _linked_listings(db, body.product_id):
        res = {"store": l["store"], "item_id": l["item_id"], "product_id": None, "name": "", "before": "", "after": cls, "status": "", "detail": ""}
        results.append(res)
        try:
            row = _store_row(db, l["store"])
            sm = _sm_details(db, row.store, l["item_id"])
            if sm is None:
                res["status"], res["detail"] = "none", "This listing is not tied to a Selling Manager product, so there is no inventory record to change."
                continue
            res.update(product_id=sm["product_id"], name=sm["name"], before=sm["label"])
            key = (row.store, sm["product_id"])
            if key in seen:
                res["status"], res["detail"] = "same_record", "Same inventory record as a listing above."
                continue
            seen.add(key)
            if sm["label"] == cls:
                res["status"], res["detail"] = "already_current", "The inventory record already has this label."
                continue
            if not body.apply:
                res["status"], res["detail"] = "would_change", f"{sm['label'] or '(none)'} -> {cls}"
                continue
            if body.limit is not None and changed >= body.limit:
                res["status"], res["detail"] = "waiting", "Not changed in this request (limit reached)."
                continue
            enabled = db.execute(text("select live_pushes_enabled from ebay_accounts where store = :s"), {"s": row.store}).scalar()
            if not enabled:
                res["status"], res["detail"] = "not_sent", f"Live pushes are OFF for {row.store}. Turn them on in the Stores box first."
                continue
            ok, errs, msg = _revise(db, row.store, sm["product_id"], cls)
            _log(db, user, row.store, l["item_id"], "sm_product_label", False,
                 {"sm_product_id": sm["product_id"], "from": sm["label"], "to": cls}, {"errors": errs}, ok, None if ok else msg)
            if ok:
                changed += 1
                res["status"], res["detail"] = "updated", f"{sm['label'] or '(none)'} -> {cls}"
            else:
                res["status"], res["detail"] = "error", "eBay did not accept it: " + msg
        except HTTPException as e:
            db.rollback()
            res["status"], res["detail"] = "error", str(e.detail)
        except Exception:
            db.rollback()
            res["status"], res["detail"] = "error", "Something unexpected went wrong with this listing."
    return {"cls": cls, "applied": body.apply, "results": results}


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


def _resolve_by_title(db: Session, names: list[str]) -> dict[str, Optional[str]]:
    """inventory record name -> the custom label of the ONE product with that same title (letters and digits only,
    any case). None when no product, or more than one product, has that title, or the product has no label."""
    keys = list({_norm_title(n) for n in names if _norm_title(n)})
    hit: dict[str, set] = {}
    if keys:
        rows = db.execute(
            text(
                "select btrim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) as k, id, custom_label_sku "
                "from products where btrim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) = any(:k)"
            ),
            {"k": keys},
        ).all()
        for r in rows:
            hit.setdefault(r.k, set()).add((r.id, (r.custom_label_sku or "").strip()))
    out: dict[str, Optional[str]] = {}
    for n in names:
        found = hit.get(_norm_title(n), set())
        labels = {lab for _, lab in found}
        out[n] = next(iter(labels)) if len(found) == 1 and len(labels) == 1 and next(iter(labels)) else None
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
        for n in root.iter(f"{NS}SellingManagerProduct"):
            pid = (n.findtext(f".//{NS}ProductID") or "").strip()
            label = (n.findtext(f".//{NS}CustomLabel") or "").strip()
            if not pid:
                continue
            scanned += 1
            if OLD_JOIN in label:
                found.append({"product_id": pid, "name": (n.findtext(f".//{NS}ProductName") or "").strip(), "before": label})
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
