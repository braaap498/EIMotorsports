#!/usr/bin/env python3
"""
Download product photos from eBay's servers to a local folder.

Each photo is saved as  <dest>/<part-number>/<part-number>_<n>.<ext>  (n = the
photo's position on that product, 1 = primary) and product_photos.storage_path
is set to the relative path. The photo stays linked to its product in the
database by product_id regardless of the file name.

Safe to stop and re-run: photos that already have a storage_path are skipped,
and a file already on disk is recorded without downloading it again.

Setup (from the backend folder, venv active):
    pip install sqlalchemy psycopg2-binary      # likely already installed
    set DATABASE_URL=postgresql://user:pass@host:5432/postgres     (PowerShell: $env:DATABASE_URL="...")

Usage (from the backend folder):
    python scripts\\download_product_images.py --dest .\\product_images --estimate
    python scripts\\download_product_images.py --dest .\\product_images --dry-run
    python scripts\\download_product_images.py --dest .\\product_images --limit 20
    python scripts\\download_product_images.py --dest .\\product_images --workers 8

Options:
    --name-by F   Which product field names the folders and files:
                  sku (default), manufacturer_part_number, internal_part_number.
                  A blank value falls back to product-<id>; if two products share
                  a value, the product id is appended so files never collide.
    --estimate    Download a random sample WITHOUT saving it, then report the
                  average size, the image dimensions, how many photos could not
                  be fetched at the large size, and the projected total size.
    --size N      Ask eBay for the N-pixel version (default 1600, the largest
                  size eBay serves; 0 keeps the stored URL as-is). If that
                  fails, the stored URL is used and the photo is flagged.
    --sample N    Photos to sample for --estimate (default 60).
    --redo        Also re-download rows that already have a storage_path.
    --skip-db     Download only; do not write storage_path back.
Failures are written to <dest>/failed.csv, fallbacks to <dest>/fallbacks.csv.
"""
import argparse
import csv
import os
import random
import re
import statistics
import struct
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
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
NAME_FIELDS = {"sku": 4, "manufacturer_part_number": 5, "internal_part_number": 6}


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


def fetch_best(source_url: str, size: int):
    """Try the large size first. Returns (bytes, ctype, used_fallback)."""
    big = upgrade_size(source_url, size)
    try:
        data, ctype = fetch(big)
        return data, ctype, False
    except Exception:
        if big == source_url:
            raise
        data, ctype = fetch(source_url)  # large size missing: use the stored URL
        return data, ctype, True


