"""
Analytics from QuickBooks: costs, fees, vendors and a check against the eBay export. Owner / admin only.
Mounted at /admin/analytics/quickbooks/*.

  GET  /analytics/quickbooks/summary    everything the QuickBooks section of the Analytics page needs
                                        (?start=2022-01-01&end=2022-12-31; no dates = everything)

It only READS qb_records (the mirror the QuickBooks sync fills) and, for the cross-check, analytics_ebay_orders.
Nothing is written anywhere.

How the numbers are worked out (all from the line items saved with each record):

  Sales      sales receipt / invoice lines whose item posts to an income account, minus credit memos.
             Split into product sales, shipping collected (accounts with "freight" or "shipping" in the name) and
             refunds (accounts with "refund" or "return" in the name). Lines that post to a cost account (a fee
             taken off the receipt, say) count as costs instead. Sales tax and anything else is left out.
             The channel (eBay / Amazon / Other / Unassigned) comes from the deposit account or payment method
             on the receipt.
  Costs      bills, checks, credit card charges (less card credits and vendor credits) and journal entries:
             - expense lines count when the account is an expense or cost-of-goods account,
             - item lines count as "Inventory purchases" (an item line that posts to an expense account counts
               under that account instead),
             - lines to bank, card, loan, tax, payroll liability or equity accounts are NOT costs and are left out.
             Bill payments are not counted (the bill already was).
             Each cost is tagged: inventory | cogs (cost-of-goods accounts: fees, postage, supplies) | opex.
  Vendors    the same purchase costs, grouped by who was paid.
"""
import logging
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/analytics/quickbooks", tags=["admin-analytics-quickbooks"], dependencies=[Depends(require_admin)])

# Entities whose lines are costs, and the ones that REDUCE a cost.
PURCHASE_ENTITIES = ("Bill", "Check", "CreditCardCharge", "CreditCardCredit", "VendorCredit")
CREDIT_ENTITIES = ("CreditCardCredit", "VendorCredit")


def _arr(expr: str) -> str:
    """
    A JSON field that may be a list, a single object (the sync keeps a repeated tag as a list but a tag that
    appears once as an object, except tags ending in "Ret"), or missing: always as a JSON list.
    """
    return f"(case jsonb_typeof({expr}) when 'array' then {expr} when 'object' then jsonb_build_array({expr}) else '[]'::jsonb end)"


def _num(expr: str) -> str:
    """A text field as a number, or null if it isn't one (so one odd record can't break the whole page)."""
    return f"(case when ({expr}) ~ '^-?[0-9]+(\\.[0-9]+)?$' then ({expr})::numeric end)"


def _tables_ready(db: Session) -> bool:
    return bool(db.execute(text("select to_regclass('public.qb_records') is not null")).scalar())


def _ebay_ready(db: Session) -> bool:
    return bool(db.execute(text("select to_regclass('public.analytics_ebay_orders') is not null")).scalar())


def _rows(db: Session, sql: str, params: dict) -> list[dict]:
    return [
        {k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in r.items()}
        for r in db.execute(text(sql), params).mappings().all()
    ]


# --------------------------------------------------------------------------
# SQL pieces
# --------------------------------------------------------------------------
_RANGE = "(cast(:start as date) is null or r.txn_date >= cast(:start as date)) and (cast(:end as date) is null or r.txn_date <= cast(:end as date))"

# Account name -> account type, and item name -> the accounts it posts to.
_LISTS = """
accts as (
  select name, max(data->>'AccountType') as atype
  from qb_records where entity = 'Account' and deleted_at is null and name is not null group by name
),
items as (
  select distinct on (name) name,
    coalesce(data->'IncomeAccountRef'->>'FullName',
             data->'SalesAndPurchase'->'IncomeAccountRef'->>'FullName',
             data->'SalesOrPurchase'->'AccountRef'->>'FullName') as sale_acct,
    coalesce(data->'AssetAccountRef'->>'FullName',
             data->'SalesAndPurchase'->'ExpenseAccountRef'->>'FullName',
             data->'COGSAccountRef'->>'FullName',
             data->'ExpenseAccountRef'->>'FullName',
             data->'SalesOrPurchase'->'AccountRef'->>'FullName') as buy_acct
  from qb_records where entity = 'Item' and deleted_at is null and name is not null
  order by name, qb_id
)
"""

