"""
Photo backup list for the dashboard's "Back up photos" button.

The button runs in the owner's browser (Chrome or Edge on the office PC): it asks this endpoint which files the store
uses, then copies the ones that are not in the chosen folder yet. Nothing is written on the server, and no database
password is involved.

  GET /admin/storage-backup/list       {base_url, count, paths}      owner / admin only (no @needs marker)
  GET /admin/storage-backup/manifest   {photos, ebay}                owner / admin only (no @needs marker)

The manifest is what the button turns into product_photos.csv, ebay_label_import.csv and the by_sku/<SKU>/ folders.
Without it the button stops with "photo index could not be loaded (Not Found)".

paths are the bucket paths of every photo the site refers to (product photos, hand-catalog pictures, page pictures):
  full/<key>
  thumb/<key without extension>.webp
"""
import json
import logging
import re

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_route_access
from app.config import get_settings
from app.database import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/storage-backup", tags=["admin-storage-backup"], dependencies=[Depends(require_route_access)])

_SAFE = re.compile(r"^[A-Za-z0-9_\-./]+$")

# every place that stores a photo key; a table that does not exist yet is skipped
_KEY_QUERIES = (
    "select distinct image_key from product_photos where nullif(btrim(image_key), '') is not null",
    "select distinct image_key from manual_catalog_items where nullif(btrim(image_key), '') is not null",
    "select distinct image_key from site_pages where nullif(btrim(image_key), '') is not null",
)


@router.get("/list")
def list_paths(db: Session = Depends(get_db)):
    keys: set[str] = set()
    for q in _KEY_QUERIES:
        try:
            keys.update(r[0].strip() for r in db.execute(text(q)).all())
        except Exception:
            db.rollback()
    paths: set[str] = set()
    for k in keys:
        if ".." in k or k.startswith("/") or not _SAFE.match(k):
            continue
        paths.add(f"full/{k}")
        paths.add(f"thumb/{k.rsplit('.', 1)[0]}.webp")
    ordered = sorted(paths)
    return {"base_url": get_settings().image_base_url.rstrip("/"), "count": len(ordered), "paths": ordered}


@router.get("/manifest")
def manifest(db: Session = Depends(get_db)):
    """One row per product_photos row (with the owning product's SKU) plus the eBay item -> custom label map.

    sku is products.sku_display: the EI number once assigned, otherwise the eBay item number, so unassigned
    products never all land in one EI-0000000 folder. It is null for a photo with no product yet; the button
    then uses the EBAY-<item> folder in storage_path.
    """
    rows = db.execute(
        text(
            "select pp.id, pp.product_id, pp.storage_path, pp.source_url, coalesce(pp.is_primary, false) as is_primary, "
            "       coalesce(pp.sort_order, 0) as sort_order, pp.created_at, pp.image_key, "
            "       coalesce(nullif(btrim(cast(p.sku_display as text)), ''), "
            "         (select nullif(btrim(cast(p2.sku_display as text)), '') from channel_listings cl "
            "            join products p2 on p2.id = cl.product_id "
            "           where pp.product_id is null "
            "             and regexp_replace(cast(cl.external_id as text), '[^0-9]', '', 'g') = substring(pp.storage_path from '^EBAY-([0-9]+)/') "
            "           limit 1)) as sku "
            "from product_photos pp left join products p on p.id = pp.product_id "
            "where nullif(btrim(pp.image_key), '') is not null "
            "order by pp.product_id nulls last, pp.sort_order, pp.id"
        )
    ).all()
    photos = []
    for r in rows:
        key = (r.image_key or "").strip()
        if ".." in key or key.startswith("/") or not _SAFE.match(key):
            continue
        photos.append(
            {
                "id": r.id,
                "product_id": r.product_id,
                "storage_path": r.storage_path or "",
                "source_url": r.source_url,
                "is_primary": bool(r.is_primary),
                "sort_order": int(r.sort_order or 0),
                "created_at": r.created_at.isoformat() if r.created_at is not None else "",
                "image_key": key,
                "sku": r.sku,
            }
        )
    ebay: list[dict] = []
    try:
        for r in db.execute(text("select item_number, custom_label_sku from ebay_label_import order by item_number")).all():
            ebay.append({"item_number": str(r[0]), "custom_label_sku": "" if r[1] is None else str(r[1])})
    except Exception:
        db.rollback()  # the table may not exist yet; the photo CSV is still useful
    # Each eBay listing also keeps its own saved photo set (ebay_listings.photos). A SKU's folder gets those too.
    listing_photos: list[dict] = []
    try:
        for r in db.execute(
            text(
                "select el.ebay_item_id, nullif(btrim(cast(p.sku_display as text)), '') as sku, el.photos "
                "from ebay_listings el join products p on p.id = el.product_id "
                "where el.photos is not null"
            )
        ).all():
            raw = r.photos
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    continue
            if not isinstance(raw, list) or not r.sku:
                continue
            for n, e in enumerate(raw):
                if not isinstance(e, dict):
                    continue
                key = str(e.get("image_key") or "").strip()
                if not key or ".." in key or key.startswith("/") or not _SAFE.match(key):
                    continue
                so = e.get("sort_order")
                listing_photos.append(
                    {
                        "sku": r.sku,
                        "image_key": key,
                        "is_primary": e.get("is_primary") is True,
                        "sort_order": int(so) if isinstance(so, (int, float)) else n,
                        "item": str(r.ebay_item_id),
                    }
                )
    except Exception:
        db.rollback()  # the table or its photos column may not exist; the product photos are still returned
    return {"photos": photos, "ebay": ebay, "listing_photos": listing_photos}
