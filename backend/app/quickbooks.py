"""
QuickBooks Desktop -> website, READ ONLY.

How it connects: QuickBooks Desktop cannot be reached from the internet. Instead, Intuit's free
"QuickBooks Web Connector" runs on the Windows PC that has QuickBooks and calls THIS server on a
schedule. Each call is a small SOAP message; we answer with a qbXML question ("send me the
customers changed since ..."), QuickBooks answers, and we save the answer. Nothing is ever pushed
into QuickBooks: every request below is a *Query, and the connection file asks QuickBooks for
read-only access.

Two routers live here:

  soap_router   POST /qbwc                 the endpoint the Web Connector calls (public URL; every
                                           call is checked against the username/password below)
  admin_router  /admin/quickbooks/*        owner/admin only: status, "sync now", the connection
                                           file to download, and a browser for the saved records

What is saved: every QuickBooks record, whole, as JSON in the table qb_records (one row per
customer, invoice, item and so on). That is a mirror for looking at and reporting on; it is not
mixed into the products or orders tables.

How a sync runs (one "session"):
  1. authenticate    the Web Connector logs in; we decide whether there is anything to do and build
                     a plan: one step per kind of record (customers, invoices, ...).
  2. sendRequestXML  we hand over the next step as qbXML (one page of 100 records at a time).
  3. receiveResponseXML  we save the records, then either ask for the next page or move on.
  4. closeConnection we write the result.
  Lists (customers, items, accounts ...) are re-read in full every few hours, because balances and
  stock quantities change without the record's "modified" date changing. Transactions (invoices,
  payments ...) are read only from the day before the newest one we already have.

Settings (environment variables on the API):
    QBWC_USERNAME, QBWC_PASSWORD   what the Web Connector logs in with (you choose them)
    PUBLIC_API_URL                 optional, the API's public https address (otherwise taken from the request)
    QB_PAGE_SIZE                   records per request, default 100
    QB_FULL_REFRESH_HOURS          how often lists are fully re-read, default 6
    QB_MIN_MINUTES                 least time between scheduled syncs, default 10
    QBWC_RUN_EVERY_MINUTES         how often the connection file tells the Web Connector to check in, default 60
    QB_ENTITIES                    optional comma list to sync only some kinds, e.g. "Customer,Item"
"""
import hmac
import json
import logging
import os
import re
import secrets
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple, Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin
from app.database import get_db

log = logging.getLogger(__name__)

soap_router = APIRouter(tags=["quickbooks-web-connector"])
admin_router = APIRouter(prefix="/quickbooks", tags=["admin-quickbooks"], dependencies=[Depends(require_admin)])

SOAP_NS = "http://developer.intuit.com/"
MAX_BODY = 80 * 1024 * 1024  # QuickBooks answers can be large; nothing legitimate is bigger
MAX_QBXML = (13, 0)  # never ask for a newer qbXML dialect than this


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def page_size() -> int:
    return min(_env_int("QB_PAGE_SIZE", 100), 500)


def full_refresh_hours() -> int:
    return _env_int("QB_FULL_REFRESH_HOURS", 6)


def min_minutes() -> int:
    return _env_int("QB_MIN_MINUTES", 10)


def run_every_minutes() -> int:
    """Written into the connection file (.qwc). The Web Connector can also be changed by hand,
    in its own window, without a new file."""
    return _env_int("QBWC_RUN_EVERY_MINUTES", 60)


# --------------------------------------------------------------------------
# What we read
# --------------------------------------------------------------------------
class Spec(NamedTuple):
    entity: str  # our name for it (also the entity column in qb_records)
    query: str  # qbXML request name without "Rq"
    kind: str  # "list": re-read in full now and then | "txn": only what changed
    lines: bool = False  # ask for the line items too
    linked: bool = False  # ask for linked transactions too
    paged: bool = True  # False: small lists that do not take iterator / MaxReturned (QuickBooks rejects those with 0x80040400)


SPECS: list[Spec] = [
    # lists
    Spec("Customer", "CustomerQuery", "list"),
    Spec("Vendor", "VendorQuery", "list"),
    Spec("Item", "ItemQuery", "list"),  # every item type; see record_type
    Spec("Account", "AccountQuery", "list", paged=False),
    Spec("Class", "ClassQuery", "list", paged=False),
    Spec("StandardTerms", "StandardTermsQuery", "list", paged=False),
    Spec("DateDrivenTerms", "DateDrivenTermsQuery", "list", paged=False),
    Spec("SalesTaxCode", "SalesTaxCodeQuery", "list", paged=False),
    Spec("PriceLevel", "PriceLevelQuery", "list", paged=False),
    Spec("PaymentMethod", "PaymentMethodQuery", "list", paged=False),
    Spec("ShipMethod", "ShipMethodQuery", "list", paged=False),
    Spec("CustomerType", "CustomerTypeQuery", "list", paged=False),
    Spec("VendorType", "VendorTypeQuery", "list", paged=False),
    Spec("JobType", "JobTypeQuery", "list", paged=False),
    Spec("SalesRep", "SalesRepQuery", "list", paged=False),
    Spec("CustomerMsg", "CustomerMsgQuery", "list", paged=False),
    Spec("OtherName", "OtherNameQuery", "list", paged=False),
    # transactions
    Spec("Invoice", "InvoiceQuery", "txn", lines=True, linked=True),
    Spec("SalesReceipt", "SalesReceiptQuery", "txn", lines=True),
    Spec("CreditMemo", "CreditMemoQuery", "txn", lines=True, linked=True),
    Spec("ReceivePayment", "ReceivePaymentQuery", "txn", lines=True),
    Spec("Estimate", "EstimateQuery", "txn", lines=True, linked=True),
    Spec("SalesOrder", "SalesOrderQuery", "txn", lines=True, linked=True),
    Spec("Charge", "ChargeQuery", "txn"),
    Spec("PurchaseOrder", "PurchaseOrderQuery", "txn", lines=True, linked=True),
    Spec("ItemReceipt", "ItemReceiptQuery", "txn", lines=True, linked=True),
    Spec("Bill", "BillQuery", "txn", lines=True, linked=True),
    Spec("BillPaymentCheck", "BillPaymentCheckQuery", "txn", lines=True),
    Spec("BillPaymentCreditCard", "BillPaymentCreditCardQuery", "txn", lines=True),
    Spec("VendorCredit", "VendorCreditQuery", "txn", lines=True),
    Spec("Check", "CheckQuery", "txn", lines=True),
    Spec("CreditCardCharge", "CreditCardChargeQuery", "txn", lines=True),
    Spec("CreditCardCredit", "CreditCardCreditQuery", "txn", lines=True),
    Spec("Deposit", "DepositQuery", "txn", lines=True),
    Spec("JournalEntry", "JournalEntryQuery", "txn", lines=True),
    Spec("InventoryAdjustment", "InventoryAdjustmentQuery", "txn", lines=True),
    Spec("BuildAssembly", "BuildAssemblyQuery", "txn"),
]
SPEC_BY_ENTITY = {s.entity: s for s in SPECS}
TXN_TYPES = [s.entity for s in SPECS if s.kind == "txn"]  # names QuickBooks uses for deleted-transaction queries


