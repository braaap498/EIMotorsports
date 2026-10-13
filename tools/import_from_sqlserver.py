#!/usr/bin/env python3
"""
One-time import: legacy E&I desktop app (SQL Server) -> new Postgres/Supabase.

Run it on the Windows machine that can reach the old SQL Server database.

    python import_from_sqlserver.py                 # DRY RUN: loads everything,
                                                    #   prints a report, saves nothing
    python import_from_sqlserver.py --commit        # same, but actually saves

The destination comes from DATABASE_URL (same value the API uses). The source
defaults to the old app's settings (.\\SQLEXPRESS / EIMotorsportsInventory /
Windows auth); override with --mssql or the SOURCE_MSSQL env var.

What it does
  * Keeps the original ids for categories, manufacturers, products, inventory
    history, vendors, purchase orders and sales, so history stays linked.
  * Rebuilds vehicle fitment in the new normalized form (vehicles +
    product_fitment) from BOTH the old ProductCompatibility rows and the old
    "model + year range" fields on Products.
  * Cleans listing descriptions (strips scripts/styles/unsafe markup) because
    they are public web content now.
  * Leaves everything UNPUBLISHED unless you pass --publish-active.
  * Keeps eBay identifiers as BACKGROUND cross-references only, never as core
    data: eBay item numbers -> channel_listings, eBay Store category ids ->
    channel_category_refs, seller name / category ids -> integration_settings.
    Nothing on the website depends on them.
  * Does NOT bring over eBay login tokens (they are encrypted to the old PC;
    reconnect later) or QuickBooks ids.

Photos are imported as links (source_url). The old links point at eBay's
servers, so copy them into your own storage before those listings end.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import nh3
import psycopg
from psycopg.types.json import Jsonb

DEFAULT_MSSQL = (
    r"DRIVER={ODBC Driver 17 for SQL Server};SERVER=.\SQLEXPRESS;"
    r"DATABASE=EIMotorsportsInventory;Trusted_Connection=yes;"
)

SOURCE_TABLES = [
    "Categories", "VehicleTypes", "VehicleMakes", "VehicleModels",
    "PartManufacturers", "Products", "ProductPhotos", "ProductItemSpecifics",
    "ProductCompatibility", "InventoryTransactions", "Users", "Vendors",
    "PurchaseOrders", "PurchaseOrderLines", "SalesChannels", "SalesTransactions",
    "EbaySettings",
]

TX_TYPES = {"Receive", "Sale", "Adjustment", "Return", "Breakage"}

ALLOWED_TAGS = {
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u",
    "h2", "h3", "h4", "table", "thead", "tbody", "tr", "th", "td",
    "a", "span", "div",
}

# Tables whose ids we keep; their sequences are advanced past the imported ids.
SEQUENCE_TABLES = [
    "categories", "part_manufacturers", "vehicle_types", "vehicle_makes",
    "vehicle_models", "vehicles", "products", "inventory_transactions",
    "vendors", "purchase_orders", "purchase_order_lines", "sales_transactions",
]

WIPE_TABLES = [
    "sales_transactions", "inventory_transactions", "purchase_order_lines",
    "purchase_orders", "vendors", "product_fitment", "product_photos",
    "product_item_specifics", "channel_listings", "channel_category_refs", "products", "vehicles",
    "vehicle_models", "vehicle_makes", "vehicle_types", "part_manufacturers",
    "categories",
]


class ImportAbort(Exception):
    pass


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def clean(value):
    """Trim, collapse whitespace; empty -> None."""
    if value is None:
        return None
    s = " ".join(str(value).split())
    return s or None


def slugify(value, maxlen=80):
    s = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:maxlen].strip("-")


class SlugSet:
    def __init__(self):
        self.used = set()

    def make(self, base, fallback="item", maxlen=100):
        base = slugify(base, maxlen) or fallback
        slug, n = base, 2
        while slug in self.used:
            slug = f"{base}-{n}"
            n += 1
        self.used.add(slug)
        return slug


def to_utc(dt, tz):
    """Legacy timestamps are naive local times; make them real UTC instants."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def sanitize_html(html):
    if not html or not str(html).strip():
        return None
    out = nh3.clean(
        str(html),
        tags=ALLOWED_TAGS,
        attributes={"a": {"href"}},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    ).strip()
    visible = re.sub(r"<[^>]+>|&nbsp;|\s", "", out)
    return out if visible else None


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------
class MssqlSource:
    def __init__(self, conn_str):
        try:
            import pyodbc
        except ImportError:
            sys.exit("pyodbc is not installed. Run: pip install -r requirements.txt")
        try:
            self.conn = pyodbc.connect(conn_str, timeout=15)
        except pyodbc.Error as e:
            sys.exit(
                "Could not connect to SQL Server.\n"
                f"  {e}\n"
                "Check that SQL Server (SQLEXPRESS) is running, that the ODBC driver\n"
                "name matches one installed on this PC, and pass --mssql if your\n"
                "server or database name differs from the old app's defaults."
            )

    def rows(self, table):
        cur = self.conn.cursor()
        cur.execute(f"SELECT * FROM [{table}]")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


