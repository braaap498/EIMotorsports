"""
Shipping choices and prices for checkout. This is the ONLY place that knows how
shipping is priced, so it can be replaced later (for example with live carrier
rates from Shippo or EasyPost) without touching the rest of checkout.

For now the prices are fixed amounts modeled on the eBay listings. They do not
depend on weight, box size or destination ZIP, because the products table has
no weight or box-size data yet. Change a price by editing RATES below.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ShippingOption:
    code: str
    label: str
    cents: int


# (code, label, cents per item). "Each additional" on eBay costs the same as the
# first item, so the price is simply per item.
RATES = [
    ("ground_advantage", "USPS Ground Advantage", 0),
    ("priority", "USPS Priority Mail", 799),
    ("priority_express", "USPS Priority Mail Express", 6999),
]


def shipping_options(units: int, subtotal_cents: int, ship_to: Optional[dict] = None) -> list[ShippingOption]:
    """The choices offered for a cart of `units` items. `ship_to` (region, postal_code,
    country) is accepted now so ZIP-based pricing can be added here later."""
    return [ShippingOption(code, label, per_item * max(units, 0)) for code, label, per_item in RATES]


def find_option(code: str, units: int, subtotal_cents: int, ship_to: Optional[dict] = None) -> Optional[ShippingOption]:
    for opt in shipping_options(units, subtotal_cents, ship_to):
        if opt.code == code:
            return opt
    return None