def _active_specs() -> list[Spec]:
    wanted = {w.strip() for w in (os.environ.get("QB_ENTITIES") or "").split(",") if w.strip()}
    if not wanted:
        return SPECS
    return [s for s in SPECS if s.entity in wanted]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _j(v: Any) -> Any:
    """jsonb columns normally come back as Python objects; be safe if one comes back as text."""
    return json.loads(v) if isinstance(v, str) else v


def _dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _from_date(last_modified: Optional[str]) -> Optional[str]:
    """Day to start reading from: the day before the newest record we have (re-reading a day is harmless)."""
    d = _dt(last_modified)
    return (d - timedelta(days=1)).date().isoformat() if d else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


_UNSAFE_XML = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _parse_xml(data: bytes | str) -> ET.Element:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    # No DTDs or entities: this endpoint is reachable from the internet, and expanding entities
    # is how "billion laughs" style attacks work. Real QuickBooks traffic never contains them.
    if _UNSAFE_XML.search(raw):
        raise ValueError("DTD/entity declarations are not allowed")
    # drop the <?qbxml ...?> processing instruction's effect on the parser by parsing as-is
    return ET.fromstring(raw)


# ---- qbXML -> Python ------------------------------------------------------
_ALWAYS_LIST = {"LinkedTxn"}


def _to_obj(el: ET.Element) -> Any:
    kids = list(el)
    if not kids:
        return (el.text or "").strip()
    groups: dict[str, list[Any]] = {}
    for k in kids:
        groups.setdefault(_local(k.tag), []).append(_to_obj(k))
    out: dict[str, Any] = {}
    for tag, vals in groups.items():
        listy = len(vals) > 1 or tag.endswith("Ret") or tag in _ALWAYS_LIST
        out[tag] = vals if listy else vals[0]
    return out


def _first(d: dict, *keys: str) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _num(v: Optional[str]) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def _amount(o: dict) -> Optional[Decimal]:
    direct = _num(_first(o, "TotalAmount", "Amount", "AmountDue"))
    if direct is not None:
        return direct
    sub = _num(_first(o, "Subtotal"))
    if sub is not None:  # invoices: subtotal plus tax
        return sub + (_num(_first(o, "SalesTaxTotal")) or Decimal(0))
    return _num(_first(o, "Balance", "TotalBalance"))


def _row(entity: str, ret_tag: str, o: dict) -> Optional[dict]:
    qb_id = "company" if entity == "Company" else _first(o, "ListID", "TxnID")
    if not qb_id:
        return None
    party = None
    for key in ("CustomerRef", "VendorRef", "PayeeEntityRef", "EntityRef"):
        ref = o.get(key)
        if isinstance(ref, dict) and _first(ref, "FullName"):
            party = _first(ref, "FullName")
            break
    when = _first(o, "TxnDate")
    if when and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
        when = None
    amt = _amount(o)
    payload = json.dumps({"_type": ret_tag, **o}).replace("\\u0000", "")
    return {
        "entity": entity,
        "qb_id": qb_id,
        "rt": ret_tag,
        "name": _first(o, "FullName", "Name", "RefNumber", "CompanyName"),
        "party": party,
        "txn_date": when,
        "amount": str(amt) if amt is not None else None,
        "modified_at": _first(o, "TimeModified"),
        "data": payload,
    }


# --------------------------------------------------------------------------
# SOAP plumbing
# --------------------------------------------------------------------------
def _envelope(method: str, result_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        f'<soap:Body><{method}Response xmlns="{SOAP_NS}">{result_xml}</{method}Response></soap:Body></soap:Envelope>'
    )


def _fault(message: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><soap:Fault>'
        f"<faultcode>soap:Client</faultcode><faultstring>{escape(message)}</faultstring>"
        "</soap:Fault></soap:Body></soap:Envelope>"
    )


def _res(method: str, value: Any) -> str:
    return f"<{method}Result>{escape(str(value))}</{method}Result>"


def _auth_result(ticket: str, second: str) -> str:
    return f"<authenticateResult><string>{escape(ticket)}</string><string>{escape(second)}</string></authenticateResult>"


def _creds_ok(user: str, password: str) -> bool:
    want_u, want_p = os.environ.get("QBWC_USERNAME", ""), os.environ.get("QBWC_PASSWORD", "")
    if not want_u or not want_p:
        return False
    ok_u = hmac.compare_digest(user.encode(), want_u.encode())
    ok_p = hmac.compare_digest(password.encode(), want_p.encode())
    return ok_u and ok_p


