"""Keep website products' list price and cost in step with the distributor catalog they were made from.

A product made from a catalog part (Automatic Distributors or Rocky Mountain ATV) is a SINGLE-part product: it has one
catalog part behind it, found either in supplier_product_links (Automatic) or as its only Distributor parts entry
(product_kit_parts, quantity 1, with a supplier_item_id). Those products got their price and cost copied once, when they
were made. This module copies them again whenever the catalog changes:

    products.list_price = the catalog's list price (the MAP price when there is one, else the MSRP)
    products.cost       = the catalog's cost (the price file's own cost when there is one, else the dealer cost;
                          for Automatic: the catalog's effective cost)

Rules:
  * Only the catalog part's SINGLE product (the one in the catalog's "Ours" column): no parts line or exactly one with quantity 1.
    A kit that merely contains or links the part is never touched: its price is not one part's price.
  * A catalog value that is blank never wipes a price or cost already on the product.
  * Only rows whose value really changed are written (and get a new updated_at), so running it again is cheap and safe.
  * It overwrites the product's price with the catalog's: that is the point. Edit the price in the catalog file, not on the product.
Nothing here commits; the caller does.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# the catalog's cost and list price for one supplier_items row `s`
_COST_SQL = {
    "autodist": "s.effective_cost",
    "rmatv": "coalesce(s.my_cost, s.dealer_cost)",
}
_LIST_SQL = "coalesce(s.map_price, s.list_price)"


def _sql(supplier: str) -> str:
    cost = _COST_SQL.get(supplier, "coalesce(s.my_cost, s.dealer_cost)")
    return f"""
with src as (
  -- Automatic: the link made when the product was created from the catalog
  select l.product_id, s.id as item_id
  from supplier_product_links l
  join supplier_items s on s.supplier = l.supplier and s.sku = l.supplier_sku
  where l.supplier = :sup
    and (select count(*) from product_kit_parts z where z.product_id = l.product_id) <= 1
    and not exists (select 1 from product_kit_parts z where z.product_id = l.product_id and coalesce(z.quantity, 1) <> 1)
  union
  -- both suppliers: the product's only Distributor parts entry (quantity 1) points at a part of this supplier
  select k.product_id, k.supplier_item_id
  from product_kit_parts k
  join supplier_items s on s.id = k.supplier_item_id and s.supplier = :sup
  where k.quantity = 1
    and (select count(*) from product_kit_parts k2 where k2.product_id = k.product_id) = 1
), one as (
  -- a product that somehow points at two different catalog parts is left alone
  select product_id, min(item_id) as item_id from src group by product_id having count(distinct item_id) = 1
), want as (
  select o.product_id, {cost} as new_cost, {_LIST_SQL} as new_list
  from one o join supplier_items s on s.id = o.item_id
), up as (
  update products p set
    cost = coalesce(w.new_cost, p.cost),
    list_price = coalesce(w.new_list, p.list_price),
    updated_at = now()
  from want w
  where p.id = w.product_id
    and (p.cost, p.list_price) is distinct from (coalesce(w.new_cost, p.cost), coalesce(w.new_list, p.list_price))
  returning 1
)
select (select count(*) from one) as linked, (select count(*) from up) as changed
"""


def sync_linked_product_prices(db: Session, supplier: str) -> dict:
    """Copy the catalog's list price and cost onto every single-part product made from this supplier's catalog.
    Returns {"linked": products found, "changed": products whose price or cost actually changed}. No commit."""
    row = db.execute(text(_sql(supplier)), {"sup": supplier}).first()
    return {"linked": int(row.linked or 0), "changed": int(row.changed or 0)}


def sync_after_import(db: Session, supplier: str) -> None:
    """Called when a feed import finishes. Never lets a problem here break the import itself."""
    try:
        res = sync_linked_product_prices(db, supplier)
        db.commit()
        if res["changed"]:
            log.info("%s: updated list price / cost on %s linked products", supplier, res["changed"])
    except Exception:
        db.rollback()
        log.exception("%s: could not update the linked products' prices after the import", supplier)
