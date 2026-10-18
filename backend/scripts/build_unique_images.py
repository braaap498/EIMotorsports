#!/usr/bin/env python3
"""
Build a de-duplicated copy of the downloaded product photos.

NON-DESTRUCTIVE: your product_images folder is only READ. Nothing is deleted,
moved or renamed there, and the database is not touched. The script COPIES one
file for each distinct image into a new folder, named by its content hash:

    <out>/ab/ab12cd34....jpg        (first 2 characters of the hash = subfolder)

It also writes a mapping file that links every original photo path (the value
stored in product_photos.storage_path) to its de-duplicated file, so the
database can be pointed at the shared copies later:

    <out>/mapping.csv   columns: original_path, sha256, unique_path

Safe to stop and re-run: files already copied are skipped.

Usage (from the backend folder):
    python scripts\\build_unique_images.py --dest .\\product_images --out .\\product_images_unique
    python scripts\\build_unique_images.py --dest .\\product_images --out .\\product_images_unique --workers 8
"""
import argparse
import csv
import hashlib
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SKIP_SUFFIXES = {".csv", ".part"}
HASH_CHARS = 32  # 128 bits, plenty to avoid collisions


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="the downloaded product_images folder (read only)")
    ap.add_argument("--out", required=True, help="new folder for the de-duplicated copies")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    src = Path(args.dest).resolve()
    out = Path(args.out).resolve()
    if not src.is_dir():
        sys.exit(f"Folder not found: {src}")
    if out == src or src in out.parents or out in src.parents:
        sys.exit("--out must be a separate folder, not inside (or containing) --dest.")

    files = []
    for dirpath, _dirs, names in os.walk(src):
        for name in names:
            p = Path(dirpath) / name
            if p.suffix.lower() not in SKIP_SUFFIXES:
                files.append(p)
    files.sort()
    print(f"{len(files)} photos found in {src}. Hashing ...")

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (p, digest) in enumerate(zip(files, pool.map(sha256_of, files)), 1):
            rows.append((p, digest))
            if i % 5000 == 0:
                print(f"  hashed {i}/{len(files)}")

    out.mkdir(parents=True, exist_ok=True)
    first_by_hash = {}
    copied = skipped = 0
    unique_bytes = total_bytes = 0
    mapping = []
    for p, digest in rows:
        key = digest[:HASH_CHARS]
        size = p.stat().st_size
        total_bytes += size
        ext = p.suffix.lower() or ".jpg"
        rel_unique = f"{key[:2]}/{key}{ext}"
        if key not in first_by_hash:
            first_by_hash[key] = rel_unique
            unique_bytes += size
            target = out / rel_unique
            if target.exists() and target.stat().st_size == size:
                skipped += 1
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(target.suffix + ".part")
                shutil.copyfile(p, tmp)
                tmp.replace(target)
                copied += 1
        mapping.append([p.relative_to(src).as_posix(), digest, first_by_hash[key]])

    with open(out / "mapping.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["original_path", "sha256", "unique_path"])
        w.writerows(mapping)

    print("\n=== De-duplicated copy built (originals untouched) ===")
    print(f"Original photos:     {len(files)}  ({human(total_bytes)})")
    print(f"Distinct images:     {len(first_by_hash)}  ({human(unique_bytes)})")
    print(f"Copied this run:     {copied}   already present: {skipped}")
    print(f"Mapping file:        {out / 'mapping.csv'}")


if __name__ == "__main__":
    main()