# --------------------------------------------------------------------------
# database helpers
# --------------------------------------------------------------------------
def _control(db: Session):
    db.execute(text("insert into qb_control (id) values (1) on conflict (id) do nothing"))
    return db.execute(text("select sync_requested, full_resync from qb_control where id = 1")).first()


def _run(db: Session, ticket: str, open_only: bool = True):
    if not ticket:
        return None
    extra = "and finished_at is null and started_at > now() - interval '12 hours'" if open_only else ""
    return db.execute(text(f"select * from qb_runs where ticket = :t {extra}"), {"t": ticket}).first()


def _add_error(db: Session, run_id: int, entity: Optional[str], message: str, code: Optional[str] = None) -> None:
    err = {"entity": entity, "code": code, "message": message[:500], "at": datetime.now(timezone.utc).isoformat()}
    db.execute(
        text("update qb_runs set errors = errors || cast(:e as jsonb) where id = :id"),
        {"e": json.dumps([err]), "id": run_id},
    )


def _build_plan(db: Session, full: bool) -> list[dict]:
    state = {r.entity: r for r in db.execute(text("select * from qb_sync_state")).all()}
    now = datetime.now(timezone.utc)
    stale_after = timedelta(hours=full_refresh_hours())
    plan: list[dict] = [{"t": "company", "entity": "Company"}]
    any_previous = False
    for s in _active_specs():
        st = state.get(s.entity)
        if st and st.last_success_at:
            any_previous = True
        from_ = _from_date(st.last_modified) if st else None
        if full or not st or not st.last_success_at or not from_:
            mode, from_ = "full", None
        elif s.kind == "list" and (not st.last_full_at or now - st.last_full_at > stale_after):
            mode, from_ = "full", None
        else:
            mode = "inc"
        plan.append({"t": "q", "entity": s.entity, "mode": mode, "from": from_, "n": 0, "max_mod": None})
    # Transactions QuickBooks has deleted since the last time (only meaningful after a first read).
    if any_previous and not full:
        active_txn = [s.entity for s in _active_specs() if s.kind == "txn"]
        if active_txn:
            since = (now - timedelta(days=3)).date().isoformat()
            plan.append({"t": "del", "types": active_txn, "from": since})
    return plan


def _should_run(db: Session) -> tuple[bool, bool, str]:
    ctrl = _control(db)
    if ctrl.sync_requested:
        return True, bool(ctrl.full_resync), "manual"
    last = db.execute(
        text("select max(started_at) from qb_runs where status in ('ok', 'partial')")
    ).scalar()
    if not last or datetime.now(timezone.utc) - last > timedelta(minutes=min_minutes()):
        return True, False, "schedule"
    return False, False, ""


# --------------------------------------------------------------------------
# the Web Connector calls
# --------------------------------------------------------------------------
def m_serverVersion(db: Session, a: dict) -> str:
    return _res("serverVersion", "E&I QuickBooks sync 1.0")


def m_clientVersion(db: Session, a: dict) -> str:
    return _res("clientVersion", "")  # any Web Connector version is fine


def m_authenticate(db: Session, a: dict) -> str:
    if not _creds_ok(a.get("strUserName", ""), a.get("strPassword", "")):
        return _auth_result("", "nvu")  # not a valid user
    # A session that stopped talking to us (PC went to sleep, QuickBooks crashed) must not block forever.
    db.execute(
        text(
            "update qb_runs set status = 'failed', finished_at = now(), "
            "errors = errors || cast(:e as jsonb) "
            "where finished_at is null and started_at < now() - interval '30 minutes'"
        ),
        {"e": json.dumps([{"entity": None, "code": None, "message": "Session abandoned: the Web Connector stopped responding."}])},
    )
    if db.execute(text("select 1 from qb_runs where finished_at is null limit 1")).first():
        db.commit()
        return _auth_result("", "busy")
    go, full, trigger = _should_run(db)
    if not go:
        db.commit()
        return _auth_result(secrets.token_urlsafe(8), "none")  # nothing to do right now
    plan = _build_plan(db, full)
    ticket = secrets.token_urlsafe(24)
    db.execute(
        text(
            "insert into qb_runs (ticket, trigger, full_resync, plan) values (:t, :trg, :f, cast(:p as jsonb))"
        ),
        {"t": ticket, "trg": trigger, "f": full, "p": json.dumps(plan)},
    )
    db.execute(text("update qb_control set sync_requested = false, full_resync = false where id = 1"))
    db.commit()
    return _auth_result(ticket, "")  # "" = use the company file that is open in QuickBooks


def _request_xml(step: dict, idx: int, iterator_id: Optional[str], page: int, version: str) -> str:
    rid = idx + 1
    t = step["t"]
    if t == "company":
        body = f'<CompanyQueryRq requestID="{rid}"/>'
    elif t == "del":
        types = "".join(f"<TxnDelType>{escape(x)}</TxnDelType>" for x in step["types"])
        rng = (
            f"<DeletedDateRangeFilter><FromDeletedDate>{escape(step['from'])}</FromDeletedDate></DeletedDateRangeFilter>"
            if step.get("from")
            else ""
        )
        body = f'<TxnDeletedQueryRq requestID="{rid}">{types}{rng}</TxnDeletedQueryRq>'
    else:
        spec = SPEC_BY_ENTITY[step["entity"]]
        plain = spec.kind == "list" and (not spec.paged or bool(step.get("plain")))
        if plain:  # one answer with everything, no paging
            it, inner = "", ""
        else:
            it = f' iterator="Continue" iteratorID="{escape(iterator_id)}"' if iterator_id else ' iterator="Start"'
            inner = f"<MaxReturned>{page}</MaxReturned>"
        if spec.kind == "list":
            inner += "<ActiveStatus>All</ActiveStatus>"  # otherwise inactive records are left out
            if step.get("from"):
                inner += f"<FromModifiedDate>{escape(step['from'])}</FromModifiedDate>"
        else:
            if step.get("from"):
                inner += (
                    "<ModifiedDateRangeFilter>"
                    f"<FromModifiedDate>{escape(step['from'])}</FromModifiedDate>"
                    "</ModifiedDateRangeFilter>"
                )
            if spec.lines:
                inner += "<IncludeLineItems>true</IncludeLineItems>"
            if spec.linked:
                inner += "<IncludeLinkedTxns>true</IncludeLinkedTxns>"
        body = f'<{spec.query}Rq requestID="{rid}"{it}>{inner}</{spec.query}Rq>'
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<?qbxml version="{version}"?>'
        f'<QBXML><QBXMLMsgsRq onError="continueOnError">{body}</QBXMLMsgsRq></QBXML>'
    )