_SALES_SQL = f"""
with {_LISTS},
raw as (
  select r.qb_id, r.txn_date, 1 as sign,
         lower(coalesce(r.data->'DepositToAccountRef'->>'FullName', '') || ' ' || coalesce(r.data->'PaymentMethodRef'->>'FullName', '')) as chan_text,
         l.value as line
  from qb_records r
  cross join lateral jsonb_array_elements({_arr("r.data->'SalesReceiptLineRet'")}) as l(value)
  where r.entity = 'SalesReceipt' and r.deleted_at is null and r.txn_date is not null and {_RANGE}
  union all
  select r.qb_id, r.txn_date, 1,
         lower(coalesce(r.data->'PaymentMethodRef'->>'FullName', '')),
         l.value
  from qb_records r
  cross join lateral jsonb_array_elements({_arr("r.data->'InvoiceLineRet'")}) as l(value)
  where r.entity = 'Invoice' and r.deleted_at is null and r.txn_date is not null and {_RANGE}
  union all
  select r.qb_id, r.txn_date, -1,
         lower(coalesce(r.data->'PaymentMethodRef'->>'FullName', '')),
         l.value
  from qb_records r
  cross join lateral jsonb_array_elements({_arr("r.data->'CreditMemoLineRet'")}) as l(value)
  where r.entity = 'CreditMemo' and r.deleted_at is null and r.txn_date is not null and {_RANGE}
),
priced as (
  select w.qb_id, w.txn_date, w.chan_text, w.sign * {_num("w.line->>'Amount'")} as amt,
         i.sale_acct, a.atype
  from raw w
  left join items i on i.name = w.line->'ItemRef'->>'FullName'
  left join accts a on a.name = i.sale_acct
),
tagged as (
  select qb_id, to_char(date_trunc('month', txn_date), 'YYYY-MM') as month, amt,
         case when chan_text like '%ebay%' then 'eBay'
              when chan_text like '%amazon%' then 'Amazon'
              when btrim(chan_text) = '' then 'Unassigned' else 'Other' end as channel,
         case when atype in ('Income', 'OtherIncome') and sale_acct ilike any (array['%refund%', '%return%']) then 'refunds'
              when atype in ('Income', 'OtherIncome') and sale_acct ilike any (array['%freight%', '%shipping%']) then 'shipping'
              when atype in ('Income', 'OtherIncome') and sale_acct ilike '%other income%' then 'other'
              when atype in ('Income', 'OtherIncome') then 'product'
              when atype in ('CostOfGoodsSold', 'Expense', 'OtherExpense') then 'cost'
              else 'other' end as bucket,
         case when atype in ('CostOfGoodsSold', 'Expense', 'OtherExpense') then sale_acct end as cost_acct,
         case when atype = 'CostOfGoodsSold' then 'cogs' else 'opex' end as cost_kind
  from priced
)
select month, channel, bucket,
       case when bucket = 'cost' then split_part(cost_acct, ':', 1) end as category,
       case when bucket = 'cost' then cost_kind end as kind,
       coalesce(sum(amt), 0) as amount, count(distinct qb_id) as receipts
from tagged
group by 1, 2, 3, 4, 5
order by 1, 2, 3
"""

# One row per purchase line (or journal line) with the account it posts to.
_BUY_LINES = f"""
raw as (
  select r.entity, r.qb_id, r.txn_date, r.party, 'acct' as src,
         case when r.entity in {CREDIT_ENTITIES!r} then -1 else 1 end as sign, l.value as line
  from qb_records r
  cross join lateral jsonb_array_elements({_arr("r.data->'ExpenseLineRet'")}) as l(value)
  where r.entity in {PURCHASE_ENTITIES!r} and r.deleted_at is null and r.txn_date is not null and {_RANGE}
  union all
  select r.entity, r.qb_id, r.txn_date, r.party, 'item',
         case when r.entity in {CREDIT_ENTITIES!r} then -1 else 1 end, l.value
  from qb_records r
  cross join lateral jsonb_array_elements({_arr("r.data->'ItemLineRet'")}) as l(value)
  where r.entity in {PURCHASE_ENTITIES!r} and r.deleted_at is null and r.txn_date is not null and {_RANGE}
  union all
  select r.entity, r.qb_id, r.txn_date, r.party, 'item',
         case when r.entity in {CREDIT_ENTITIES!r} then -1 else 1 end, l.value
  from qb_records r
  cross join lateral jsonb_array_elements({_arr("r.data->'ItemGroupLineRet'")}) as g(value)
  cross join lateral jsonb_array_elements({_arr("g.value->'ItemLineRet'")}) as l(value)
  where r.entity in {PURCHASE_ENTITIES!r} and r.deleted_at is null and r.txn_date is not null and {_RANGE}
  union all
  select r.entity, r.qb_id, r.txn_date, null, 'acct', 1, l.value
  from qb_records r
  cross join lateral jsonb_array_elements({_arr("r.data->'JournalDebitLine'")}) as l(value)
  where r.entity = 'JournalEntry' and r.deleted_at is null and r.txn_date is not null and {_RANGE}
  union all
  select r.entity, r.qb_id, r.txn_date, null, 'acct', -1, l.value
  from qb_records r
  cross join lateral jsonb_array_elements({_arr("r.data->'JournalCreditLine'")}) as l(value)
  where r.entity = 'JournalEntry' and r.deleted_at is null and r.txn_date is not null and {_RANGE}
),
priced as (
  select w.entity, w.qb_id, w.txn_date, w.party, w.src, w.sign * {_num("w.line->>'Amount'")} as amt,
         case when w.src = 'item' then i.buy_acct else w.line->'AccountRef'->>'FullName' end as acct,
         a.atype
  from raw w
  left join items i on w.src = 'item' and i.name = w.line->'ItemRef'->>'FullName'
  left join accts a on a.name = (case when w.src = 'item' then i.buy_acct else w.line->'AccountRef'->>'FullName' end)
),
costs as (
  select entity, qb_id, txn_date, party, amt,
         case when src = 'item' and (atype is null or atype not in ('Expense', 'OtherExpense')) then 'inventory'
              when atype = 'CostOfGoodsSold' then 'cogs'
              when atype in ('Expense', 'OtherExpense') then 'opex'
              else null end as kind,
         case when src = 'item' and (atype is null or atype not in ('Expense', 'OtherExpense')) then 'Inventory purchases'
              else split_part(acct, ':', 1) end as category
  from priced
)
"""

