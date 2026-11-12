"""
Admin: manage the photos of an existing product.

    POST   /admin/products/{id}/photos                  add one photo
    POST   /admin/products/{id}/photos/{photo}/replace  swap a photo's picture
    POST   /admin/products/{id}/photos/{photo}/main     make it the main photo
    POST   /admin/products/{id}/photos/order            set the order of the others
    POST   /admin/products/{id}/photos/arrange          set the main photo and the order together
    POST   /admin/products/{id}/photos/{photo}/delete   remove a photo
    POST   /admin/products/{id}/photos/hidden           turn photos off (or back on) without deleting them
    POST   /admin/products/{id}/photos/delete-preview   which stored files a delete would remove
    GET    /admin/photo-library?q=...                   find pictures already in storage, by product
    POST   /admin/products/{id}/photos/attach           add pictures already in storage (no upload)

The picture itself is sent as base64 text inside JSON (the admin screen shrinks
it first, so it is small). Every route answers with the product's full, current
photo list so the screen can simply show it.

How the store picks photos (unchanged): the main photo is the one with
is_primary = true, then the rest by sort_order. After every change the
order is tidied here so there is exactly one main photo and sort_order is 0, 1, 2...

A photo can be "hidden" (product_photos.hidden = true): it stays on the product, in the admin screen and in storage,
but is left out of the website and of eBay listings. The main photo is always a visible one when the product has any.

Employees need the "products.photos" permission (see app/permissions.py).
"""
import base64
import binascii
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app import photo_store
from app.auth import require_route_access, needs
from app.database import _session_factory, get_db
from app.images import full_url, thumb_url

log = logging.getLogger(__name__)

router = APIRouter(tags=["admin-photos"], dependencies=[Depends(require_route_access)])

MAX_BASE64_CHARS = 21 * 1024 * 1024  # a 15 MB file is about 20 MB as text


def ensure_columns() -> None:
    """Add product_photos.hidden if it is missing (every photo starts as not hidden). Called once at startup."""
    db = _session_factory()()
    try:
        db.execute(text("alter table product_photos add column if not exists hidden boolean not null default false"))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not add product_photos.hidden; hiding photos will not work until it exists")
    finally:
        db.close()


class NewPhoto(BaseModel):
    data: str = Field(..., max_length=MAX_BASE64_CHARS, description="the picture, base64 encoded")


class PhotoOrder(BaseModel):
    photo_ids: list[int]


class PhotoArrangement(BaseModel):
    photo_ids: list[int] = Field(..., max_length=500, description="all the photos, main photo first")


class HiddenIn(BaseModel):
    photo_ids: list[int] = Field(..., min_length=1, max_length=200)
    hidden: bool


class DeletePreview(BaseModel):
    photo_ids: list[int] = Field(..., max_length=200)


class AttachPhotos(BaseModel):
    image_keys: list[str] = Field(..., max_length=50)


# ------------------------------------------------------------------ helpers
def _decode(data: str) -> bytes:
    if "," in data[:100]:  # tolerate a "data:image/jpeg;base64," prefix
        data = data.split(",", 1)[1]
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "The picture could not be read. Try a different file.")


def _process(data: str) -> photo_store.Processed:
    try:
        return photo_store.process_image(_decode(data))
    except photo_store.PhotoError as e:
        raise HTTPException(400, str(e))


def _upload(p: photo_store.Processed) -> None:
    try:
        photo_store.upload(p)
    except photo_store.StorageError as e:
        raise HTTPException(502, str(e))


def _require_product(db: Session, product_id: int) -> None:
    if not db.execute(text("select 1 from products where id = :id"), {"id": product_id}).first():
        raise HTTPException(404, "Product not found")


def _photo_or_404(db: Session, product_id: int, photo_id: int):
    row = db.execute(
        text("select id, image_key, is_primary from product_photos where id = :id and product_id = :p"),
        {"id": photo_id, "p": product_id},
    ).first()
    if not row:
        raise HTTPException(404, "Photo not found on this product")
    return row


