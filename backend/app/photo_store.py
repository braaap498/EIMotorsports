"""
Turns an uploaded picture into the two files the store uses, and puts them in
(or removes them from) the Supabase Storage bucket.

Bucket layout (same as the existing photos, see app/images.py):

    full/<key>                    JPEG, longest side at most 2000 px
    thumb/<key without ext>.webp  WEBP, longest side at most 400 px

<key> is "<first 2 hex of hash>/<hash>.jpg", where the hash is taken from the
finished full-size file. The same picture therefore always gets the same key, so
uploading it twice never creates two copies in storage.

Needs the Pillow package. The storage calls use the server-only
SUPABASE_SERVICE_KEY (the same one the account screens already use).
"""
import hashlib
import io
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from PIL import Image, ImageOps

from app.config import get_settings

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 60_000_000  # guards against "decompression bomb" files
FULL_MAX_PX = 2000
THUMB_MAX_PX = 400
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class PhotoError(Exception):
    """A problem the person can fix (wrong file type, too big, ...). Safe to show."""


class StorageError(Exception):
    """The storage service refused or could not be reached."""


@dataclass
class Processed:
    key: str
    full: bytes
    thumb: bytes


def process_image(raw: bytes) -> Processed:
    if not raw:
        raise PhotoError("The file was empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise PhotoError("That photo is too large (the limit is 15 MB).")
    try:
        img = Image.open(io.BytesIO(raw))
        fmt = img.format
        img.load()
    except Exception:
        raise PhotoError("That file could not be read as a picture. Use a JPG, PNG or WEBP.")
    if fmt not in ALLOWED_FORMATS:
        raise PhotoError("Only JPG, PNG and WEBP pictures can be uploaded.")

    img = ImageOps.exif_transpose(img)  # phone photos: turn the right way up
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))  # transparency -> white
        flat.paste(rgba, mask=rgba.split()[-1])
        img = flat
    else:
        img = img.convert("RGB")

    full_img = img.copy()
    full_img.thumbnail((FULL_MAX_PX, FULL_MAX_PX), Image.LANCZOS)  # only ever shrinks
    full_buf = io.BytesIO()
    full_img.save(full_buf, "JPEG", quality=85, optimize=True, progressive=True)
    full = full_buf.getvalue()

    thumb_img = img.copy()
    thumb_img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.LANCZOS)
    thumb_buf = io.BytesIO()
    thumb_img.save(thumb_buf, "WEBP", quality=80, method=4)

    digest = hashlib.sha1(full).hexdigest()
    return Processed(key=f"{digest[:2]}/{digest}.jpg", full=full, thumb=thumb_buf.getvalue())


def full_path(key: str) -> str:
    return f"full/{key}"


def thumb_path(key: str) -> str:
    return f"thumb/{key.rsplit('.', 1)[0]}.webp"


# ------------------------------------------------------------------ storage
def _bucket() -> str:
    # image_base_url ends in ".../object/public/<bucket>"
    return get_settings().image_base_url.rstrip("/").rsplit("/", 1)[-1]


def _auth() -> tuple[str, dict]:
    s = get_settings()
    key = getattr(s, "supabase_service_key", None)
    if not key:
        raise StorageError("Photo storage is not set up: add SUPABASE_SERVICE_KEY to the API's environment settings.")
    return s.supabase_url.rstrip("/"), {"apikey": key, "Authorization": f"Bearer {key}"}


def _send(req: urllib.request.Request) -> None:
    try:
        with urllib.request.urlopen(req, timeout=30):
            return
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        log.error("storage %s %s -> %s %s", req.get_method(), req.full_url, e.code, detail)
        raise StorageError("The photo storage service rejected the request.")
    except (urllib.error.URLError, TimeoutError) as e:
        log.error("storage unreachable: %s", e)
        raise StorageError("Could not reach the photo storage service. Try again.")


def _put(path: str, data: bytes, content_type: str) -> None:
    base, headers = _auth()
    url = f"{base}/storage/v1/object/{_bucket()}/{urllib.parse.quote(path)}"
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            **headers,
            "Content-Type": content_type,
            "x-upsert": "true",  # same picture = same key, so overwriting is harmless
            "cache-control": "max-age=31536000",
        },
    )
    _send(req)


def upload(p: Processed) -> None:
    _put(full_path(p.key), p.full, "image/jpeg")
    _put(thumb_path(p.key), p.thumb, "image/webp")


def remove(keys: list[str]) -> None:
    """Delete the stored files for these keys. Never raises: a leftover file is harmless."""
    if not keys:
        return
    try:
        base, headers = _auth()
        paths = [p for k in keys for p in (full_path(k), thumb_path(k))]
        req = urllib.request.Request(
            f"{base}/storage/v1/object/{_bucket()}",
            data=json.dumps({"prefixes": paths}).encode(),
            method="DELETE",
            headers={**headers, "Content-Type": "application/json"},
        )
        _send(req)
    except StorageError:
        log.warning("could not delete stored files for %s", keys)
