"""
New Amazon listings (Sales channels > Amazon > New listing), modeled on ebay_newdraft.py + ebay_copy.py. Mounted at /admin/amazon-new/*.

  POST /drafts                       {account, marketplace, product_id, sku?, mode, asin?, product_type?}  create a draft from a website product
                                     mode "offer"  = offer only on an existing ASIN (needs asin)
                                     mode "new"    = create a new catalog item (needs product_type; attributes are filled from the product)
  POST /drafts/from-listing/{id}     "sell similar": copy an existing synced Amazon listing into a new draft (new SKU, same ASIN/type)
  GET  /drafts                       ?account=&status=    list drafts
  PUT  /drafts/{id}                  edit price, quantity, condition, title, bullets, description, attributes_json
  POST /drafts/{id}/validate         asks Amazon to check the draft (VALIDATION_PREVIEW) without creating anything; needs only a saved key
  POST /drafts/{id}/publish          sends it (PUT listings item). Needs Live pushes ON. Never publishes a draft that has not validated clean.
  GET  /fitment/{product_id}.csv     fitment export for a product (make / model / year range), as a file to upload in Seller Central

Nothing here is verified against Amazon. The attribute names under `_attributes` follow the generic Listings Items schema
(item_name, bullet_point, product_description, purchasable_offer, fulfillment_availability); the real required set depends on the
product type, so run Validate and read the issues it returns. Fitment: Amazon automotive fitment is ACES/PCdb based and varies by
marketplace; the CSV is a plain make/model/year list to convert or upload, not a guaranteed Amazon format.
"""
import csv
import io
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.amazon_core import MARKETPLACES, NA_ENDPOINT, _row, access_token
from app.auth import require_admin
from app.database import _session_factory, get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/amazon-new", tags=["admin-amazon-new"], dependencies=[Depends(require_admin)])

CURRENCY = {"US": "USD", "CA": "CAD", "MX": "MXN", "BR": "BRL"}
LANGUAGE = {"US": "en_US", "CA": "en_CA", "MX": "es_MX", "BR": "pt_BR"}


def ensure_tables() -> None:
    db = _session_factory()()
    try:
        db.execute(text(
            """create table if not exists amazon_drafts (
                 id bigserial primary key,
                 account text not null,
                 marketplace text not null default 'US',
                 product_id bigint,
                 source_listing_id bigint,
                 mode text not null default 'offer',
                 sku text not null,
                 asin text,
                 product_type text,
                 title text,
                 bullets text,
                 description text,
                 price numeric,
                 quantity integer not null default 0,
                 condition text not null default 'new_new',
                 attributes_json text,
                 status text not null default 'draft',
                 validated_ok boolean not null default false,
                 last_issues text,
                 published_at timestamptz,
                 created_at timestamptz not null default now(),
                 updated_at timestamptz not null default now(),
                 unique (account, marketplace, sku)
               )"""
        ))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not set up the Amazon drafts table")
    finally:
        db.close()


class DraftIn(BaseModel):
    account: str
    marketplace: str = "US"
    product_id: int
    sku: Optional[str] = Field(None, max_length=80)
    mode: str = Field("offer", pattern="^(offer|new)$")
    asin: Optional[str] = Field(None, max_length=20)
    product_type: Optional[str] = Field(None, max_length=80)


def _mk(m: str) -> str:
    m = (m or "").upper()
    if m not in MARKETPLACES:
        raise HTTPException(422, "Marketplace must be one of: " + ", ".join(MARKETPLACES))
    return m