_COSTS_SQL = f"""
with {_LISTS},
{_BUY_LINES}
select to_char(date_trunc('month', txn_date), 'YYYY-MM') as month, kind, category,
       coalesce(sum(amt), 0) as amount
from costs where kind is not null and amt is not null
group by 1, 2, 3 order by 1, 2, 3
"""

_NOT_COSTS_SQL = f"""
with {_LISTS},
{_BUY_LINES}
select coalesce(sum(amt), 0) as amount, count(*) as lines
from costs where kind is null and amt is not null and entity <> 'JournalEntry'
"""

_VENDORS_SQL = f"""
with {_LISTS},
{_BUY_LINES}
select party as vendor, coalesce(sum(amt), 0) as amount,
       coalesce(sum(amt) filter (where kind = 'inventory'), 0) as inventory,
       count(distinct qb_id) as txns
from costs
where kind is not null and amt is not null and entity <> 'JournalEntry' and party is not null
group by party order by amount desc limit 15
"""

_COVERAGE_SQL = """
select
  min(txn_date) filter (where entity in ('SalesReceipt', 'Invoice')) as sales_first,
  max(txn_date) filter (where entity in ('SalesReceipt', 'Invoice')) as sales_last,
  count(*) filter (where entity in ('SalesReceipt', 'Invoice')) as sales_records,
  min(txn_date) filter (where entity in ('Bill', 'Check', 'CreditCardCharge', 'CreditCardCredit', 'VendorCredit', 'JournalEntry')) as costs_first,
  max(txn_date) filter (where entity in ('Bill', 'Check', 'CreditCardCharge', 'CreditCardCredit', 'VendorCredit', 'JournalEntry')) as costs_last,
  count(*) filter (where entity in ('Bill', 'Check', 'CreditCardCharge', 'CreditCardCredit', 'VendorCredit', 'JournalEntry')) as cost_records
from qb_records where deleted_at is null and txn_date is not null
"""


@router.get("/summary")
def summary(start: Optional[date] = None, end: Optional[date] = None, db: Session = Depends(get_db)):
    if not _tables_ready(db):
        return {"ready": False}
    p: dict[str, Any] = {"start": start, "end": end}

    coverage = _rows(db, _COVERAGE_SQL, {})[0]
    if not coverage["sales_records"] and not coverage["cost_records"]:
        return {"ready": True, "empty": True, "coverage": coverage}

    sales_rows = _rows(db, _SALES_SQL, p)
    cost_rows = _rows(db, _COSTS_SQL, p)
    not_costs = _rows(db, _NOT_COSTS_SQL, p)[0]
    vendors = _rows(db, _VENDORS_SQL, p)

    # Sales lines split out by what they are; the receipt lines that are really costs (a fee taken off a sale)
    # join the cost list.
    sales = [r for r in sales_rows if r["bucket"] in ("product", "shipping", "refunds")]
    receipt_costs = [
        # A fee taken off a sale is a NEGATIVE line on the receipt; as a cost it is positive.
        {"month": r["month"], "kind": r["kind"], "category": r["category"] or "Unknown account", "amount": -r["amount"], "source": "receipt"}
        for r in sales_rows if r["bucket"] == "cost"
    ]
    purchase_costs = [
        {"month": r["month"], "kind": r["kind"], "category": r["category"] or "Unknown account", "amount": r["amount"], "source": "purchase"}
        for r in cost_rows
    ]
    sales_other = sum(r["amount"] for r in sales_rows if r["bucket"] == "other")

    ebay_monthly: list[dict] = []
    if _ebay_ready(db):
        ebay_monthly = _rows(
            db,
            """select to_char(date_trunc('month', sale_date), 'YYYY-MM') as month, coalesce(sum(net_sales), 0) as net_sales,
                      count(*) as orders
               from analytics_ebay_orders o
               where (cast(:start as date) is null or o.sale_date >= cast(:start as date))
                 and (cast(:end as date) is null or o.sale_date <= cast(:end as date))
               group by 1 order by 1""",
            p,
        )

    return {
        "ready": True,
        "empty": False,
        "start": start,
        "end": end,
        "coverage": coverage,
        "sales": sales,
        "costs": receipt_costs + purchase_costs,
        "vendors": vendors,
        "ebay_monthly": ebay_monthly,
        "left_out": {
            "sales_other": sales_other,
            "purchase_non_costs": not_costs["amount"],
            "purchase_non_cost_lines": not_costs["lines"],
        },
    }
