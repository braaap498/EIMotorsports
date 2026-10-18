#!/usr/bin/env python3
"""
Upload the de-duplicated photos (made by build_unique_images.py) to a Supabase
Storage bucket, with a small thumbnail of each.

Reads only the --src folder. Writes nothing to your database and nothing on
disk. Safe to stop and re-run: files already in the bucket are skipped.

Bucket layout (names come from the image content, so they never change):
    full/ab/ab12cd34....jpg      the full-size photo
    thumb/ab/ab12cd34....webp    a thumbnail (400 px on the long side)

Setup (PowerShell, from the backend folder):
    pip install pillow
    $env:SUPABASE_SERVICE_KEY="paste-your-service-role-key"

The service role key gives full access to your project. Never put it in a
file, never commit it, never paste it into a chat.

Usage:
    python scripts\\upload_unique_images.py --src .\\product_images_unique --dry-run
    python scripts\\upload_unique_images.py --src .\\product_images_unique --limit 20
    python scripts\\upload_unique_images.py --src .\\product_images_unique --workers 6

Options:
    --url         Supabase project URL (default: the E&I project)
    --bucket      bucket name (default: product-images; must already exist)
    --thumb-size  long side of thumbnails in pixels (default 400)
    --no-thumbs   upload the full-size photos only
    --limit N     only the first N images (for a trial)
    --dry-run     show what would be uploaded, upload nothing
"""
import argparse
import io
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_URL = "https://ezaqtxicodbcnnirwdfa.supabase.co"
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
CACHE = "public, max-age=31536000, immutable"  # names are content hashes


def upload(base: str, bucket: str, key: str, object_path: str, data: bytes, ctype: str, retries: int = 4) -> str:
    """Return 'uploaded' or 'exists'. Raises on any other failure."""
    url = f"{base}/storage/v1/object/{bucket}/{object_path}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {key}",
                    "apikey": key,
                    "Content-Type": ctype,
                    "Cache-Control": CACHE,
                    "x-upsert": "false",
                },
            )
            with urllib.request.urlopen(req, timeout=60):
                return "uploaded"
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 409 or "Duplicate" in body or "already exists" in body:
                return "exists"
            last = RuntimeError(f"HTTP {e.code}: {body[:200]}")
            if e.code in (400, 401, 403, 404):  # retrying will not help
                break
        except Exception as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise last


def make_thumb(path: Path, size: int) -> bytes:
    from PIL import Image  # imported here so --no-thumbs works without Pillow

    with Image.open(path) as im:
        im = im.convert("RGBA") if im.mode in ("P", "LA") else im
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=80)
        return buf.getvalue()


def process(base, bucket, key, src: Path, path: Path, thumb_size: int, thumbs: bool):
    rel = path.relative_to(src).as_posix()  # ab/ab12....jpg
    stem = rel.rsplit(".", 1)[0]
    results = [upload(base, bucket, key, f"full/{rel}", path.read_bytes(),
                      CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))]
    if thumbs:
        results.append(upload(base, bucket, key, f"thumb/{stem}.webp", make_thumb(path, thumb_size), "image/webp"))
    return rel, results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="the product_images_unique folder")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--bucket", default="product-images")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--thumb-size", type=int, default=400)
    ap.add_argument("--no-thumbs", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.src).resolve()
    if not src.is_dir():
        sys.exit(f"Folder not found: {src}")
    files = sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() in CONTENT_TYPES)
    if args.limit:
        files = files[: args.limit]
    base = args.url.rstrip("/")
    thumbs = not args.no_thumbs
    print(f"{len(files)} images -> {base} bucket '{args.bucket}'  (thumbnails: {'yes' if thumbs else 'no'})")

    if args.dry_run or not files:
        for p in files[:10]:
            print(f"  would upload full/{p.relative_to(src).as_posix()}")
        if len(files) > 10:
            print(f"  ... and {len(files) - 10} more")
        return

    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        sys.exit("Set SUPABASE_SERVICE_KEY first (Supabase > Project Settings > API > service_role key).")
    if thumbs:
        try:
            import PIL  # noqa: F401
        except ImportError:
            sys.exit("Thumbnails need Pillow: pip install pillow   (or run with --no-thumbs)")

    uploaded = existed = 0
    failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process, base, args.bucket, key, src, p, args.thumb_size, thumbs): p for p in files}
        for i, fut in enumerate(as_completed(futs), 1):
            p = futs[fut]
            try:
                _rel, results = fut.result()
                uploaded += results.count("uploaded")
                existed += results.count("exists")
            except Exception as e:
                failed.append((p.name, str(e)))
            if i % 100 == 0:
                print(f"  {i}/{len(files)}  uploaded={uploaded}  already there={existed}  failed={len(failed)}")

    print(f"\nFinished. files uploaded={uploaded}  already in bucket={existed}  failed={len(failed)}")
    for name, err in failed[:10]:
        print(f"  FAILED {name}: {err}")
    if failed:
        print("Re-run the same command to retry the failures.")
    elif files:
        first = files[0].relative_to(src).as_posix()
        print(f"Check one in your browser:\n  {base}/storage/v1/object/public/{args.bucket}/full/{first}")


if __name__ == "__main__":
    main()