def _tidy(db: Session, product_id: int, first_ids: list[int] | None = None) -> None:
    """
    Exactly one main photo, and sort_order 0..n-1 in display order. `first_ids` pins the order of the
    non-main photos. The main photo is the current one, unless it is hidden and a visible photo exists:
    then the first visible photo takes over (a hidden photo is never the one the shop leads with).
    """
    rows = db.execute(
        text(
            "select id, is_primary, hidden from product_photos where product_id = :p "
            "order by is_primary desc, sort_order, id"
        ),
        {"p": product_id},
    ).all()
    if not rows:
        return
    ids = [r.id for r in rows]
    main = next((r.id for r in rows if r.is_primary and not r.hidden), None)
    if main is None:
        main = next((r.id for r in rows if not r.hidden), None)
    if main is None:  # every photo is hidden: keep the current main one
        main = next((r.id for r in rows if r.is_primary), ids[0])
    rest = [i for i in ids if i != main]
    if first_ids:
        wanted = [i for i in first_ids if i in rest]
        rest = wanted + [i for i in rest if i not in wanted]
    # Clear the old main first, so two photos are never "main" at the same moment.
    db.execute(text("update product_photos set is_primary = false where product_id = :p"), {"p": product_id})
    for position, pid in enumerate([main] + rest):
        db.execute(
            text("update product_photos set is_primary = :m, sort_order = :o where id = :id"),
            {"m": pid == main, "o": position, "id": pid},
        )


def _listing(db: Session, product_id: int) -> dict:
    rows = db.execute(
        text(
            "select id, source_url, storage_path, image_key, is_primary, hidden, sort_order from product_photos "
            "where product_id = :p order by is_primary desc, sort_order, id"
        ),
        {"p": product_id},
    ).all()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["thumb"] = thumb_url(d["image_key"]) or d["source_url"]
        d["url"] = full_url(d["image_key"]) or d["source_url"]
        out.append(d)
    return {"photos": out}


def _forget_files(db: Session, keys: list[str | None]) -> None:
    """After a photo row is gone: delete its stored files unless another row still uses them."""
    for key in {k for k in keys if k}:
        still_used = db.execute(
            text("select 1 from product_photos where image_key = :k limit 1"), {"k": key}
        ).first()
        if not still_used:
            photo_store.remove([key])


def _like_escape(token: str) -> str:
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ------------------------------------------------------------------- routes
@router.post("/products/{product_id}/photos", status_code=201)
@needs("products.photos")
def add_photo(product_id: int, body: NewPhoto, db: Session = Depends(get_db)):
    _require_product(db, product_id)
    p = _process(body.data)
    if db.execute(
        text("select 1 from product_photos where product_id = :p and image_key = :k"),
        {"p": product_id, "k": p.key},
    ).first():
        raise HTTPException(409, "That picture is already on this product.")
    _upload(p)
    next_pos = db.execute(
        text("select coalesce(max(sort_order), -1) + 1 from product_photos where product_id = :p"),
        {"p": product_id},
    ).scalar()
    db.execute(
        text(
            "insert into product_photos (product_id, image_key, storage_path, is_primary, sort_order) "
            "values (:p, :k, :sp, false, :o)"
        ),
        {"p": product_id, "k": p.key, "sp": photo_store.full_path(p.key), "o": next_pos},
    )
    _tidy(db, product_id)  # the first photo of a product becomes the main one
    db.commit()
    return _listing(db, product_id)


@router.post("/products/{product_id}/photos/order")
@needs("products.photos")
def reorder_photos(product_id: int, body: PhotoOrder, db: Session = Depends(get_db)):
    _require_product(db, product_id)
    _tidy(db, product_id, first_ids=body.photo_ids)
    db.commit()
    return _listing(db, product_id)


