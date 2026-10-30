#!/usr/bin/env python3
"""
Adds UNLINKED photo rows (product_id empty) to product_photos for listings in
ebay_label_import that are not in products yet. Inserts only: no deletes, no updates.

Reads the files written by check_label_import_photos.py (run from the same folder):
    reuse_plan.csv    -> copy a sibling product's photos (same image_key, no download)
    label_photos.csv  -> download once per label, store, then share with every listing of that label

Each new row gets storage_path = EBAY-<itemid>/EBAY-<itemid>_<n>.jpg, so the item id can be
read back from the path when the product row appears.

    python scripts\\apply_label_photos.py                 (dry run: writes nothing)
    python scripts\\apply_label_photos.py --apply --limit-labels 5   (small real trial)
    python scripts\\apply_label_photos.py --apply

Needs product_photos.product_id to allow empty values; --apply stops if it does not.
Run from the backend folder so "app" can be imported (same as your other scripts).
"""
import argparse, csv, json, os, sys, urllib.request
from collections import defaultdict

from sqlalchemy import create_engine, text

from check_ebay_photos_new_listings_only import clean_item_id, db_url

CACHE = "label_keys.json"  # label -> [image_key, ...]  (so a rerun does not re-download)


def path_for(item: str, n: int) -> str:
    return f"EBAY-{item}/EBAY-{item}_{n}.jpg"


def read_csv(name):
    with open(name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually download/upload/insert")
    ap.add_argument("--limit-labels", type=int, default=0, help="trial: only the first N labels")
    ap.add_argument("--limit-reuse", type=int, default=0, help="trial: only the first N reuse listings")
    args = ap.parse_args()

    engine = create_engine(db_url())
    with engine.connect() as conn:
        nullable = conn.execute(text(
            "select is_nullable from information_schema.columns "
            "where table_name='product_photos' and column_name='product_id'")).scalar()
        if args.apply and nullable != "YES":
            sys.exit("product_photos.product_id does not allow empty values yet. Nothing was changed.")

        done = {r[0].replace("EBAY-", "", 1) for r in conn.execute(text(
            "select distinct split_part(storage_path,'/',1) from product_photos "
            "where product_id is null and storage_path like 'EBAY-%'"))}
        in_products = set()
        for (eid,) in conn.execute(text("select ebay_item_id from products where ebay_item_id is not null")):
            c = clean_item_id(eid)
            if c:
                in_products.add(c)
        label_items = defaultdict(list)  # label -> item numbers not in products
        for item, label in conn.execute(text("select item_number::text, custom_label_sku::text from ebay_label_import")):
            item = str(item).strip()
            if item not in in_products and item not in done:
                label_items[(label or "").strip() or f"item:{item}"].append(item)

        # ---------- group 1: reuse sibling photos
        reuse = [r for r in read_csv("reuse_plan.csv") if r["item_number"] not in done]
        if args.limit_reuse:
            reuse = reuse[: args.limit_reuse]
        src_ids = sorted({int(r["source_product_id"]) for r in reuse})
        sib = defaultdict(list)
        if src_ids:
            for pid, key in conn.execute(text(
                    "select product_id, image_key from product_photos "
                    "where product_id = any(cast(:ids as bigint[])) and image_key is not null "
                    "order by product_id, is_primary desc, sort_order, id"), {"ids": src_ids}):
                if key not in sib[pid]:
                    sib[pid].append(key)

    reuse_rows = []
    for r in reuse:
        for n, key in enumerate(sib.get(int(r["source_product_id"]), []), 1):
            reuse_rows.append((path_for(r["item_number"], n), key, n == 1, n - 1))
    # ---------- group 2: labels needing a download
    lab = defaultdict(list)
    for r in read_csv("label_photos.csv"):
        lab[r["label"]].append(r["source_url"])
    labels = [k for k in lab if label_items.get(k)]
    if args.limit_labels:
        labels = labels[: args.limit_labels]
    n_label_items = sum(len(label_items[k]) for k in labels)

    print(f"Reuse listings: {len(reuse)}  -> {len(reuse_rows)} rows to add")
    print(f"Labels to download: {len(labels)} covering {n_label_items} listings "
          f"({sum(len(lab[k]) for k in labels)} photos to fetch)")
    print(f"Already done earlier (skipped): {len(done)} listings")
    if not args.apply:
        print("\nDRY RUN: nothing was downloaded, stored or written. Add --apply to do it.")
        return 0

    from app import photo_store  # your existing processing + storage upload

    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    added = 0
    with engine.begin() as conn:  # reuse group: one transaction
        for item_path, key, primary, order in reuse_rows:
            conn.execute(text(
                "insert into product_photos (product_id, image_key, storage_path, is_primary, sort_order) "
                "values (null, :k, :sp, :pr, :o)"), {"k": key, "sp": item_path, "pr": primary, "o": order})
            added += 1
    print(f"Reuse rows added: {added}")

    bad = []
    for i, k in enumerate(labels, 1):
        if k not in cache:
            keys = []
            for url in lab[k]:
                try:
                    p = photo_store.process_image(download(url))
                    photo_store.upload(p)
                    if p.key not in keys:
                        keys.append(p.key)
                except Exception as e:
                    bad.append((k, url, str(e)[:120]))
            cache[k] = keys
            json.dump(cache, open(CACHE, "w"))
        if not cache[k]:
            continue
        with engine.begin() as conn:  # one transaction per label
            for item in label_items[k]:
                for n, key in enumerate(cache[k], 1):
                    conn.execute(text(
                        "insert into product_photos (product_id, image_key, storage_path, is_primary, sort_order) "
                        "values (null, :k, :sp, :pr, :o)"),
                        {"k": key, "sp": path_for(item, n), "pr": n == 1, "o": n - 1})
                    added += 1
        if i % 10 == 0:
            print(f"  {i}/{len(labels)} labels")
    if bad:
        with open("apply_failures.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["label", "url", "error"]); w.writerows(bad)
    print(f"\nTotal rows added: {added}. Photos that failed: {len(bad)}"
          + (" (see apply_failures.csv)" if bad else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