def m_sendRequestXML(db: Session, a: dict) -> str:
    run = _run(db, a.get("ticket", ""))
    if not run:
        return _res("sendRequestXML", "")
    plan, step = _j(run.plan), run.step
    if step >= len(plan):
        return _res("sendRequestXML", "")  # nothing left: ends the session
    try:
        reported = (int(a.get("qbXMLMajorVers") or 0), int(a.get("qbXMLMinorVers") or 0))
    except ValueError:
        reported = MAX_QBXML
    v = min(reported, MAX_QBXML) if reported != (0, 0) else MAX_QBXML
    version = f"{v[0]}.{v[1]}"
    xml = _request_xml(plan[step], step, run.iterator_id, page_size(), version)
    db.execute(
        text("update qb_runs set company_file = :c, qbxml_version = :v where id = :id"),
        {"c": (a.get("strCompanyFileName") or "")[:500], "v": version, "id": run.id},
    )
    db.commit()
    return _res("sendRequestXML", xml)


_UPSERT = text(
    """
    insert into qb_records (entity, qb_id, record_type, name, party, txn_date, amount, modified_at, data, synced_at, deleted_at)
    values (:entity, :qb_id, :rt, :name, :party, cast(:txn_date as date), cast(:amount as numeric),
            cast(:modified_at as timestamptz), cast(:data as jsonb), now(), null)
    on conflict (entity, qb_id) do update set
      record_type = excluded.record_type, name = excluded.name, party = excluded.party,
      txn_date = excluded.txn_date, amount = excluded.amount, modified_at = excluded.modified_at,
      data = excluded.data, synced_at = now(), deleted_at = null
    """
)


def _finish_step_ok(db: Session, run, step: dict) -> None:
    """A step read everything it asked for: update the marks and, after a full read, mark
    records QuickBooks no longer has."""
    if step["t"] != "q":
        return
    ent = step["entity"]
    full = step["mode"] == "full"
    if full:
        db.execute(
            text("update qb_records set deleted_at = now() where entity = :e and deleted_at is null and synced_at < :s"),
            {"e": ent, "s": run.started_at},
        )
    prev = db.execute(text("select last_modified from qb_sync_state where entity = :e"), {"e": ent}).scalar()
    cands = [d for d in (_dt(prev), _dt(step.get("max_mod"))) if d]
    newest = max(cands).isoformat() if cands else None
    db.execute(
        text(
            """
            insert into qb_sync_state (entity, last_modified, last_full_at, last_success_at, last_error, record_count)
            values (:e, :lm, case when :full then now() end, now(), null,
                    (select count(*) from qb_records where entity = :e and deleted_at is null))
            on conflict (entity) do update set
              last_modified = excluded.last_modified,
              last_full_at = case when :full then now() else qb_sync_state.last_full_at end,
              last_success_at = now(), last_error = null,
              record_count = excluded.record_count
            """
        ),
        {"e": ent, "lm": newest, "full": full},
    )


def _process(db: Session, run, xml: str) -> int:
    plan, i = _j(run.plan), run.step
    step = plan[i]
    root = _parse_xml(xml)
    rs = next((e for e in root.iter() if _local(e.tag) == "QBXMLMsgsRs"), None)
    resp = next((c for c in rs if _local(c.tag).endswith("Rs")), None) if rs is not None else None
    if resp is None:
        raise ValueError("QuickBooks sent an answer with no response element")

    code = int(resp.attrib.get("statusCode", "0") or 0)
    severity = resp.attrib.get("statusSeverity", "")
    message = resp.attrib.get("statusMessage", "")
    fine = code in (0, 1) or severity == "Warn"  # 1 = "nothing matched", which is not a problem
    ent = step.get("entity") or "TxnDeleted"
    counts = _j(run.counts) or {}

    if not fine:
        # 3250 = "this feature is not available in your edition of QuickBooks" (for example
        # assemblies in Pro). That is normal, so it is noted on the entity, not counted as a failure.
        if code != 3250:
            _add_error(db, run.id, ent, message or "QuickBooks refused the request", str(code))
        if step["t"] == "q":
            db.execute(
                text(
                    "insert into qb_sync_state (entity, last_error) values (:e, :m) "
                    "on conflict (entity) do update set last_error = excluded.last_error"
                ),
                {"e": ent, "m": f"{code}: {message}"[:500]},
            )
        # skip this step and carry on with the next one
        return _advance(db, run, plan, i, counts, failed=True)

    rets = [c for c in resp if _local(c.tag).endswith("Ret")]
    if step["t"] == "del":
        for el in rets:
            o = _to_obj(el)
            if isinstance(o, dict) and _first(o, "TxnID") and _first(o, "TxnDelType"):
                db.execute(
                    text("update qb_records set deleted_at = now() where entity = :e and qb_id = :i"),
                    {"e": _first(o, "TxnDelType"), "i": _first(o, "TxnID")},
                )
        step["n"] = int(step.get("n", 0)) + len(rets)
    else:
        rows = []
        for el in rets:
            o = _to_obj(el)
            if not isinstance(o, dict):
                continue
            r = _row(ent, _local(el.tag), o)
            if r:
                rows.append(r)
                m = _dt(r["modified_at"])
                if m and (not _dt(step.get("max_mod")) or m > _dt(step["max_mod"])):
                    step["max_mod"] = r["modified_at"]
        if rows:
            db.execute(_UPSERT, rows)
        step["n"] = int(step.get("n", 0)) + len(rows)

    remaining = int(resp.attrib.get("iteratorRemainingCount", "0") or 0)
    iid = resp.attrib.get("iteratorID")
    if remaining > 0 and iid:  # more pages: ask again, same step
        plan[i] = step
        db.execute(
            text("update qb_runs set plan = cast(:p as jsonb), iterator_id = :it where id = :id"),
            {"p": json.dumps(plan), "it": iid, "id": run.id},
        )
        db.commit()
        return min(99, int(100 * i / max(1, len(plan))))

    _finish_step_ok(db, run, step)
    counts[ent] = step.get("n", 0)
    plan[i] = step
    return _advance(db, run, plan, i, counts, failed=False)