@router.post("/products/{product_id}/photos/arrange")
@needs("products.photos")
def arrange_photos(product_id: int, body: PhotoArrangement, db: Session = Depends(get_db)):
    """
    Saves a whole arrangement in one step: the first id becomes the main photo and
    the rest follow in the order given. Photos missing from the list keep their
    relative order after it, so nothing is lost if the list is out of date.
    """
    _require_product(db, product_id)
    existing = [
        r.id
        for r in db.execute(
            text("select id from product_photos where product_id = :p order by is_primary desc, sort_order, id"),
            {"p": product_id},
        ).all()
    ]
    wanted = [i for i in dict.fromkeys(body.photo_ids) if i in existing]
    final = wanted + [i for i in existing if i not in wanted]
    # Clear the old main first, so two photos are never "main" at the same moment (a one-main-per-product rule in the
    # database would otherwise reject the save halfway through).
    db.execute(text("update product_photos set is_primary = false where product_id = :p"), {"p": product_id})
    for position, pid in enumerate(final):
        db.execute(
            text("update product_photos set is_primary = :m, sort_order = :o where id = :id and product_id = :p"),
            {"m": position == 0, "o": position, "id": pid, "p": product_id},
        )
    _tidy(db, product_id)  # a hidden photo moved to the front does not stay the main one
    db.commit()
    return _listing(db, product_id)


@router.post("/products/{product_id}/photos/{photo_id}/main")
@needs("products.photos")
def make_main(product_id: int, photo_id: int, db: Session = Depends(get_db)):
    _photo_or_404(db, product_id, photo_id)
    if db.execute(text("select hidden from product_photos where id = :id"), {"id": photo_id}).scalar():
        raise HTTPException(409, "That photo is turned off. Turn it back on before making it the main photo.")
    db.execute(
        text("update product_photos set is_primary = (id = :id) where product_id = :p"),
        {"id": photo_id, "p": product_id},
    )
    _tidy(db, product_id)
    db.commit()
    return _listing(db, product_id)


@router.post("/products/{product_id}/photos/{photo_id}/replace")
@needs("products.photos")
def replace_photo(product_id: int, photo_id: int, body: NewPhoto, db: Session = Depends(get_db)):
    old = _photo_or_404(db, product_id, photo_id)
    p = _process(body.data)
    if p.key != old.image_key and db.execute(
        text("select 1 from product_photos where product_id = :p and image_key = :k"),
        {"p": product_id, "k": p.key},
    ).first():
        raise HTTPException(409, "That picture is already on this product.")
    _upload(p)
    # The old eBay link (source_url) pointed at the old picture, so it is cleared.
    db.execute(
        text("update product_photos set image_key = :k, storage_path = :sp, source_url = null, hidden = false where id = :id"),
        {"k": p.key, "sp": photo_store.full_path(p.key), "id": photo_id},
    )
    db.commit()
    if old.image_key and old.image_key != p.key:
        _forget_files(db, [old.image_key])
    return _listing(db, product_id)


@router.post("/products/{product_id}/photos/hidden")
@needs("products.photos")
def set_hidden(product_id: int, body: HiddenIn, db: Session = Depends(get_db)):
    """
    Turn photos off (hidden = true) or back on. A hidden photo is kept: it stays on the product and in storage, but
    the website and eBay listings do not use it. If the main photo is turned off, the first visible photo becomes main.
    """
    _require_product(db, product_id)
    ids = list(dict.fromkeys(body.photo_ids))
    db.execute(
        text("update product_photos set hidden = :h where product_id = :p and id in :ids").bindparams(
            bindparam("ids", expanding=True)
        ),
        {"h": body.hidden, "p": product_id, "ids": ids},
    )
    _tidy(db, product_id)
    db.commit()
    return _listing(db, product_id)


@router.post("/products/{product_id}/photos/{photo_id}/delete")
@needs("products.photos")
def delete_photo(product_id: int, photo_id: int, db: Session = Depends(get_db)):
    old = _photo_or_404(db, product_id, photo_id)
    db.execute(text("delete from product_photos where id = :id"), {"id": photo_id})
    _tidy(db, product_id)  # if the main photo was deleted, the next one takes over
    db.commit()
    _forget_files(db, [old.image_key])
    return _listing(db, product_id)