@router.post("/drafts")
def create_draft(body: DraftIn, db: Session = Depends(get_db)):
    a, mk = _row(db, body.account), _mk(body.marketplace)
    if mk not in (a.marketplaces or "").split(","):
        raise HTTPException(409, f"{mk} is not enabled for this account.")
    if body.mode == "offer" and not (body.asin or "").strip():
        raise HTTPException(422, "An offer on an existing item needs its ASIN.")
    if body.mode == "new" and not (body.product_type or "").strip():
        raise HTTPException(422, "A new catalog item needs an Amazon product type.")
    p = db.execute(text(
        "select id, title, to_jsonb(p)->>'description' as description, to_jsonb(p)->>'sku_display' as sku_display, "
        "nullif(to_jsonb(p)->>'price', '')::numeric as price from products p where id = :i"), {"i": body.product_id}).first()
    if not p:
        raise HTTPException(404, "Product not found.")
    sku = (body.sku or p.sku_display or f"P{p.id}").strip()[:80]
    try:
        r = db.execute(text(
            "insert into amazon_drafts (account, marketplace, product_id, mode, sku, asin, product_type, title, description, price) "
            "values (:a, :m, :p, :mode, :sku, :asin, :pt, :t, :d, :pr) returning id"),
            {"a": a.account, "m": mk, "p": p.id, "mode": body.mode, "sku": sku, "asin": (body.asin or "").strip().upper() or None,
             "pt": (body.product_type or "").strip().upper() or None, "t": (p.title or "")[:200], "d": p.description, "pr": p.price}).first()
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(409, "That SKU already has a draft or listing on this marketplace. Choose another SKU.")
    return {"ok": True, "id": r.id, "sku": sku}


@router.post("/drafts/from-listing/{listing_id}")
def sell_similar(listing_id: int, sku: str, db: Session = Depends(get_db)):
    l = db.execute(text("select * from amazon_listings where id = :i"), {"i": listing_id}).first()
    if not l:
        raise HTTPException(404, "Listing not found.")
    sku = sku.strip()[:80]
    if not sku or sku.lower() == (l.sku or "").lower():
        raise HTTPException(422, "Give the copy a new SKU.")
    try:
        r = db.execute(text(
            "insert into amazon_drafts (account, marketplace, product_id, source_listing_id, mode, sku, asin, product_type, title, price) "
            "values (:a, :m, null, :src, 'offer', :sku, :asin, :pt, :t, :pr) returning id"),
            {"a": l.account, "m": l.marketplace, "src": l.id, "sku": sku, "asin": l.asin, "pt": l.product_type, "t": l.title, "pr": l.price}).first()
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(409, "That SKU already exists on this marketplace.")
    return {"ok": True, "id": r.id, "sku": sku, "note": "Link a product to the draft before publishing so stock can sync."}


@router.get("/drafts")
def list_drafts(account: str = "", status: str = "", db: Session = Depends(get_db)):
    w, p = ["true"], {}
    if account.strip():
        w.append("account = :a"); p["a"] = account.strip()
    if status.strip():
        w.append("status = :s"); p["s"] = status.strip()
    rows = db.execute(text(f"select * from amazon_drafts where {' and '.join(w)} order by id desc limit 200"), p).all()
    return {"drafts": [dict(r._mapping) for r in rows]}