def _advance(db: Session, run, plan: list, i: int, counts: dict, failed: bool) -> int:
    nxt = i + 1
    db.execute(
        text(
            "update qb_runs set plan = cast(:p as jsonb), step = :s, iterator_id = null, "
            "counts = cast(:c as jsonb) where id = :id"
        ),
        {"p": json.dumps(plan), "s": nxt, "c": json.dumps(counts), "id": run.id},
    )
    db.commit()
    return 100 if nxt >= len(plan) else min(99, int(100 * nxt / len(plan)))


def m_receiveResponseXML(db: Session, a: dict) -> str:
    run = _run(db, a.get("ticket", ""))
    if not run:
        return _res("receiveResponseXML", -1)
    if a.get("hresult"):
        # QuickBooks or the Web Connector itself could not run the request (file closed, a dialog is open ...)
        hr = str(a.get("hresult"))
        plan, i = _j(run.plan), run.step
        step = plan[i] if 0 <= i < len(plan) else {}
        ent = step.get("entity") or ("TxnDeleted" if step.get("t") == "del" else None)
        if hr.lower() == "0x80040400" and step.get("t") == "q" and not step.get("plain") and SPEC_BY_ENTITY[ent].kind == "list":
            # Some small lists do not accept paging. Ask once more for the whole list in one answer before giving up.
            step["plain"] = True
            plan[i] = step
            db.execute(
                text("update qb_runs set plan = cast(:p as jsonb), iterator_id = null where id = :id"),
                {"p": json.dumps(plan), "id": run.id},
            )
            db.commit()
            return _res("receiveResponseXML", min(99, int(100 * i / max(1, len(plan)))))
        _add_error(db, run.id, ent, f"{hr}: {a.get('message', '')}", "hresult")
        if hr.lower() == "0x80040400" and ent:
            # QuickBooks could not understand our request for this one record type. Note it, skip it and carry
            # on, so one bad request does not stop everything after it (and fail the same way on every retry).
            if step.get("t") == "q":
                db.execute(
                    text(
                        "insert into qb_sync_state (entity, last_error) values (:e, :m) "
                        "on conflict (entity) do update set last_error = excluded.last_error"
                    ),
                    {"e": ent, "m": f"{hr}: {a.get('message', '')}"[:500]},
                )
            percent = _advance(db, run, plan, i, _j(run.counts) or {}, failed=True)
            return _res("receiveResponseXML", percent)
        db.commit()
        return _res("receiveResponseXML", -1)
    try:
        percent = _process(db, run, a.get("response", ""))
    except Exception as e:  # never let one bad answer wedge the session
        db.rollback()
        log.exception("could not process a QuickBooks answer")
        _add_error(db, run.id, None, f"Could not read QuickBooks' answer: {e}", "parse")
        db.commit()
        return _res("receiveResponseXML", -1)
    return _res("receiveResponseXML", percent)


def m_getLastError(db: Session, a: dict) -> str:
    run = _run(db, a.get("ticket", ""), open_only=False)
    errs = _j(run.errors) if run else []
    return _res("getLastError", errs[-1]["message"] if errs else "")


def m_closeConnection(db: Session, a: dict) -> str:
    run = _run(db, a.get("ticket", ""), open_only=False)
    if run and run.finished_at is None:
        plan, errs = _j(run.plan), _j(run.errors)
        finished_all = run.step >= len(plan)
        status = "failed" if not finished_all else ("partial" if errs else "ok")
        db.execute(
            text("update qb_runs set status = :s, finished_at = now() where id = :id"),
            {"s": status, "id": run.id},
        )
        db.commit()
        log.info("QuickBooks sync %s finished: %s (%s)", run.id, status, _j(run.counts))
    return _res("closeConnection", "OK")


def m_connectionError(db: Session, a: dict) -> str:
    run = _run(db, a.get("ticket", ""), open_only=False)
    if run and run.finished_at is None:
        _add_error(db, run.id, None, f"{a.get('hresult', '')}: {a.get('message', '')}", "connection")
        db.execute(text("update qb_runs set status = 'failed', finished_at = now() where id = :id"), {"id": run.id})
        db.commit()
    return _res("connectionError", "done")  # do not retry now


def m_getInteractiveURL(db: Session, a: dict) -> str:
    return _res("getInteractiveURL", "")


def m_interactiveDone(db: Session, a: dict) -> str:
    return _res("interactiveDone", "Done")


def m_interactiveRejected(db: Session, a: dict) -> str:
    return _res("interactiveRejected", "Interactive mode is not used")


_METHODS = {
    "serverVersion": m_serverVersion,
    "clientVersion": m_clientVersion,
    "authenticate": m_authenticate,
    "sendRequestXML": m_sendRequestXML,
    "receiveResponseXML": m_receiveResponseXML,
    "getLastError": m_getLastError,
    "closeConnection": m_closeConnection,
    "connectionError": m_connectionError,
    "getInteractiveURL": m_getInteractiveURL,
    "interactiveDone": m_interactiveDone,
    "interactiveRejected": m_interactiveRejected,
}


