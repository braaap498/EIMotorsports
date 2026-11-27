"""
Photo backup list for the dashboard's "Back up photos" button.

The button runs in the owner's browser (Chrome or Edge on the office PC): it asks this endpoint which files the store
uses, then copies the ones that are not in the chosen folder yet. Nothing is written on the server, and no database
password is involved.

  GET /admin/storage-backup/list   {base_url, count, paths}   owner / admin only (no @needs marker)

paths are the bucket paths of every photo the site refers to (product photos, hand-catalog pictures, page pictures):
  full/<key>
  thumb/<key without extension>.webp
"""
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
