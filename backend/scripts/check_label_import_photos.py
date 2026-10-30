#!/usr/bin/env python3
"""
REPORT ONLY. Writes nothing to the database. Only writes CSV files + a progress file.

For listings in ebay_label_import that are NOT in products yet:
  1. If another listing with the same custom label IS in products and that
     product has photos in storage, the new listing reuses them (no eBay call).
     -> reuse_plan.csv
  2. Everything else (no sibling product, or sibling has no stored photos) needs
     eBay. Per custom label, listings are tried one at a time until eBay (Browse
     API) can read one; remaining listings with that label are skipped.
     -> label_photos.csv      (label, item_number, source_url)
     -> label_lookup.csv      (one row per label: which item worked, or none)

Run from the backend folder (same env vars as the other scripts):
    python scripts\\check_label_import_photos.py --limit 10     (trial: 10 labels)
    python scripts\\check_label_import_photos.py                (all, up to --max-calls)

Progress is saved in --state, so a rerun carries on after the daily eBay limit.
Needs check_ebay_photos_new_listings_only.py in the same folder.
"""
import argparse, csv, json, os, sys, time
from collections import defaultdict

from sqlalchemy import create_engine, text

from check_ebay_photos_new_listings_only import (
    Ebay, RateLimited, clean_item_id, db_url, env_or_dotenv, load_state, save_state,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="only look up N labels (trial)")
    ap.add_argument("--max-calls", type=int, default=4500)
    ap.add_argument("--state", default="label_photo_state.json")
    args = ap.parse_args()

    cid, secret = env_or_dotenv("EBAY_CLIENT_ID"), env_or_dotenv("EBAY_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not found.")

    engine = create_engine(db_url())
    with engine.connect() as conn:
        imp = [(str(r[0]).strip(), (r[1] or "").strip()) for r in conn.execute(text(
            "select item_number::text, custom_label_sku::text from ebay_label_import"))]
        prod = {}  # clean item id -> product id
        for pid, eid in conn.execute(text(
                "select id, ebay_item_id from products where ebay_item_id is not null and ebay_item_id <> ''")):
            c = clean_item_id(eid)
            if c:
                prod[c] = pid
        stored = {r[0]: r[1] for r in conn.execute(text(
            "select product_id, count(*) from product_photos "
            "where storage_path is not null and storage_path <> '' group by product_id"))}

    # label -> best sibling product (most stored photos)
    best = {}
    for item, label in imp:
        pid = prod.get(item)
        if pid is None or not label:
            continue
        n = stored.get(pid, 0)
        if label not in best or n > best[label][1]:
            best[label] = (pid, n, item)

    reuse, need = [], defaultdict(list)  # need: key -> [item numbers]
    for item, label in imp:
        if item in prod:
            continue
        sib = best.get(label)
        if sib and sib[1] > 0:
            reuse.append((item, label, sib[0], sib[2], sib[1]))
        else:
            need[label or f"item:{item}"].append(item)

    with open("reuse_plan.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item_number", "custom_label", "source_product_id", "source_ebay_item_id", "stored_photo_count"])
        w.writerows(reuse)
    print(f"Not in products: {len(reuse) + sum(len(v) for v in need.values())}")
    print(f"  reuse sibling photos (no eBay call): {len(reuse)}  -> reuse_plan.csv")
    print(f"  need eBay: {sum(len(v) for v in need.values())} listings across {len(need)} labels")

    state = load_state(args.state)  # key -> {"item": ..., "urls": [...]} or {"item": None}
    todo = [k for k in need if k not in state]
    cap = args.limit if args.limit else len(todo)
    todo = todo[:cap]
    print(f"Looking up {len(todo)} label(s) this run ({len(need) - len(state)} not done yet).")

    ebay, calls = Ebay(cid, secret), 0
    try:
        for n, key in enumerate(todo, 1):
            hit = {"item": None, "urls": [], "tried": 0}
            for item in need[key]:
                if calls >= args.max_calls:
                    raise StopIteration
                try:
                    urls = ebay.photos(item)
                except RateLimited:
                    print("\neBay rate limit still hit; stopping. Run again later.")
                    raise StopIteration
                except Exception as e:
                    print(f"  error on {item}: {e}")
                    continue
                calls += 1
                hit["tried"] += 1
                time.sleep(0.1)
                if urls:  # readable and has photos
                    hit.update(item=item, urls=urls)
                    break
            state[key] = hit
            if n % 25 == 0:
                save_state(args.state, state)
                print(f"  {n}/{len(todo)} labels, {calls} eBay calls so far")
    except StopIteration:
        pass
    finally:
        save_state(args.state, state)

    ok = [k for k in need if state.get(k, {}).get("item")]
    bad = [k for k in need if k in state and not state[k].get("item")]
    with open("label_lookup.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["label", "listings_with_label", "result", "item_used", "listings_tried", "photo_count"])
        for k in need:
            s = state.get(k)
            if s is None:
                w.writerow([k, len(need[k]), "not checked yet", "", "", ""])
            else:
                w.writerow([k, len(need[k]), "photos found" if s["item"] else "none readable",
                            s["item"] or "", s["tried"] if "tried" in s else "", len(s["urls"])])
    with open("label_photos.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["label", "item_number", "source_url"])
        for k in ok:
            for u in state[k]["urls"]:
                w.writerow([k, state[k]["item"], u])
    print(f"\neBay calls this run: {calls}")
    print(f"Labels with photos: {len(ok)}   none readable: {len(bad)}   not checked yet: {len(need) - len(ok) - len(bad)}")
    print("Wrote reuse_plan.csv, label_lookup.csv, label_photos.csv. Database untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