def handle_soap(db: Session, body: bytes) -> str:
    """One Web Connector call in, one SOAP answer out."""
    if len(body) > MAX_BODY:
        return _fault("Request too large")
    try:
        root = _parse_xml(body)
        soap_body = next(e for e in root.iter() if _local(e.tag) == "Body")
        call = list(soap_body)[0]
    except (ValueError, StopIteration, IndexError, ET.ParseError):
        return _fault("Not a valid SOAP request")
    name = _local(call.tag)
    fn = _METHODS.get(name)
    if not fn:
        return _fault(f"Unknown method {name}")
    args = {_local(c.tag): (c.text or "") for c in call}
    try:
        return _envelope(name, fn(db, args))
    except Exception:
        db.rollback()
        log.exception("QuickBooks Web Connector call %s failed", name)
        return _fault("The server could not handle that request")


@soap_router.get("/qbwc")
def qbwc_info() -> Response:
    return Response("E&I Motorsports QuickBooks Web Connector endpoint.", media_type="text/plain")


@soap_router.post("/qbwc")
async def qbwc(request: Request, db: Session = Depends(get_db)) -> Response:
    body = await request.body()
    xml = await run_in_threadpool(handle_soap, db, body)
    return Response(xml, media_type="text/xml; charset=utf-8")


# --------------------------------------------------------------------------
# admin API
# --------------------------------------------------------------------------
def _tables_ready(db: Session) -> bool:
    return bool(db.execute(text("select to_regclass('public.qb_records') is not null and to_regclass('public.qb_runs') is not null")).scalar())


def _need_tables(db: Session) -> None:
    if not _tables_ready(db):
        raise HTTPException(409, "The QuickBooks tables have not been created yet. Run qb_tables.sql in Supabase first.")


def _public_url(request: Request) -> str:
    env = (os.environ.get("PUBLIC_API_URL") or "").strip().rstrip("/")
    if env:
        return env
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return f"https://{host}"


@admin_router.get("/status")
def status(request: Request, db: Session = Depends(get_db)):
    configured = bool(os.environ.get("QBWC_USERNAME") and os.environ.get("QBWC_PASSWORD"))
    out = {
        "tables_ready": _tables_ready(db),
        "credentials_set": configured,
        "username": os.environ.get("QBWC_USERNAME", ""),
        "endpoint": f"{_public_url(request)}/qbwc",
        "entities_filter": os.environ.get("QB_ENTITIES", ""),
        "full_refresh_hours": full_refresh_hours(),
        "run_every_minutes": run_every_minutes(),
    }
    if not out["tables_ready"]:
        return out
    out["control"] = dict(_control(db)._mapping)
    db.commit()
    out["entities"] = [
        dict(r._mapping)
        for r in db.execute(
            text(
                """
                select r.entity,
                       count(*) filter (where r.deleted_at is null) as records,
                       count(*) filter (where r.deleted_at is not null) as removed,
                       max(r.synced_at) as last_synced,
                       s.last_success_at, s.last_full_at, s.last_error
                from qb_records r left join qb_sync_state s on s.entity = r.entity
                group by r.entity, s.last_success_at, s.last_full_at, s.last_error
                order by r.entity
                """
            )
        ).all()
    ]
    out["errors_by_entity"] = [
        dict(r._mapping)
        for r in db.execute(text("select entity, last_error from qb_sync_state where last_error is not null order by entity")).all()
    ]
    last = db.execute(
        text("select id, started_at, finished_at, status, trigger, full_resync, step, jsonb_array_length(plan) as steps, company_file, qbxml_version, counts, errors from qb_runs order by id desc limit 1")
    ).first()
    out["last_run"] = dict(last._mapping) if last else None
    return out


@admin_router.get("/runs")
def runs(limit: int = Query(20, le=100), db: Session = Depends(get_db)):
    _need_tables(db)
    rows = db.execute(
        text(
            "select id, started_at, finished_at, status, trigger, full_resync, step, jsonb_array_length(plan) as steps, "
            "counts, errors from qb_runs order by id desc limit :n"
        ),
        {"n": limit},
    ).all()
    return [dict(r._mapping) for r in rows]


class SyncIn(BaseModel):
    full: bool = False


@admin_router.post("/sync-now")
def sync_now(body: SyncIn, db: Session = Depends(get_db), user: AuthUser = Depends(require_admin)):
    _need_tables(db)
    _control(db)
    db.execute(
        text(
            "update qb_control set sync_requested = true, full_resync = :f, requested_by = :u, requested_at = now() where id = 1"
        ),
        {"f": body.full, "u": user.email or user.user_id},
    )
    db.commit()
    return {"ok": True, "full": body.full}


@admin_router.get("/qwc")
def qwc(request: Request):
    """The small connection file the Web Connector loads once.

    IsReadOnly must be false: when the Web Connector registers the app it writes a small marker into the company
    file, and QuickBooks refuses that for a read-only app (error 3263 / QBWC1039 "Unable to add FileID"). The sync
    itself still only sends queries and never changes the books."""
    username = os.environ.get("QBWC_USERNAME", "")
    if not username or not os.environ.get("QBWC_PASSWORD"):
        raise HTTPException(409, "Set QBWC_USERNAME and QBWC_PASSWORD on the API first, then download this file.")
    base = _public_url(request)
    owner = str(uuid.uuid5(uuid.NAMESPACE_URL, "ei-motorsports/qbwc/owner/v2")).upper()
    file_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "ei-motorsports/qbwc/file/v2")).upper()
    xml = f"""<?xml version="1.0"?>
<QBWCXML>
  <AppName>E&amp;I Motorsports</AppName>
  <AppID></AppID>
  <AppURL>{escape(base)}/qbwc</AppURL>
  <AppDescription>Copies QuickBooks data to the E&amp;I Motorsports back office. It only reads your data and never changes your books.</AppDescription>
  <AppSupport>{escape(base)}/qbwc</AppSupport>
  <UserName>{escape(username)}</UserName>
  <OwnerID>{{{owner}}}</OwnerID>
  <FileID>{{{file_id}}}</FileID>
  <QBType>QBFS</QBType>
  <Scheduler>
    <RunEveryNMinutes>{run_every_minutes()}</RunEveryNMinutes>
  </Scheduler>
  <IsReadOnly>false</IsReadOnly>
</QBWCXML>
"""
    return {"filename": "EI-Motorsports.qwc", "xml": xml, "endpoint": f"{base}/qbwc"}