class DictSource:
    """In-memory source (used by the tests)."""

    def __init__(self, data):
        self.data = data

    def rows(self, table):
        return list(self.data.get(table, []))


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.source_counts = {}
        self.dest_counts = {}
        self.skipped = defaultdict(list)   # table -> [(id, reason)]
        self.notes = []                    # informational
        self.checks = {}                   # name -> value

    def skip(self, table, row_id, reason):
        self.skipped[table].append((row_id, reason))

    def note(self, text):
        self.notes.append(text)

    def render(self):
        out = ["", "=" * 64, "IMPORT REPORT", "=" * 64]
        out.append(f"{'Table':<26}{'Source rows':>12}{'Imported':>12}")
        for name, dest in self.dest_counts.items():
            src = self.source_counts.get(name, "")
            out.append(f"{name:<26}{str(src):>12}{dest:>12}")
        if self.skipped:
            out.append("\nSKIPPED ROWS (not imported):")
            for table, items in self.skipped.items():
                out.append(f"  {table}: {len(items)}")
                for rid, reason in items[:10]:
                    out.append(f"    id {rid}: {reason}")
                if len(items) > 10:
                    out.append(f"    ... and {len(items) - 10} more")
        if self.notes:
            out.append("\nNOTES:")
            out.extend(f"  - {n}" for n in self.notes)
        if self.checks:
            out.append("\nCHECKS:")
            out.extend(f"  {k}: {v}" for k, v in self.checks.items())
        return "\n".join(out)


# --------------------------------------------------------------------------
# vehicle normalization
# --------------------------------------------------------------------------
class VehicleIndex:
    def __init__(self, report):
        self.rep = report
        self.make_slugs = SlugSet()
        self.model_slugs = defaultdict(SlugSet)
        self.makes = {}      # lower name -> id
        self.models = {}     # (make_id, lower name) -> id
        self.vehicles = {}   # (model_id, year, lower submodel|None) -> id
        self.make_rows, self.model_rows, self.vehicle_rows = [], [], []
        self.next_make = self.next_model = self.next_vehicle = 1

    def seed_make(self, mid, name):
        self.makes[name.lower()] = mid
        self.make_rows.append((mid, name, self.make_slugs.make(name, "make")))
        self.next_make = max(self.next_make, mid + 1)

    def seed_model(self, mid, make_id, type_id, name):
        self.models[(make_id, name.lower())] = mid
        self.model_rows.append(
            (mid, make_id, type_id, name, self.model_slugs[make_id].make(name, "model"))
        )
        self.next_model = max(self.next_model, mid + 1)

    def get_make(self, name):
        key = name.lower()
        if key not in self.makes:
            self.seed_make(self.next_make, name)
            self.rep.note(f"Created make '{name}' (found in fitment data, not in the makes list)")
        return self.makes[key]

    def get_model(self, make_id, name):
        key = (make_id, name.lower())
        if key not in self.models:
            self.seed_model(self.next_model, make_id, None, name)
            self.rep.note(f"Created model '{name}' (found in fitment data, not in the models list)")
        return self.models[key]

    def get_vehicle(self, model_id, year, submodel):
        key = (model_id, year, submodel.lower() if submodel else None)
        if key not in self.vehicles:
            vid = self.next_vehicle
            self.next_vehicle += 1
            self.vehicles[key] = vid
            self.vehicle_rows.append((vid, model_id, year, submodel))
        return self.vehicles[key]


