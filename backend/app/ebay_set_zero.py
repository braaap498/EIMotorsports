"""Set a live eBay listing's quantity to 0 WITHOUT ending it.

POST /ebay-edit/set-zero   {store, item_id}

Uses ReviseInventoryStatus. The listing keeps its item number, photos and history. eBay only accepts
quantity 0 on a listing when the "out of stock" preference is ON in that eBay account; if it is off, eBay's
error is passed back as it is. Uses the helpers already in app/ebay_edit.py (same login, same push log,
same "live pushes" switch). Register it in main.py next to the ebay_edit router:

    from app import ebay_set_zero
    app.include_router(ebay_set_zero.router)
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.database import get_db
from app.ebay_edit import (
    ITEM_RE,
    NS,
    _errors,
    _failed,
    _first_message,
    _log,
    _read_item,
    _store_row,
    _trading,
)

router = APIRouter(prefix="/ebay-edit", tags=["ebay-edit"])


class SetZeroIn(BaseModel):
    store: str
    item_id: str = Field(..., max_length=20)


@router.post("/set-zero")
def set_zero(body: SetZeroIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    row = _store_row(db, body.store)
    enabled = db.execute(text("select live_pushes_enabled from ebay_accounts where store = :s"), {"s": row.store}).scalar()
    if not enabled:
        raise HTTPException(403, f"Live pushes are OFF for {row.store}. Turn them on in the Stores box first.")
    item_id = body.item_id.strip()
    if not ITEM_RE.fullmatch(item_id):
        raise HTTPException(400, "That is not an eBay item number.")
    current = _read_item(db, row.store, item_id)
    if current["ended"]:
        raise HTTPException(409, "That listing has ended. Activate it first; quantity 0 only applies to a live listing.")
    if current["has_variations"]:
        raise HTTPException(409, "Listings with variations are not supported here yet (each variation needs its own quantity).")
    if not current["editable"]:
        raise HTTPException(409, current.get("not_editable_reason") or "This listing cannot be changed here.")

    inner = f"<InventoryStatus><ItemID>{item_id}</ItemID><Quantity>0</Quantity></InventoryStatus>"
    root = _trading(db, row.store, "ReviseInventoryStatus", inner)
    errs = _errors(root)
    ok = not _failed(root)
    request = {"item_id": item_id, "quantity": 0}
    _log(db, user, row.store, item_id, "set_zero", False, request, {"ack": root.findtext(f"{NS}Ack"), "errors": errs}, ok, None if ok else _first_message(root))
    if not ok:
        raise HTTPException(
            502,
            "eBay did not accept quantity 0: " + _first_message(root)
            + " (If this mentions quantity, the out-of-stock setting is probably OFF in that eBay account.)",
        )
    db.execute(
        text(
            "update channel_listings set last_pushed_at = now() "
            "where regexp_replace(cast(external_id as text), '[^0-9]', '', 'g') = :i and store = :s"
        ),
        {"i": item_id, "s": row.store},
    )
    db.commit()
    return {"ok": True, "item_id": item_id, "warnings": [e for e in errs if e["severity"] == "Warning"]}
