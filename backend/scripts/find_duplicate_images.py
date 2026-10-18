#!/usr/bin/env python3
"""
Report exact duplicate photos in the downloaded product_images folder.

READ-ONLY: it never deletes, moves, renames or edits any photo and does not
touch the database. The only file it writes is the CSV report you ask for
(default: duplicate_report.csv in the folder you run it from).

Exact duplicates = files with identical bytes. Files are compared by size
first, and only files that share a size are hashed (SHA-256), so it is fast.

Usage (from the backend folder):
    python scripts\\find_duplicate_images.py --dest .\\product_images
    python scripts\\find_duplicate_images.py --dest .\\product_images --report .\\duplicate_report.csv --workers 8

Report columns (one row per duplicate file, not counting the copy to keep):
    group, keep_path, duplicate_path, bytes, same_product
"""
import argparse
import csv
import hashlib
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SKIP_SUFFIXES = {".csv", ".part"}


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
    ap.add_argument("--dest", required=True, help="the product_images folder")
    ap.add_argument("--report", default="duplicate_report.csv", help="CSV file to write")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    root = Path(args.dest).resolve()
    if not root.is_dir():
        sys.exit(f"Folder not found: {root}")

    print(f"Scanning {root} ...")
    by_size = defaultdict(list)
    total_files, total_bytes = 0, 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            if p.suffix.lower() in SKIP_SUFFIXES:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            by_size[size].append(p)
            total_files += 1
            total_bytes += size
    print(f"{total_files} photos, {human(total_bytes)} total")

    # Only files that share a size can be identical.
    candidates = [p for paths in by_size.values() if len(paths) > 1 for p in paths]
    print(f"{len(candidates)} photos share a size with another photo; hashing those ...")

    hashes = {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for p, digest in zip(candidates, pool.map(sha256_of, candidates)):
            hashes[p] = digest
            done += 1
            if done % 2000 == 0:
                print(f"  hashed {done}/{len(candidates)}")

    groups = defaultdict(list)
    for p, digest in hashes.items():
        groups[(p.stat().st_size, digest)].append(p)
    dup_groups = [sorted(v) for v in groups.values() if len(v) > 1]
    dup_groups.sort(key=lambda g: (-len(g), str(g[0])))

    def folder(p: Path) -> str:
        return p.relative_to(root).parts[0]

    extra_files = extra_bytes = 0
    same_product_files = same_product_bytes = 0
    cross_product_files = cross_product_bytes = 0
    rows = []
    for gi, g in enumerate(dup_groups, 1):
        keep = g[0]
        for dup in g[1:]:
            size = dup.stat().st_size
            same = folder(dup) == folder(keep)
            extra_files += 1
            extra_bytes += size
            if same:
                same_product_files += 1
                same_product_bytes += size
            else:
                cross_product_files += 1
                cross_product_bytes += size
            rows.append([
                gi,
                keep.relative_to(root).as_posix(),
                dup.relative_to(root).as_posix(),
                size,
                "yes" if same else "no",
            ])

    with open(args.report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["group", "keep_path", "duplicate_path", "bytes", "same_product"])
        w.writerows(rows)

    print("\n=== Exact duplicate report (nothing was changed) ===")
    print(f"Photos scanned:              {total_files}  ({human(total_bytes)})")
    print(f"Groups of identical photos:  {len(dup_groups)}")
    print(f"Extra copies (removable):    {extra_files}  ({human(extra_bytes)})")
    print(f"  within the same product:   {same_product_files}  ({human(same_product_bytes)})")
    print(f"  across different products: {cross_product_files}  ({human(cross_product_bytes)})")
    if total_bytes:
        print(f"Size after removing extras:  {human(total_bytes - extra_bytes)}")
    print(f"Report written to: {Path(args.report).resolve()}")


if __name__ == "__main__":
    main()
