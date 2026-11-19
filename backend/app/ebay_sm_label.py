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