def image_size(data: bytes):
    """(width, height) for JPEG and PNG, else None. No imaging library needed."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:2] == b"\xff\xd8":
            i = 2
            sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2 : i + 4])[0]
                if marker in sof:
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return w, h
                i += 2 + length
    except Exception:
        pass
    return None


def ext_for(ctype: str, url: str) -> str:
    if ctype in EXT_BY_TYPE:
        return EXT_BY_TYPE[ctype]
    m = re.search(r"\.(jpe?g|png|webp|gif)(?:\?|$)", url, re.I)
    return (m.group(1).lower().replace("jpeg", "jpg")) if m else "jpg"


def safe(value) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")[:80]


def existing_file(dest: Path, folder: str, stem: str):
    d = dest / folder
    if d.is_dir():
        for f in d.glob(f"{stem}.*"):
            if f.suffix != ".part" and f.stat().st_size > 0:
                return f
    return None


def process(dest: Path, job, size: int):
    """Download one photo. Returns (photo_id, relative_path, used_fallback)."""
    photo_id, _product_id, source_url, folder, pos = job
    stem = f"{folder}_{pos}"
    found = existing_file(dest, folder, stem)
    if found:
        return photo_id, found.relative_to(dest).as_posix(), False

    data, ctype, fallback = fetch_best(source_url, size)
    (dest / folder).mkdir(parents=True, exist_ok=True)
    target = dest / folder / f"{stem}.{ext_for(ctype, source_url)}"
    part = target.with_suffix(target.suffix + ".part")
    part.write_bytes(data)
    part.replace(target)
    return photo_id, target.relative_to(dest).as_posix(), fallback


def sample_one(job, size: int):
    """Fetch one photo for --estimate; nothing is written to disk."""
    data, _ctype, fallback = fetch_best(job[2], size)
    return len(data), image_size(data), fallback


def run_estimate(jobs, total, size, sample_n, workers):
    n = min(sample_n, len(jobs))
    sample = random.sample(jobs, n)
    print(f"Sampling {n} of {total} photos (nothing is saved)...")
    sizes, longs, fallbacks, errors = [], [], 0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(sample_one, j, size) for j in sample]):
            try:
                nbytes, dims, fb = fut.result()
            except Exception:
                errors += 1
                continue
            sizes.append(nbytes)
            fallbacks += int(fb)
            if dims:
                longs.append(max(dims))
    if not sizes:
        sys.exit("Could not download any sample photos.")
    avg = statistics.mean(sizes)
    mb = lambda b: b / (1024 * 1024)
    print(f"\nDownloaded {len(sizes)} samples, {errors} failed.")
    print(f"Average photo size: {avg / 1024:.0f} KB  (largest {max(sizes) / 1024:.0f} KB)")
    print(f"Projected total for {total} photos: {mb(avg * total) / 1024:.2f} GB  ({mb(avg * total):.0f} MB)")
    if longs:
        longs.sort()
        print(
            f"Longest side in pixels: min {longs[0]}, median {int(statistics.median(longs))}, max {longs[-1]}"
        )
        small = sum(1 for x in longs if x < 1000)
        print(f"Under 1000 px on the long side: {small} of {len(longs)}")
    print(f"Could not be fetched at the large size (fell back to the stored URL): {fallbacks} of {len(sizes)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="folder to save images into")
    ap.add_argument("--name-by", choices=sorted(NAME_FIELDS), default="sku")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--size", type=int, default=1600)
    ap.add_argument("--limit", type=int, default=0, help="only do the first N photos")
    ap.add_argument("--sample", type=int, default=60)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--skip-db", action="store_true")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="show what would be downloaded")
    args = ap.parse_args()

    dest = Path(args.dest).resolve()
    engine = create_engine(db_url())

    sql = """
        select ph.id, ph.product_id, ph.source_url, ph.storage_path,
               p.sku, p.manufacturer_part_number, p.internal_part_number,
               row_number() over (
                 partition by ph.product_id
                 order by ph.is_primary desc, ph.sort_order, ph.id) as pos
        from product_photos ph
        join products p on p.id = ph.product_id
        where ph.source_url is not null and ph.source_url <> ''
        order by ph.id
    """
    with engine.connect() as conn:
        rows = [tuple(r) for r in conn.execute(text(sql)).all()]

    # One folder name per product; duplicates get the product id appended.
    col = NAME_FIELDS[args.name_by]
    base = {r[1]: safe(r[col]) or f"product-{r[1]}" for r in rows}
    uses = Counter(base.values())
    folder_of = {pid: (b if uses[b] == 1 else f"{b}-{pid}") for pid, b in base.items()}

    jobs = [
        (r[0], r[1], r[2], folder_of[r[1]], r[7])
        for r in rows
        if args.redo or not (r[3] or "").strip()
    ]
    if args.limit:
        jobs = jobs[: args.limit]

    print(f"{len(jobs)} photos to process -> {dest}  (named by {args.name_by})")
    if args.estimate:
        if jobs:
            run_estimate(jobs, len(jobs), args.size, args.sample, args.workers)
        return
    if args.dry_run or not jobs:
        for j in jobs[:10]:
            print(f"  photo {j[0]} -> {j[3]}/{j[3]}_{j[4]}   {upgrade_size(j[2], args.size)}")
        if len(jobs) > 10:
            print(f"  ... and {len(jobs) - 10} more")
        return

    dest.mkdir(parents=True, exist_ok=True)
    done, failed, fallback_rows, pending_updates = 0, [], [], []

    def flush(conn):
        if pending_updates and not args.skip_db:
            conn.execute(
                text("update product_photos set storage_path = :p where id = :id"),
                [{"id": i, "p": p} for i, p in pending_updates],
            )
            conn.commit()
        pending_updates.clear()

    with engine.connect() as conn, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, dest, j, args.size): j for j in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                photo_id, rel, fb = fut.result()
                pending_updates.append((photo_id, rel))
                done += 1
                if fb:
                    fallback_rows.append((photo_id, job[1], job[2], rel))
            except Exception as e:
                failed.append((job[0], job[1], job[2], str(e)))
            total = done + len(failed)
            if total % 50 == 0:
                flush(conn)
                print(f"  {total}/{len(jobs)}  ok={done}  failed={len(failed)}")
        flush(conn)

    if failed:
        with open(dest / "failed.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["photo_id", "product_id", "source_url", "error"])
            w.writerows(failed)
    if fallback_rows:
        with open(dest / "fallbacks.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["photo_id", "product_id", "source_url", "saved_as"])
            w.writerows(fallback_rows)
    print(
        f"Finished. downloaded/recorded={done}  failed={len(failed)}  "
        f"not available at large size={len(fallback_rows)}"
    )


if __name__ == "__main__":
    main()
