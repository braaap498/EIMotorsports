"""
Turns rows of eBay's "All orders" report (Seller Hub > Reports > Downloads) into clean order + line records.

No web or database code in here, so it can be tested on its own.

How eBay lays the report out (checked against real exports from both stores):
  * ONE-item order: a single row that has the item AND the order totals.
        "Sold For" is the price of ONE unit, "Total Price" is the whole order (items + shipping + tax).
  * MULTI-item order: a "header" row with no Item Number that carries the order totals
        ("Sold For" there is the items subtotal), followed by one row per item (price per unit, no totals).
  * "Total Price" is what is left AFTER refunds. A cancelled / fully refunded order shows $0.00 and a partly
        refunded one shows less than items + shipping + tax. The report has no refund column, so refunds are
        ESTIMATED from that shortfall. (It also has no eBay fees; those come from QuickBooks / the Finances API.)

Only the columns in WANTED are kept. Buyer names, emails and street addresses are never read or stored.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Optional

WANTED = [
    "Sales Record Number", "Order Number", "Buyer Username", "Buyer State", "Buyer Country",
    "Item Number", "Item Title", "Custom Label", "Sold Via Promoted Listings", "Quantity", "Sold For",
    "Shipping And Handling", "Seller Collected Tax", "eBay Collected Tax", "Total Price", "Sale Date",
    "Paid On Date", "Shipped On Date", "Feedback Received", "Shipping Service", "Transaction ID",
    "Global Shipping Program",
]

STORE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
_CENT = Decimal("0.01")
REFUND_NOISE = Decimal("1.00")  # a shortfall under $1 is rounding / a tax adjustment, not a refund


def clean_store(value: str) -> str:
    s = (value or "").strip().lower()
    if not STORE_RE.match(s):
        raise ValueError("Store name must be letters, numbers, dots, dashes or underscores (e.g. genuine-oem-parts).")
    return s


def money(v: Any) -> Decimal:
    s = str(v or "").strip().replace("$", "").replace(",", "")
    if not s or s in ("-", "--"):
        return Decimal(0)
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        d = Decimal(s)
    except Exception:
        return Decimal(0)
    return -d if neg else d


def whole(v: Any) -> int:
    try:
        return int(Decimal(str(v or "0").strip() or "0"))
    except Exception:
        return 0


def parse_date(v: Any) -> Optional[date]:
    s = str(v or "").strip()
    if not s or s == "--":
        return None
    for fmt in ("%b-%d-%y", "%b-%d-%Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _txt(v: Any, limit: int = 300) -> Optional[str]:
    s = str(v or "").strip()
    return s[:limit] if s and s != "--" else None


def _yes(v: Any) -> Optional[bool]:
    s = str(v or "").strip().lower()
    if s in ("yes", "y", "true"):
        return True
    if s in ("no", "n", "false"):
        return False
    return None


def _q(d: Decimal) -> Decimal:
    return d.quantize(_CENT, rounding=ROUND_HALF_UP)


def group_rows(rows: Iterable[dict]) -> "OrderedDict[str, list[dict]]":
    """Group report rows by Order Number (footer lines such as '1289 record(s) downloaded' are dropped)."""
    out: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in rows:
        num = str(r.get("Order Number") or "").strip()
        if not num or "record(s) downloaded" in num or num.lower().startswith("seller id"):
            continue
        out.setdefault(num, []).append(r)
    return out


def build_order(store: str, number: str, rows: list[dict], source_file: Optional[str]) -> Optional[dict]:
    """One order's rows -> {'order': {...}, 'items': [...]}, or None if the order can't be read."""
    head = next((r for r in rows if str(r.get("Total Price") or "").strip() != ""), None)
    if head is None:
        return None
    sale = parse_date(head.get("Sale Date"))
    if sale is None:
        return None

    lines = [r for r in rows if str(r.get("Item Number") or "").strip() != ""]
    items = []
    for i, r in enumerate(lines, start=1):
        qty = max(whole(r.get("Quantity")), 0)
        items.append({
            "line_no": i,
            "item_number": _txt(r.get("Item Number"), 40),
            "item_title": _txt(r.get("Item Title"), 300),
            "custom_label": _txt(r.get("Custom Label"), 200),
            "transaction_id": _txt(r.get("Transaction ID"), 60),
            "quantity": qty,
            "unit_price": _q(money(r.get("Sold For"))),
        })

    multi = str(head.get("Item Number") or "").strip() == ""
    if multi:
        subtotal = money(head.get("Sold For"))
        if subtotal == 0:
            subtotal = sum((it["unit_price"] * it["quantity"] for it in items), Decimal(0))
    else:
        subtotal = money(head.get("Sold For")) * whole(head.get("Quantity"))
    units = sum(it["quantity"] for it in items) or whole(head.get("Quantity"))

    shipping = money(head.get("Shipping And Handling"))
    tax = money(head.get("eBay Collected Tax")) + money(head.get("Seller Collected Tax"))
    total = money(head.get("Total Price"))

    gross = subtotal + shipping                       # what was sold, before any refund, without tax
    net = max(total - tax, Decimal(0))                # what was kept, without tax
    if net > gross:
        net = gross
    refund = gross - net
    if refund < REFUND_NOISE:
        refund, net = Decimal(0), gross

    shipped = parse_date(head.get("Shipped On Date"))
    order = {
        "store": store,
        "order_number": number[:40],
        "sales_record_number": _txt(head.get("Sales Record Number"), 40),
        "sale_date": sale,
        "paid_on_date": parse_date(head.get("Paid On Date")),
        "shipped_on_date": shipped,
        "buyer_username": _txt(head.get("Buyer Username"), 80),
        "buyer_state": _txt(head.get("Buyer State"), 40),
        "buyer_country": _txt(head.get("Buyer Country"), 60),
        "line_count": len(items),
        "units": units,
        "items_subtotal": _q(subtotal),
        "shipping_charged": _q(shipping),
        "tax_collected": _q(tax),
        "total_price": _q(total),
        "gross_sales": _q(gross),
        "net_sales": _q(net),
        "refund_est": _q(refund),
        "shipping_service": _txt(head.get("Shipping Service"), 80),
        "promoted": _yes(head.get("Sold Via Promoted Listings")),
        "global_shipping": _yes(head.get("Global Shipping Program")),
        "feedback_received": _txt(head.get("Feedback Received"), 20),
        "days_to_ship": (shipped - sale).days if shipped and shipped >= sale else None,
        "source_file": (source_file or "")[:200] or None,
    }
    return {"order": order, "items": items}


def build_orders(store: str, rows: Iterable[dict], source_file: Optional[str] = None) -> tuple[list[dict], int]:
    """Returns (records, skipped_order_count)."""
    out, skipped = [], 0
    for number, group in group_rows(rows).items():
        rec = build_order(store, number, group, source_file)
        if rec is None:
            skipped += 1
        else:
            out.append(rec)
    return out, skipped