@router.post("/products/{product_id}/photos/delete-preview")
@needs("products.photos")
def delete_preview(product_id: int, body: DeletePreview, db: Session = Depends(get_db)):
    """
    Says, for each photo about to be deleted, whether its stored file would go too.
    `last_use` is true when no photo row OUTSIDE the selection uses the same file
    (on this product or any other). Only advice for the screen: the real delete
    still does its own check.
    """
    _require_product(db, product_id)
    ids = list(dict.fromkeys(body.photo_ids))
    if not ids:
        return {"items": []}
    rows = db.execute(
        text("select id, image_key from product_photos where product_id = :p and id in :ids").bindparams(
            bindparam("ids", expanding=True)
        ),
        {"p": product_id, "ids": ids},
    ).all()
    items = []
    for r in rows:
        entry = {"id": r.id, "image_key": r.image_key, "last_use": False, "also_on": []}
        if r.image_key:
            others = db.execute(
                text(
                    "select distinct p.sku_display as sku from product_photos pp join products p on p.id = pp.product_id "
                    "where pp.image_key = :k and pp.id not in :ids order by p.sku_display limit 4"
                ).bindparams(bindparam("ids", expanding=True)),
                {"k": r.image_key, "ids": ids},
            ).all()
            entry["also_on"] = [o.sku for o in others]
            entry["last_use"] = not others
        items.append(entry)
    return {"items": items}


@router.get("/photo-library")
@needs("products.photos")
def photo_library(
    q: str = Query("", max_length=100),
    exclude: int = 0,
    db: Session = Depends(get_db),
):
    """Products (other than `exclude`) whose title, SKU or stored file name matches every word in q."""
    tokens = [t for t in q.split() if t][:5]
    if not tokens or sum(len(t) for t in tokens) < 2:
        return {"products": []}
    params: dict = {"ex": exclude}
    conds = []
    for i, t in enumerate(tokens):
        params[f"t{i}"] = f"%{_like_escape(t)}%"
        conds.append(
            f"(p.sku ilike :t{i} or p.ebay_item_id ilike :t{i} or p.title ilike :t{i} or exists (select 1 from product_photos x "
            f"where x.product_id = p.id and x.image_key ilike :t{i}))"
        )
    prods = db.execute(
        text(
            "select p.id, p.sku_display as sku, p.title from products p "
            "where p.id <> :ex and exists (select 1 from product_photos y where y.product_id = p.id and y.image_key is not null) "
            f"and {' and '.join(conds)} order by p.id desc limit 12"
        ),
        params,
    ).all()
    if not prods:
        return {"products": []}
    photo_rows = db.execute(
        text(
            "select product_id, image_key from product_photos "
            "where product_id in :ids and image_key is not null "
            "order by product_id, is_primary desc, sort_order, id"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": [p.id for p in prods]},
    ).all()
    by_product: dict[int, list[dict]] = {p.id: [] for p in prods}
    seen: set[tuple[int, str]] = set()
    for r in photo_rows:
        if (r.product_id, r.image_key) in seen:
            continue
        seen.add((r.product_id, r.image_key))
        by_product[r.product_id].append({"image_key": r.image_key, "thumb": thumb_url(r.image_key)})
    return {
        "products": [
            {"id": p.id, "sku": p.sku, "title": p.title, "photos": by_product[p.id]} for p in prods
        ]
    }


@router.post("/products/{product_id}/photos/attach", status_code=201)
@needs("products.photos")
def attach_photos(product_id: int, body: AttachPhotos, db: Session = Depends(get_db)):
    """
    Put pictures that are already in storage on this product, without uploading
    anything. Only keys that another photo row already uses are accepted, and a
    picture already on this product is skipped.
    """
    _require_product(db, product_id)
    keys = list(dict.fromkeys(k.strip() for k in body.image_keys if k and k.strip()))
    added = 0
    skipped = 0
    for k in keys:
        known = db.execute(text("select 1 from product_photos where image_key = :k limit 1"), {"k": k}).first()
        on_product = db.execute(
            text("select 1 from product_photos where product_id = :p and image_key = :k limit 1"),
            {"p": product_id, "k": k},
        ).first()
        if not known or on_product:
            skipped += 1
            continue
        next_pos = db.execute(
            text("select coalesce(max(sort_order), -1) + 1 from product_photos where product_id = :p"),
            {"p": product_id},
        ).scalar()
        db.execute(
            text(
                "insert into product_photos (product_id, image_key, storage_path, is_primary, sort_order) "
                "values (:p, :k, :sp, false, :o)"
            ),
            {"p": product_id, "k": k, "sp": photo_store.full_path(k), "o": next_pos},
        )
        added += 1
    _tidy(db, product_id)
    db.commit()
    return {**_listing(db, product_id), "added": added, "skipped": skipped}