# --------------------------------------------------------------------------
# the import
# --------------------------------------------------------------------------
def run_import(src, conn, *, tz, publish_active=False, replace=False):
    rep = Report()
    cur = conn.cursor()

    cur.execute("select count(*) from products")
    if cur.fetchone()[0] and not replace:
        raise ImportAbort(
            "The destination already has products. Re-run with --replace to wipe the "
            "imported tables and start over (staff accounts are never touched)."
        )
    if replace:
        cur.execute("truncate " + ", ".join(WIPE_TABLES) + " restart identity cascade")

    S = {t: src.rows(t) for t in SOURCE_TABLES}
    rep.source_counts = {
        "categories": len(S["Categories"]), "part_manufacturers": len(S["PartManufacturers"]),
        "vehicle_types": len(S["VehicleTypes"]), "vehicle_makes": len(S["VehicleMakes"]),
        "vehicle_models": len(S["VehicleModels"]), "products": len(S["Products"]),
        "product_photos": len(S["ProductPhotos"]), "product_item_specifics": len(S["ProductItemSpecifics"]),
        "inventory_transactions": len(S["InventoryTransactions"]), "vendors": len(S["Vendors"]),
        "purchase_orders": len(S["PurchaseOrders"]), "purchase_order_lines": len(S["PurchaseOrderLines"]),
        "sales_transactions": len(S["SalesTransactions"]),
    }
    ts = lambda dt: to_utc(dt, tz)  # noqa: E731

    # ---- taxonomy -------------------------------------------------------
    slugs = SlugSet()
    cat_ids, rows, cat_ebay = set(), [], {}
    for r in S["Categories"]:
        name = clean(r.get("CategoryName"))
        if not name:
            rep.skip("categories", r.get("CategoryID"), "empty name")
            continue
        cat_ids.add(r["CategoryID"])
        cat_ebay[r["CategoryID"]] = clean(r.get("EbayStoreCatID"))
        rows.append((r["CategoryID"], name, slugs.make(name, "category"), ts(r.get("CreatedDate"))))
    cur.executemany(
        "insert into categories (id, name, slug, created_at) values (%s,%s,%s,coalesce(%s, now()))", rows)

    slugs, mfr_ids, rows = SlugSet(), set(), []
    for r in S["PartManufacturers"]:
        name = clean(r.get("Name"))
        if not name:
            rep.skip("part_manufacturers", r.get("PartManufacturerID"), "empty name")
            continue
        mfr_ids.add(r["PartManufacturerID"])
        rows.append((r["PartManufacturerID"], name, slugs.make(name, "manufacturer")))
    cur.executemany("insert into part_manufacturers (id, name, slug) values (%s,%s,%s)", rows)

    slugs, type_ids, rows = SlugSet(), set(), []
    for r in S["VehicleTypes"]:
        name = clean(r.get("TypeName"))
        if not name:
            continue
        type_ids.add(r["VehicleTypeID"])
        rows.append((r["VehicleTypeID"], name, slugs.make(name, "type")))
    cur.executemany("insert into vehicle_types (id, name, slug) values (%s,%s,%s)", rows)

    vi = VehicleIndex(rep)
    for r in S["VehicleMakes"]:
        name = clean(r.get("MakeName"))
        if name:
            vi.seed_make(r["MakeID"], name)
    for r in S["VehicleModels"]:
        name = clean(r.get("ModelName"))
        if name and r["MakeID"] in {m[0] for m in vi.make_rows}:
            tid = r.get("VehicleTypeID")
            vi.seed_model(r["ModelID"], r["MakeID"], tid if tid in type_ids else None, name)
    old_model_ids = {m[0] for m in vi.model_rows}

    # ---- products -------------------------------------------------------
    prod_slugs = SlugSet()
    users = {u["UserID"]: clean(u.get("DisplayName")) for u in S["Users"]}
    product_ids, prod_rows, seen_sku, seen_barcode = set(), [], set(), set()
    product_meta = {}  # id -> dict(sku, active, model_id, ys, ye)
    for r in S["Products"]:
        pid = r["ProductID"]
        sku, title = clean(r.get("SKU")), clean(r.get("Title"))
        if not sku or not title:
            rep.skip("products", pid, "missing SKU or title")
            continue
        if sku in seen_sku:
            rep.skip("products", pid, f"duplicate SKU '{sku}'")
            continue
        seen_sku.add(sku)
        barcode = clean(r.get("Barcode"))
        if barcode in seen_barcode:
            rep.note(f"Product {pid} ({sku}): duplicate barcode '{barcode}' left blank")
            barcode = None
        if barcode:
            seen_barcode.add(barcode)
        upp = r.get("UnitsPerPackage") or 1
        if upp < 1:
            upp = 1
        active = True if r.get("IsActive") is None else bool(r["IsActive"])
        cat = r.get("CategoryID") if r.get("CategoryID") in cat_ids else None
        mfr = r.get("PartManufacturerID") if r.get("PartManufacturerID") in mfr_ids else None
        slug = prod_slugs.make(f"{slugify(title, 70)}-{slugify(sku, 30)}", "product", maxlen=110)
        product_ids.add(pid)
        product_meta[pid] = dict(sku=sku, active=active, model_id=r.get("ModelID"),
                                 ys=r.get("FitmentYearStart"), ye=r.get("FitmentYearEnd"),
                                 image_url=clean(r.get("ImageUrl")),
                                 ebay_item=clean(r.get("EbayItemID")))
        prod_rows.append((
            pid, sku, slug, title, clean(r.get("InternalPartNumber")), clean(r.get("ManufacturerPartNumber")),
            cat, mfr, clean(r.get("PartType")), r.get("BoreSizeMM"), r.get("ShimCount"), r.get("ShimODmm"),
            r.get("PressureRatingBar"), upp, barcode, clean(r.get("BarcodeType")),
            ts(r.get("LabelLastPrintedDate")), r.get("ListPrice"), r.get("Cost"),
            r.get("QuantityOnHand") or 0, r.get("ReorderPoint"), clean(r.get("Condition")),
            sanitize_html(r.get("Description")), active, bool(publish_active and active),
            ts(r.get("CreatedDate")), ts(r.get("ModifiedDate")),
        ))
    cur.executemany(
        """insert into products (id, sku, slug, title, internal_part_number, manufacturer_part_number,
             category_id, part_manufacturer_id, part_type, bore_size_mm, shim_count, shim_od_mm,
             pressure_rating_bar, units_per_package, barcode, barcode_type, label_last_printed_at,
             list_price, cost, quantity_on_hand, reorder_point, condition, description,
             is_active, is_published, created_at, updated_at)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                   coalesce(%s, now()), coalesce(%s, now()))""",
        prod_rows)

    # ---- fitment --------------------------------------------------------
    fit = {}  # (product_id, vehicle_id) -> notes
    for r in S["ProductCompatibility"]:
        pid = r.get("ProductID")
        if pid not in product_ids:
            continue
        make, model, year = clean(r.get("Make")), clean(r.get("Model")), r.get("Year")
        if not make or not model or not year or not (1900 <= int(year) <= 2100):
            rep.skip("product_fitment", r.get("ProductCompatibilityID"), "missing/invalid make, model or year")
            continue
        mk = vi.get_make(make)
        v = vi.get_vehicle(vi.get_model(mk, model), int(year), clean(r.get("Submodel")))
        notes = clean(r.get("Notes"))
        if (pid, v) not in fit or (fit[(pid, v)] is None and notes):
            fit[(pid, v)] = notes

    no_year, partial_year = [], []
    for pid, m in product_meta.items():
        mid, ys, ye = m["model_id"], m["ys"], m["ye"]
        if mid not in old_model_ids:
            continue
        if ys and ye and ys <= ye and ye - ys <= 60:
            years = range(int(ys), int(ye) + 1)
        elif ys or ye:
            years = [int(ys or ye)]
            partial_year.append(m["sku"])
        else:
            no_year.append(m["sku"])
            continue
        for y in years:
            fit.setdefault((pid, vi.get_vehicle(mid, y, None)), None)
    if no_year:
        rep.note(f"{len(no_year)} product(s) were tied to a vehicle model but had NO year range, so they got no "
                 f"fitment rows (a fitment needs a year). First few SKUs: {', '.join(no_year[:8])}")
    if partial_year:
        rep.note(f"{len(partial_year)} product(s) had only one of start/end year; treated as that single year. "
                 f"First few SKUs: {', '.join(partial_year[:8])}")

    cur.executemany("insert into vehicle_makes (id, name, slug) values (%s,%s,%s)", vi.make_rows)
    cur.executemany("insert into vehicle_models (id, make_id, vehicle_type_id, name, slug) values (%s,%s,%s,%s,%s)",
                    vi.model_rows)
    cur.executemany("insert into vehicles (id, model_id, year, submodel) values (%s,%s,%s,%s)", vi.vehicle_rows)
    cur.executemany("insert into product_fitment (product_id, vehicle_id, notes) values (%s,%s,%s)",
                    [(p, v, n) for (p, v), n in fit.items()])

    # ---- photos & specifics ---------------------------------------------
    by_prod = defaultdict(list)
    for r in sorted(S["ProductPhotos"], key=lambda r: (r.get("SortOrder") or 0, r.get("ProductPhotoID") or 0)):
        url = clean(r.get("PhotoUrl"))
        if r.get("ProductID") in product_ids and url:
            by_prod[r["ProductID"]].append({"url": url, "primary": bool(r.get("IsPrimary"))})
    photo_rows = []
    for pid in product_ids:
        photos, seen = [], set()
        for p in by_prod.get(pid, []):
            if p["url"] not in seen:
                seen.add(p["url"])
                photos.append(p)
        thumb = product_meta[pid]["image_url"]
        if thumb and thumb not in seen:
            photos.insert(0, {"url": thumb, "primary": False})
        if not photos:
            continue
        primary = next((i for i, p in enumerate(photos) if p["primary"]), None)
        if primary is None:
            primary = next((i for i, p in enumerate(photos) if p["url"] == thumb), 0)
        for i, p in enumerate(photos):
            photo_rows.append((pid, p["url"], i == primary, i))
    cur.executemany(
        "insert into product_photos (product_id, source_url, is_primary, sort_order) values (%s,%s,%s,%s)",
        photo_rows)

    spec_rows = []
    for r in sorted(S["ProductItemSpecifics"], key=lambda r: (r.get("SortOrder") or 0, r.get("ProductItemSpecificID") or 0)):
        name, value = clean(r.get("SpecificName")), clean(r.get("SpecificValue"))
        if r.get("ProductID") in product_ids and name and value:
            spec_rows.append((r["ProductID"], name, value, r.get("SortOrder") or 0))
    cur.executemany(
        "insert into product_item_specifics (product_id, name, value, sort_order) values (%s,%s,%s,%s)", spec_rows)

    # ---- inventory history ---------------------------------------------
    tx_ids, rows = set(), []
    for r in S["InventoryTransactions"]:
        tid, ttype = r["TransactionID"], clean(r.get("TransactionType"))
        if r.get("ProductID") not in product_ids:
            rep.skip("inventory_transactions", tid, "product was not imported")
            continue
        if ttype not in TX_TYPES:
            rep.skip("inventory_transactions", tid, f"unknown type '{ttype}'")
            continue
        tx_ids.add(tid)
        rows.append((tid, r["ProductID"], ttype, r["QuantityChange"], clean(r.get("Reference")),
                     ts(r.get("TransactionDate")), clean(r.get("CreatedBy"))))
    cur.executemany(
        """insert into inventory_transactions (id, product_id, transaction_type, quantity_change,
             reference, created_at, legacy_created_by) values (%s,%s,%s,%s,%s,coalesce(%s, now()),%s)""", rows)

    # ---- purchasing -----------------------------------------------------
    vendor_ids, seen, rows = set(), set(), []
    for r in S["Vendors"]:
        name = clean(r.get("VendorName"))
        if not name or name in seen:
            rep.skip("vendors", r.get("VendorID"), "empty or duplicate name")
            continue
        seen.add(name)
        vendor_ids.add(r["VendorID"])
        rows.append((r["VendorID"], name, clean(r.get("ContactInfo")),
                     True if r.get("IsActive") is None else bool(r["IsActive"])))
    cur.executemany("insert into vendors (id, name, contact_info, is_active) values (%s,%s,%s,%s)", rows)

    po_ids, rows = set(), []
    for r in S["PurchaseOrders"]:
        po_ids.add(r["PurchaseOrderID"])
        vid = r.get("VendorID") if r.get("VendorID") in vendor_ids else None
        rows.append((r["PurchaseOrderID"], vid, ts(r.get("OrderDate")), ts(r.get("ReceivedDate")),
                     clean(r.get("ReferenceNumber")), users.get(r.get("CreatedByUserID"))))
    cur.executemany(
        """insert into purchase_orders (id, vendor_id, order_date, received_date, reference_number,
             legacy_created_by) values (%s,%s,coalesce(%s, now()),%s,%s,%s)""", rows)

    rows = []
    for r in S["PurchaseOrderLines"]:
        lid = r["PurchaseOrderLineID"]
        if r.get("PurchaseOrderID") not in po_ids or r.get("ProductID") not in product_ids:
            rep.skip("purchase_order_lines", lid, "order or product was not imported")
        elif (r.get("QuantityOrdered") or 0) <= 0 or (r.get("QuantityReceived") or 0) < 0:
            rep.skip("purchase_order_lines", lid, "invalid quantity")
        else:
            rows.append((lid, r["PurchaseOrderID"], r["ProductID"], r["QuantityOrdered"],
                         r.get("QuantityReceived") or 0, r["UnitCost"]))
    cur.executemany(
        """insert into purchase_order_lines (id, purchase_order_id, product_id, quantity_ordered,
             quantity_received, unit_cost) values (%s,%s,%s,%s,%s,%s)""", rows)

    # ---- sales channels & sales ----------------------------------------
    cur.execute("select id, lower(name) from sales_channels")
    existing = {name: cid for cid, name in cur.fetchall()}

    def channel_id(name):
        if name.lower() not in existing:
            cur.execute("insert into sales_channels (name) values (%s) returning id", (name,))
            existing[name.lower()] = cur.fetchone()[0]
            rep.note(f"Created sales channel '{name}'")
        return existing[name.lower()]

    channel_map = {}
    for r in S["SalesChannels"]:
        name = clean(r.get("ChannelName"))
        if name:
            channel_map[r["SalesChannelID"]] = channel_id(name)

    # ---- eBay cross-references (background only) ------------------------
    item_rows = [(pid, m["ebay_item"], m["active"]) for pid, m in product_meta.items() if m["ebay_item"]]
    cat_refs = [(cid, v) for cid, v in cat_ebay.items() if v]
    settings_row = S["EbaySettings"][0] if S["EbaySettings"] else None
    if item_rows or cat_refs or settings_row:
        ebay = channel_id("eBay")
        seen_items, listing_rows = set(), []
        for pid, item, active in item_rows:
            if item in seen_items:
                rep.skip("channel_listings", pid, f"eBay item {item} is already linked to another product")
                continue
            seen_items.add(item)
            url = f"https://www.ebay.com/itm/{item}" if item.isdigit() else None
            listing_rows.append((pid, ebay, item, url, "active" if active else "ended"))
        cur.executemany(
            """insert into channel_listings (product_id, channel_id, external_id, external_url, status)
               values (%s,%s,%s,%s,%s)""", listing_rows)
        if listing_rows:
            rep.note("eBay listing status was inferred from each product's Active flag "
                     "(active -> 'active', otherwise 'ended'); refresh it from eBay later.")
        cur.executemany(
            "insert into channel_category_refs (category_id, channel_id, external_id) values (%s,%s,%s)",
            [(cid, ebay, v) for cid, v in cat_refs])
        if settings_row and (clean(settings_row.get("SellerUsername")) or clean(settings_row.get("CategoryIds"))):
            cats = [c.strip() for c in (settings_row.get("CategoryIds") or "").split(",") if c.strip()]
            cur.execute(
                """insert into integration_settings (provider, settings) values ('ebay', %s)
                   on conflict (provider) do update set settings = excluded.settings, updated_at = now()""",
                (Jsonb({"seller_username": clean(settings_row.get("SellerUsername")), "category_ids": cats}),))

    rows = []
    for r in S["SalesTransactions"]:
        sid = r["SalesTransactionID"]
        if r.get("ProductID") not in product_ids:
            rep.skip("sales_transactions", sid, "product was not imported")
        elif (r.get("QuantitySold") or 0) <= 0:
            rep.skip("sales_transactions", sid, f"quantity sold is {r.get('QuantitySold')} (must be > 0)")
        elif r.get("UnitSalePrice") is None:
            rep.skip("sales_transactions", sid, "missing sale price")
        else:
            itx = r.get("InventoryTransactionID")
            rows.append((sid, r["ProductID"], channel_map.get(r.get("SalesChannelID")), r["QuantitySold"],
                         r["UnitSalePrice"], r.get("UnitCostAtSale"), r.get("ShippingCharged"),
                         r.get("ShippingCost"), r.get("PlatformFees"), ts(r.get("SaleDate")),
                         clean(r.get("OrderReference")), itx if itx in tx_ids else None,
                         clean(r.get("ShipToState")), clean(r.get("ShipToZip"))))
    cur.executemany(
        """insert into sales_transactions (id, product_id, channel_id, quantity_sold, unit_sale_price,
             unit_cost_at_sale, shipping_charged, shipping_cost, platform_fees, sale_date, order_reference,
             inventory_transaction_id, ship_to_state, ship_to_zip)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,coalesce(%s, now()),%s,%s,%s,%s)""", rows)

    # ---- advance identity sequences past the imported ids ---------------
    for t in SEQUENCE_TABLES:
        cur.execute(
            f"select setval(pg_get_serial_sequence('public.{t}','id'), "
            f"greatest((select coalesce(max(id),0) from public.{t}), 1), "
            f"(select count(*) > 0 from public.{t}))")

    # ---- verification ---------------------------------------------------
    for t in ["categories", "part_manufacturers", "vehicle_types", "vehicle_makes", "vehicle_models",
              "vehicles", "products", "product_fitment", "product_photos", "product_item_specifics",
              "inventory_transactions", "vendors", "purchase_orders", "purchase_order_lines",
              "sales_transactions", "channel_listings", "channel_category_refs"]:
        cur.execute(f"select count(*) from {t}")
        rep.dest_counts[t] = cur.fetchone()[0]

    def one(sql):
        cur.execute(sql)
        return cur.fetchone()[0]

    src_qty = sum((r.get("QuantityOnHand") or 0) for r in S["Products"] if r["ProductID"] in product_ids)
    rep.checks["total units on hand (source vs imported)"] = (
        f"{src_qty} vs {one('select coalesce(sum(quantity_on_hand),0) from products')}")
    rep.checks["products marked published"] = one("select count(*) from products where is_published")
    rep.checks["products with no fitment"] = one(
        "select count(*) from products p where not exists (select 1 from product_fitment f where f.product_id=p.id)")
    rep.checks["products with no photo"] = one(
        "select count(*) from products p where not exists (select 1 from product_photos f where f.product_id=p.id)")
    rep.checks["products with no category"] = one("select count(*) from products where category_id is null")
    rep.checks["products with no price"] = one("select count(*) from products where list_price is null")
    rep.checks["products whose history doesn't add up to units on hand"] = one(
        """select count(*) from products p left join
             (select product_id, sum(quantity_change) s from inventory_transactions group by 1) t
             on t.product_id = p.id where coalesce(t.s,0) <> p.quantity_on_hand""")
    return rep


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mssql", default=os.getenv("SOURCE_MSSQL", DEFAULT_MSSQL),
                    help="ODBC connection string for the old SQL Server database")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"),
                    help="destination Postgres URL (default: $DATABASE_URL)")
    ap.add_argument("--source-tz", default="America/New_York",
                    help="time zone the old app's timestamps were recorded in")
    ap.add_argument("--publish-active", action="store_true",
                    help="mark every active product as visible on the website")
    ap.add_argument("--replace", action="store_true",
                    help="wipe previously imported data in the destination first")
    ap.add_argument("--commit", action="store_true", help="save for real (default is a dry run)")
    args = ap.parse_args(argv)

    if not args.database_url:
        sys.exit("No destination. Set DATABASE_URL (or pass --database-url).")
    try:
        tz = ZoneInfo(args.source_tz)
    except Exception:
        sys.exit(f"Unknown time zone '{args.source_tz}'. Try: pip install tzdata")

    src = MssqlSource(args.mssql)
    url = args.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    conn = psycopg.connect(url, prepare_threshold=None)
    try:
        rep = run_import(src, conn, tz=tz, publish_active=args.publish_active, replace=args.replace)
    except ImportAbort as e:
        conn.rollback()
        sys.exit(f"STOPPED: {e}")
    except Exception:
        conn.rollback()
        print("\nImport failed; nothing was saved.", file=sys.stderr)
        raise
    print(rep.render())
    if args.commit:
        conn.commit()
        print("\nCOMMITTED: data saved.")
    else:
        conn.rollback()
        print("\nDRY RUN: nothing was saved. Review the report, then re-run with --commit.")
    conn.close()


if __name__ == "__main__":
    main()