class EditIn(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    bullets: Optional[str] = Field(None, max_length=3000)     # one bullet per line, up to 5 used
    description: Optional[str] = Field(None, max_length=5000)
    price: Optional[float] = Field(None, gt=0, le=100000)
    quantity: Optional[int] = Field(None, ge=0, le=100000)
    condition: Optional[str] = Field(None, max_length=40)
    product_id: Optional[int] = None
    attributes_json: Optional[str] = Field(None, max_length=20000)


def _fulfillment_error(extra: dict) -> str | None:
    """Checks the fulfillment_availability the owner typed (or the Fulfillment dropdown wrote). None means fine."""
    fa = extra.get("fulfillment_availability")
    if fa is None:
        return None
    if not isinstance(fa, list) or not fa or not all(isinstance(x, dict) for x in fa):
        return "fulfillment_availability must be a list of objects, e.g. [{\"fulfillment_channel_code\": \"AMAZON_NA\"}]."
    codes = {str(x.get("fulfillment_channel_code", "")).upper() for x in fa}
    if "" in codes:
        return "Each fulfillment_availability entry needs a fulfillment_channel_code (DEFAULT for merchant-fulfilled, AMAZON_NA for FBA)."
    if any(c.startswith("AMAZON") for c in codes) and "DEFAULT" in codes:
        return "A listing is either FBA (AMAZON_NA) or merchant-fulfilled (DEFAULT), not both."
    return None


@router.put("/drafts/{draft_id}")
def edit_draft(draft_id: int, body: EditIn, db: Session = Depends(get_db)):
    d = db.execute(text("select status from amazon_drafts where id = :i"), {"i": draft_id}).first()
    if not d:
        raise HTTPException(404, "Draft not found.")
    if d.status == "published":
        raise HTTPException(409, "Already published. Edit the live listing instead.")
    data = body.model_dump(exclude_none=True)
    if "attributes_json" in data and not data["attributes_json"].strip():
        data["attributes_json"] = None      # a blank box clears the extra attributes (the Fulfillment dropdown writes "" when nothing is left)
    elif "attributes_json" in data:
        try:
            parsed = json.loads(data["attributes_json"])
            if not isinstance(parsed, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Extra attributes must be a JSON object.")
        bad = _fulfillment_error(parsed)
        if bad:
            raise HTTPException(422, bad)
    if not data:
        return {"ok": True}
    sets = ", ".join(f"{k} = :{k}" for k in data)
    db.execute(text(f"update amazon_drafts set {sets}, validated_ok = false, last_issues = null, updated_at = now() where id = :id"), {**data, "id": draft_id})
    db.commit()
    return {"ok": True, "note": "Edited, so it must be validated again before publishing."}


def _is_fba(d) -> bool:
    """A draft is FBA when the owner's extra-attributes JSON sets fulfillment_availability to an Amazon channel code
    (AMAZON_NA), e.g. {"fulfillment_availability": [{"fulfillment_channel_code": "AMAZON_NA"}]}. Amazon holds the stock, so no quantity is sent."""
    try:
        extra = json.loads(d.attributes_json) if d.attributes_json else {}
        fa = extra.get("fulfillment_availability") if isinstance(extra, dict) else None
        return any(str((x or {}).get("fulfillment_channel_code", "")).upper().startswith("AMAZON") for x in (fa or []))
    except (ValueError, AttributeError):
        return False


def _attributes(d, mk: str) -> dict:
    lang, cur = LANGUAGE[mk], CURRENCY[mk]
    mid = MARKETPLACES[mk]
    attrs: dict = {
        "condition_type": [{"value": d.condition or "new_new", "marketplace_id": mid}],
        "fulfillment_availability": [{"fulfillment_channel_code": "DEFAULT", "quantity": int(d.quantity or 0)}],
    }
    if d.price:
        attrs["purchasable_offer"] = [{"marketplace_id": mid, "currency": cur, "our_price": [{"schedule": [{"value_with_tax": float(d.price)}]}]}]
    if d.asin and d.mode == "offer":
        attrs["merchant_suggested_asin"] = [{"value": d.asin, "marketplace_id": mid}]
    if d.mode == "new":
        if d.title:
            attrs["item_name"] = [{"value": d.title, "language_tag": lang, "marketplace_id": mid}]
        bl = [b.strip() for b in (d.bullets or "").splitlines() if b.strip()][:5]
        if bl:
            attrs["bullet_point"] = [{"value": b, "language_tag": lang, "marketplace_id": mid} for b in bl]
        if d.description:
            attrs["product_description"] = [{"value": d.description, "language_tag": lang, "marketplace_id": mid}]
    extra = json.loads(d.attributes_json) if d.attributes_json else {}
    attrs.update(extra)   # the owner's JSON wins, so required product-type fields can be supplied by hand
    if _is_fba(d):        # Amazon holds the stock, so never send a quantity for an FBA listing
        attrs["fulfillment_availability"] = [{"fulfillment_channel_code": str(x.get("fulfillment_channel_code", "AMAZON_NA")).upper()}
                                             for x in attrs.get("fulfillment_availability") or [] if isinstance(x, dict)]
    return attrs


def _put(db: Session, d, preview: bool) -> dict:
    a = _row(db, d.account)
    if not a.seller_id:
        raise HTTPException(409, "Save the seller ID on the account first.")
    mk = d.marketplace
    body = {"productType": d.product_type or "PRODUCT", "requirements": "LISTING_OFFER_ONLY" if d.mode == "offer" else "LISTING", "attributes": _attributes(d, mk)}
    q = {"marketplaceIds": MARKETPLACES[mk], "issueLocale": "en_US"}
    if preview:
        q["mode"] = "VALIDATION_PREVIEW"
    url = f"{NA_ENDPOINT}/listings/2021-08-01/items/{a.seller_id}/{urllib.parse.quote(d.sku, safe='')}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="PUT",
                                 headers={"x-amz-access-token": access_token(db, d.account), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"Amazon refused it ({e.code}): {e.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise HTTPException(502, "Could not reach Amazon.")


def _draft(db: Session, draft_id: int):
    d = db.execute(text("select * from amazon_drafts where id = :i"), {"i": draft_id}).first()
    if not d:
        raise HTTPException(404, "Draft not found.")
    return d


@router.post("/drafts/{draft_id}/validate")
def validate(draft_id: int, db: Session = Depends(get_db)):
    d = _draft(db, draft_id)
    res = _put(db, d, preview=True)     # a preview never creates or changes anything on Amazon
    issues = res.get("issues") or []
    errors = [i for i in issues if str(i.get("severity", "")).upper() == "ERROR"]
    ok = str(res.get("status", "")).upper() != "INVALID" and not errors
    db.execute(text("update amazon_drafts set validated_ok = :ok, last_issues = :iss, updated_at = now() where id = :i"),
               {"ok": ok, "iss": json.dumps(issues)[:4000], "i": draft_id})
    db.commit()
    return {"ok": ok, "issues": issues}


@router.post("/drafts/{draft_id}/publish")
def publish(draft_id: int, db: Session = Depends(get_db)):
    d = _draft(db, draft_id)
    a = _row(db, d.account)
    if not a.live_pushes_enabled:
        raise HTTPException(409, "Live pushes are OFF for this Amazon account. Nothing was sent.")
    if not d.validated_ok:
        raise HTTPException(409, "Validate the draft first (and fix any errors). Nothing was sent.")
    if d.status == "published":
        raise HTTPException(409, "Already published.")
    res = _put(db, d, preview=False)
    if str(res.get("status", "")).upper() == "INVALID":
        raise HTTPException(502, "Amazon marked it invalid: " + json.dumps(res.get("issues"))[:300])
    db.execute(text("update amazon_drafts set status = 'published', published_at = now(), updated_at = now() where id = :i"), {"i": draft_id})
    # record it so the next sync links it; if the product is known, link right away
    db.execute(text(
        "insert into amazon_listings (account, marketplace, sku, asin, title, product_type, status, price, quantity, fulfillment, product_id) "
        "values (:a, :m, :s, :asin, :t, :pt, 'SUBMITTED', :pr, :q, :ful, :p) on conflict (account, marketplace, sku) do nothing"),
        {"a": d.account, "m": d.marketplace, "s": d.sku, "asin": d.asin, "t": d.title, "pt": d.product_type, "pr": d.price, "q": 0 if _is_fba(d) else d.quantity, "ful": "FBA" if _is_fba(d) else "MFN", "p": d.product_id})
    db.commit()
    return {"ok": True, "submission": res.get("submissionId"), "status": res.get("status")}


# ---- fitment export ----------------------------------------------------------------------------
def _pick(cols: set, *names: str) -> Optional[str]:
    return next((n for n in names if n in cols), None)


@router.get("/fitment/{product_id}.csv")
def fitment_csv(product_id: int, db: Session = Depends(get_db)):
    cols = {r[0] for r in db.execute(text("select column_name from information_schema.columns where table_name = 'product_fitment'")).all()}
    if not cols or "product_id" not in cols:
        raise HTTPException(404, "There is no product_fitment table with a product_id column to export from.")
    make, model = _pick(cols, "make", "make_name"), _pick(cols, "model", "model_name")
    y1, y2 = _pick(cols, "year_start", "year_from", "start_year", "year_min"), _pick(cols, "year_end", "year_to", "end_year", "year_max")
    year = _pick(cols, "year")
    if not (make and model and (year or y1)):
        raise HTTPException(409, "Could not tell which product_fitment columns hold make, model and year. Columns found: " + ", ".join(sorted(cols)))
    sel = [f"{make} as make", f"{model} as model"] + ([f"{y1} as y1", f"{y2 or y1} as y2"] if y1 else [f"{year} as y1", f"{year} as y2"])
    rows = db.execute(text(f"select distinct {', '.join(sel)} from product_fitment where product_id = :p order by 1, 2, 3"), {"p": product_id}).all()
    sku = db.execute(text("select coalesce(to_jsonb(p)->>'sku_display', id::text) from products p where id = :p"), {"p": product_id}).scalar()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["sku", "make", "model", "year_start", "year_end"])
    for r in rows:
        w.writerow([sku, r.make, r.model, r.y1, r.y2])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="fitment_{product_id}.csv"'})
