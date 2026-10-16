#!/usr/bin/env python3
"""
Download product photos from eBay's servers to a local folder.

For every product_photos row that has a source_url but no storage_path, this
downloads the image to  <dest>/<product_id>/<photo_id>.<ext>  and then sets
product_photos.storage_path to  "<product_id>/<photo_id>.<ext>".

Safe to stop and re-run: rows that already have a storage_path are skipped, and
a file that already exists on disk is recorded without downloading it again.

Setup (from the backend folder, with the venv active):
    pip install sqlalchemy psycopg2-binary      # likely already installed
    export DATABASE_URL="postgresql://user:pass@host:5432/postgres"

Usage:
    python download_product_images.py --dest ./product_images --dry-run
    python download_product_images.py --dest ./product_images --limit 20
    python download_product_images.py --dest ./product_images --workers 8

Options:
    --size N      Ask eBay for the N-pixel version (default 1600; 0 keeps the
                  stored URL as-is). Falls back to the stored URL if the
                  larger one is not available.
    --redo        Also re-download rows that already have a storage_path.
    --skip-db     Download only; do not write storage_path back.
Failures are written to <dest>/failed.csv.
"""
import argparse
import csv
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import create_engine, text

USER_AGENT = "Mozilla/5.0 (compatible; EIMotorsportsImageSync/1.0)"
EXT_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("Set DATABASE_URL first (your Supabase Postgres connection string).")
    return url.replace("postgres://", "postgresql://", 1)


def upgrade_size(url: str, size: int) -> str:
    """eBay image URLs look like .../s-l500.jpg ; swap in a larger size."""
    if size and "ebayimg.com" in url:
        return re.sub(r"/s-l\d+\.", f"/s-l{size}.", url)
    return url


def fetch(url: str, retries: int = 3, timeout: int = 30):
    """Return (bytes, content_type). Raises on failure."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                data = resp.read()
            if not ctype.startswith("image/"):
                raise ValueError(f"not an image ({ctype or 'no content-type'})")
            if len(data) < 200:
                raise ValueError("image is empty or too small")
            return data, ctype
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 403, 404, 410):  # retrying will not help
                break
        except Exception as e:  # network hiccup, timeout, bad content
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise last


def ext_for(ctype: str, url: str) -> str:
    if ctype in EXT_BY_TYPE:
        return EXT_BY_TYPE[ctype]
    m = re.search(r"\.(jpe?g|png|webp|gif)(?:\?|$)", url, re.I)
    return (m.group(1).lower().replace("jpeg", "jpg")) if m else "jpg"


def existing_file(dest: Path, product_id: int, photo_id: int):
    folder = dest / str(product_id)
    if folder.is_dir():
        for f in folder.glob(f"{photo_id}.*"):
            if f.suffix != ".part" and f.stat().st_size > 0:
                return f
    return None


def process(dest: Path, row, size: int):
    """Download one photo. Returns (photo_id, relative_path)."""
    photo_id, product_id, source_url = row
    found = existing_file(dest, product_id, photo_id)
    if found:
        return photo_id, found.relative_to(dest).as_posix()

    big = upgrade_size(source_url, size)
    try:
        data, ctype = fetch(big)
    except Exception:
        if big == source_url:
            raise
        data, ctype = fetch(source_url)  # larger size missing: use the original

    folder = dest / str(product_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{photo_id}.{ext_for(ctype, source_url)}"
    part = target.with_suffix(target.suffix + ".part")
    part.write_bytes(data)
    part.replace(target)
    return photo_id, target.relative_to(dest).as_posix()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="folder to save images into")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--size", type=int, default=1600)
    ap.add_argument("--limit", type=int, default=0, help="only do the first N photos")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--skip-db", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="show what would be downloaded")
    args = ap.parse_args()

    dest = Path(args.dest).resolve()
    engine = create_engine(db_url())

    where = "source_url is not null and source_url <> ''"
    if not args.redo:
        where += " and (storage_path is null or storage_path = '')"
    sql = f"select id, product_id, source_url from product_photos where {where} order by id"
    if args.limit:
        sql += f" limit {int(args.limit)}"
    with engine.connect() as conn:
        rows = [tuple(r) for r in conn.execute(text(sql)).all()]

    print(f"{len(rows)} photos to process -> {dest}")
    if args.dry_run or not rows:
        for r in rows[:10]:
            print(f"  photo {r[0]} (product {r[1]}): {upgrade_size(r[2], args.size)}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
        return

    dest.mkdir(parents=True, exist_ok=True)
    done, failed, pending_updates = 0, [], []

    def flush(conn):
        if pending_updates and not args.skip_db:
            conn.execute(
                text("update product_photos set storage_path = :p where id = :id"),
                [{"id": i, "p": p} for i, p in pending_updates],
            )
            conn.commit()
        pending_updates.clear()

    with engine.connect() as conn, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, dest, r, args.size): r for r in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                photo_id, rel = fut.result()
                pending_updates.append((photo_id, rel))
                done += 1
            except Exception as e:
                failed.append((row[0], row[1], row[2], str(e)))
            total = done + len(failed)
            if total % 50 == 0:
                flush(conn)
                print(f"  {total}/{len(rows)}  ok={done}  failed={len(failed)}")
        flush(conn)

    if failed:
        with open(dest / "failed.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["photo_id", "product_id", "source_url", "error"])
            w.writerows(failed)
    print(f"Finished. downloaded/recorded={done}  failed={len(failed)}"
          + (f"  (see {dest / 'failed.csv'})" if failed else ""))


if __name__ == "__main__":
    main()
