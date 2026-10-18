#!/usr/bin/env python3
"""
Link every product_photos row to its shared image in the Supabase bucket.

For each row in mapping.csv (made by build_unique_images.py) this sets
product_photos.image_key, matching the row by its existing storage_path:

    original_path  (e.g. EBAY-165072584998/EBAY-165072584998_1.jpg)
        -> image_key  (e.g. 00/005c700f3c44ae05831d4dcd87a1b513.jpg)

Nothing else is changed: source_url, storage_path, is_primary and sort_order
are left exactly as they are, so this can be undone by clearing image_key.

SAFE BY DEFAULT: without --apply it only reads the database and reports what
it WOULD change. Re-running is harmless (rows already set are skipped).

Before --apply, the column must exist (see the ALTER TABLE step).

Setup (PowerShell, from the backend folder):
    $env:DATABASE_URL="postgresql://..."      (your real connection string)

Usage:
    python scripts\\apply_image_mapping.py --mapping .\\product_images_unique\\mapping.csv
    python scripts\\apply_image_mapping.py --mapping .\\product_images_unique\\mapping.csv --apply
"""
import argparse
import csv
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

BATCH = 2000


def db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("Set DATABASE_URL first (your Supabase Postgres connection string).")
    return url.replace("postgres://", "postgresql://", 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapping", required=True, help="path to mapping.csv")
    ap.add_argument("--apply", action="store_true", help="actually write image_key (default: report only)")
    args = ap.parse_args()

    path = Path(args.mapping)
    if not path.is_file():
        sys.exit(f"Not found: {path}")
    with open(path, newline="", encoding="utf-8") as f:
        mapping = {r["original_path"]: r["unique_path"] for r in csv.DictReader(f)}
    print(f"{len(mapping)} paths in mapping.csv")

    engine = create_engine(db_url())
    with engine.connect() as conn:
        try:
            rows = conn.execute(text("select id, storage_path, image_key from product_photos")).all()
        except Exception as e:
            sys.exit(f"Could not read product_photos.image_key - has the column been added yet?\n{e}")

        todo, already, unmatched = [], 0, 0
        for r in rows:
            key = mapping.get(r.storage_path)
            if key is None:
                unmatched += 1
            elif r.image_key == key:
                already += 1
            else:
                todo.append({"id": r.id, "k": key})

        print(f"product_photos rows:          {len(rows)}")
        print(f"  already linked:             {already}")
        print(f"  to link now:                {len(todo)}")
        print(f"  no match in mapping.csv:    {unmatched}")

        if not args.apply:
            print("\nDry run - nothing was changed. Add --apply to write image_key.")
            return

        for i in range(0, len(todo), BATCH):
            conn.execute(text("update product_photos set image_key = :k where id = :id"), todo[i : i + BATCH])
            conn.commit()
            print(f"  updated {min(i + BATCH, len(todo))}/{len(todo)}")
        print(f"\nDone. image_key set on {len(todo)} rows.")


if __name__ == "__main__":
    main()