@admin_router.get("/records")
def records(
    entity: Optional[str] = None,
    q: Optional[str] = None,
    removed: bool = False,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    _need_tables(db)
    where = ["1 = 1"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if entity:
        where.append("entity = :entity")
        params["entity"] = entity
    where.append("deleted_at is not null" if removed else "deleted_at is null")
    for i, w in enumerate([w for w in re.split(r"\s+", (q or "").strip()) if w][:6]):
        where.append(
            f"(name ilike cast(:w{i} as text) or party ilike cast(:w{i} as text) or qb_id ilike cast(:w{i} as text))"
        )
        params[f"w{i}"] = f"%{w}%"
    w_sql = " and ".join(where)
    total = db.execute(text(f"select count(*) from qb_records where {w_sql}"), params).scalar()
    rows = db.execute(
        text(
            f"""
            select entity, qb_id, record_type, name, party, txn_date, amount, modified_at, synced_at
            from qb_records where {w_sql}
            order by txn_date desc nulls last, name nulls last, qb_id
            limit :limit offset :offset
            """
        ),
        params,
    ).all()
    return {"total": total, "items": [dict(r._mapping) for r in rows]}


@admin_router.get("/records/{entity}/{qb_id}")
def record(entity: str, qb_id: str, db: Session = Depends(get_db)):
    _need_tables(db)
    row = db.execute(
        text("select entity, qb_id, record_type, name, party, txn_date, amount, modified_at, synced_at, deleted_at, data from qb_records where entity = :e and qb_id = :i"),
        {"e": entity, "i": qb_id},
    ).first()
    if not row:
        raise HTTPException(404, "No such record")
    d = dict(row._mapping)
    d["data"] = _j(d["data"])
    return d


# ==========================================================================
# QuickBooks customers -> the store's `customers` table
# ==========================================================================
# Re-runnable: a customer is "new" only if no store row has its QuickBooks ListID and none has its email.
# Existing store customers with the same email are linked (ListID stored) and only get BLANK fields filled in;
# nothing already in the store table is ever overwritten. Customers without a usable email cannot be stored
# (customers.email is required), and QuickBooks "jobs" (sub-entries under a customer) are skipped.
#
# Masked marketplace addresses (eBay, Amazon ...) are stored in `email` like any other (the column is required and
# unique), but tagged email_kind = 'masked' and marketplace = 'ebay' / 'amazon', so they sort and filter apart from
# real ones. Imported rows are never opted in to marketing.
MASKED_EMAIL_DOMAINS = {
    "members.ebay.com": "ebay",
    "marketplace.amazon.com": "amazon",
    "privaterelay.appleid.com": "apple_relay",  # Apple "Hide My Email": a forwarding alias, not the person's own address
}
_EMAIL_OK = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_TS_OK = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)?$")
_CUST_COLS = ("qb_list_id", "source", "email_kind", "marketplace")


def _classify_email(raw: Optional[str]) -> tuple[Optional[str], str, Optional[str]]:
    """(clean email or None, 'real' | 'masked', marketplace or None)."""
    e = (raw or "").strip().lower()
    if not e or not _EMAIL_OK.match(e):
        return None, "real", None
    mkt = MASKED_EMAIL_DOMAINS.get(e.rsplit("@", 1)[1])
    return e, ("masked" if mkt else "real"), mkt


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _store_address(o: dict, full_name: Optional[str], phone: Optional[str]) -> Optional[dict]:
    a = o.get("ShipAddress") if isinstance(o.get("ShipAddress"), dict) else None
    if not a or not any(_s(v) for v in a.values()):
        a = o.get("BillAddress") if isinstance(o.get("BillAddress"), dict) else None
    if not a:
        return None
    skip = {x.lower() for x in (full_name, _s(o.get("Name")), _s(o.get("CompanyName"))) if x}
    lines = [t for t in (_s(a.get(f"Addr{i}")) for i in range(1, 6)) if t and t.lower() not in skip]
    city, region, postal = _s(a.get("City")), _s(a.get("State")), _s(a.get("PostalCode"))
    if not lines or not (city or postal):
        return None
    c = _s(a.get("Country")).lower()
    country = c.upper() if len(c) == 2 else {"united states": "US", "usa": "US", "united states of america": "US", "canada": "CA"}.get(c)
    out = {"full_name": full_name, "line1": lines[0], "line2": lines[1] if len(lines) > 1 else None,
           "city": city or None, "region": region or None, "postal_code": postal or None, "phone": phone}
    if country:
        out["country"] = country
    return out


def _customer_row(r) -> dict:
    """One QuickBooks customer (a row from the chunk query) -> the fields we store."""
    fn, ln = _s(r.first), _s(r.last)
    name = " ".join(x for x in (fn, ln) if x) or _s(r.company) or _s(r.name) or None
    phone = _s(r.phone) or _s(r.mobile) or _s(r.alt) or None
    email, kind, mkt = _classify_email(r.email)
    o = {"ShipAddress": _j(r.ship), "BillAddress": _j(r.bill), "Name": r.name, "CompanyName": r.company}
    addr = _store_address(o, name, phone)
    created = _s(r.created)
    return {
        "lid": r.qb_id, "email": email, "raw_email": _s(r.email), "kind": kind, "mkt": mkt, "name": name, "phone": phone,
        "addr": json.dumps(addr) if addr else None, "created": created if _TS_OK.match(created) else None,
    }


def _customer_columns_ready(db: Session) -> bool:
    n = db.execute(
        text(
            "select count(*) from information_schema.columns where table_schema = 'public' "
            "and table_name = 'customers' and column_name = any(cast(:c as text[]))"
        ),
        {"c": list(_CUST_COLS)},
    ).scalar()
    return n == len(_CUST_COLS)


def _customers_to_store(db: Session, dry_run: bool, limit: int) -> dict:
    stats: dict[str, Any] = {
        "dry_run": dry_run, "qb_customers": 0, "jobs_skipped": 0, "no_email": 0, "bad_email": 0, "duplicate_email_in_qb": 0,
        "already_imported": 0, "matched_existing": 0, "new_real": 0, "new_masked": {}, "added": 0, "linked": 0,
        "stopped_at_limit": False, "samples": {"new_real": [], "new_masked": [], "bad_email": []},
    }
    seen_emails: set[str] = set()
    last = ""
    new_total = 0
    while True:
        rows = db.execute(
            text(
                """
                select qb_id, data->>'Email' as email, data->>'FirstName' as first, data->>'LastName' as last,
                       data->>'CompanyName' as company, data->>'Name' as name, data->>'Phone' as phone,
                       data->>'Mobile' as mobile, data->>'AltPhone' as alt, data->>'TimeCreated' as created,
                       data->'ShipAddress' as ship, data->'BillAddress' as bill,
                       case when (data->>'Sublevel') ~ '^[0-9]+$' then cast(data->>'Sublevel' as int) else 0 end as sublevel
                from qb_records
                where entity = 'Customer' and deleted_at is null and qb_id > :last
                order by qb_id limit 2000
                """
            ),
            {"last": last},
        ).all()
        if not rows:
            break
        last = rows[-1].qb_id
        cands = []
        for r in rows:
            stats["qb_customers"] += 1
            if r.sublevel > 0:
                stats["jobs_skipped"] += 1
                continue
            c = _customer_row(r)
            if not c["email"]:
                if c["raw_email"]:
                    stats["bad_email"] += 1
                    if len(stats["samples"]["bad_email"]) < 5:
                        stats["samples"]["bad_email"].append(c["raw_email"][:80])
                else:
                    stats["no_email"] += 1
                continue
            cands.append(c)
        if not cands:
            continue
        have = db.execute(
            text(
                "select id, lower(email) as e, qb_list_id from customers "
                "where lower(email) = any(cast(:e as text[])) or qb_list_id = any(cast(:l as text[]))"
            ),
            {"e": [c["email"] for c in cands], "l": [c["lid"] for c in cands]},
        ).all()
        by_email = {h.e: h for h in have}
        by_lid = {h.qb_list_id for h in have if h.qb_list_id}
        to_add, to_link = [], []
        for c in cands:
            if c["lid"] in by_lid:
                stats["already_imported"] += 1
                seen_emails.add(c["email"])
                continue
            if c["email"] in seen_emails:
                stats["duplicate_email_in_qb"] += 1
                continue
            seen_emails.add(c["email"])
            h = by_email.get(c["email"])
            if h is not None:
                stats["matched_existing"] += 1
                if h.qb_list_id is None:
                    to_link.append({**c, "id": h.id})
                continue
            if not dry_run and new_total >= limit:
                stats["stopped_at_limit"] = True
                continue
            new_total += 1
            to_add.append(c)
            if c["kind"] == "masked":
                stats["new_masked"][c["mkt"]] = stats["new_masked"].get(c["mkt"], 0) + 1
                if len(stats["samples"]["new_masked"]) < 5:
                    stats["samples"]["new_masked"].append({"email": c["email"], "name": c["name"], "marketplace": c["mkt"]})
            else:
                stats["new_real"] += 1
                if len(stats["samples"]["new_real"]) < 5:
                    stats["samples"]["new_real"].append({"email": c["email"], "name": c["name"]})
        if not dry_run:
            if to_add:
                db.execute(
                    text(
                        "insert into customers (email, full_name, phone, last_shipping_address, marketing_opt_in, "
                        "created_at, source, qb_list_id, email_kind, marketplace) "
                        "values (:email, :name, :phone, cast(:addr as jsonb), false, coalesce(cast(:created as timestamptz), now()), "
                        "'quickbooks', :lid, :kind, :mkt) on conflict ((lower(email))) do nothing"
                    ),
                    to_add,
                )
                stats["added"] += len(to_add)
            if to_link:
                db.execute(
                    text(
                        "update customers set qb_list_id = :lid, full_name = coalesce(nullif(full_name, ''), :name), "
                        "phone = coalesce(nullif(phone, ''), :phone), "
                        "last_shipping_address = coalesce(last_shipping_address, cast(:addr as jsonb)), updated_at = now() "
                        "where id = :id and qb_list_id is null"
                    ),
                    to_link,
                )
                stats["linked"] += len(to_link)
            db.commit()
    stats["new_masked_total"] = sum(stats["new_masked"].values())
    return stats


class CustToStoreIn(BaseModel):
    dry_run: bool = True
    limit: int = 20000


@admin_router.post("/customers-to-store")
def customers_to_store(body: CustToStoreIn, db: Session = Depends(get_db)):
    """Preview (dry_run true, the default) or perform the copy of QuickBooks customers into the store's customers."""
    _need_tables(db)
    if not _customer_columns_ready(db):
        raise HTTPException(409, "The customers table does not have the QuickBooks columns yet (qb_list_id, source, email_kind, marketplace).")
    limit = max(1, min(body.limit, 50000))
    try:
        return _customers_to_store(db, body.dry_run, limit)
    except Exception:
        db.rollback()
        log.exception("customer copy failed")
        raise HTTPException(500, "The customer copy stopped with an error. Nothing half-finished is kept; run it again.")
