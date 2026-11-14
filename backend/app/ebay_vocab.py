"""
eBay's own vehicle lists, for the compatibility editor (drafts and live listings).

Mounted at /admin/ebay-vocab/* (owner/admin only).

  GET  /properties?category_id=              the vehicle properties this category uses (Make, Model, Year, maybe Trim / Engine)
  GET  /values?category_id=&property=&filters={"Make":"Honda"}
                                             the values eBay accepts for one property, narrowed by the ones already chosen
  POST /convert   {product_id, category_id}  our fitment for that product as eBay compatibility rows, using eBay's lists

Why: eBay only accepts a vehicle that exists in ITS tables, and the tables we saved from older listings hold only what those
listings used. These calls ask eBay itself (Commerce Taxonomy API: get_compatibility_properties and
get_compatibility_property_values, eBay Motors US tree), so any make / model / year eBay knows can be added, not just
what was saved. It needs no seller approval, only the app keys already used by the About page. Answers are kept in memory
for a few hours.

Nothing here writes to eBay or to the database. The compatibility list is only sent when a draft is checked / listed or a
live listing is revised, and eBay's own check is the final word.
"""
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db
from app.ebay_feedback import FeedbackError, _token, configured

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay-vocab", tags=["admin-ebay-vocab"], dependencies=[Depends(require_admin)])

BASE = "https://api.ebay.com/commerce/taxonomy/v1/category_tree/{tree}/{method}"
TREES = ("100", "0")  # eBay Motors US first, then eBay US
CACHE_SECONDS = 6 * 3600
TOKEN_SECONDS = 90 * 60
MAX_VALUES = 2000

_cache: dict[tuple, tuple[float, object]] = {}
_tree_of: dict[str, str] = {}
_tok: list = [0.0, ""]


class VocabError(Exception):
    def __init__(self, status: int, message: str, error_id: Optional[int] = None):
        super().__init__(message)
        self.status, self.message, self.error_id = status, message, error_id


def _app_token() -> str:
    if time.time() < _tok[0] and _tok[1]:
        return _tok[1]
    _tok[1] = _token()
    _tok[0] = time.time() + TOKEN_SECONDS
    return _tok[1]


def _get(tree: str, method: str, params: dict) -> dict:
    url = BASE.format(tree=tree, method=method) + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + _app_token(), "Accept": "application/json",
                                               "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        if e.code == 401:
            _tok[0] = 0.0  # the cached token was refused; the next call asks for a new one
        msg, eid = f"eBay answered {e.code}.", None
        try:
            first = (json.loads(raw).get("errors") or [{}])[0]
            msg, eid = first.get("longMessage") or first.get("message") or msg, first.get("errorId")
        except Exception:
            pass
        raise VocabError(e.code, msg, eid)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise VocabError(502, f"Could not reach eBay ({e}).")


def _call(category_id: str, method: str, params: dict) -> dict:
    trees = [_tree_of[category_id]] if category_id in _tree_of else list(TREES)
    last: Optional[VocabError] = None
    for tree in trees:
        try:
            out = _get(tree, method, {"category_id": category_id, **params})
            _tree_of[category_id] = tree
            return out
        except VocabError as e:
            last = e
            if e.error_id in (62005, 62004):  # the category is not in this tree: try the next
                continue
            raise
    raise last or VocabError(502, "eBay did not answer.")


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace(",", "\\,")


def _cached(key: tuple, fn):
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    val = fn()
    _cache[key] = (time.time() + CACHE_SECONDS, val)
    return val


def properties(category_id: str) -> list[str]:
    def go():
        d = _call(category_id, "get_compatibility_properties", {})
        names = []
        for p in d.get("compatibilityProperties") or []:
            n = p.get("name") if isinstance(p, dict) else p
            if n:
                names.append(str(n))
        return names
    return _cached(("props", category_id), go)


def values(category_id: str, prop: str, filters: Optional[dict] = None) -> list[str]:
    filters = {k: str(v) for k, v in (filters or {}).items() if str(v).strip()}
    flt = ",".join(f"{k}:{_esc(v)}" for k, v in sorted(filters.items()))

    def go():
        params = {"compatibility_property": prop}
        if flt:
            params["filter"] = flt
        d = _call(category_id, "get_compatibility_property_values", params)
        out = []
        for v in d.get("compatibilityPropertyValues") or []:
            s = v.get("value") if isinstance(v, dict) else v
            if s is not None and str(s).strip():
                out.append(str(s).strip())
        return out[:MAX_VALUES]
    return _cached(("vals", category_id, prop, flt), go)


def _problem(e: Exception) -> HTTPException:
    if isinstance(e, VocabError):
        if e.error_id == 62101:
            return HTTPException(409, "This eBay category does not use a parts compatibility table.")
        return HTTPException(502 if e.status >= 500 else 409, f"eBay's vehicle list said: {e.message}")
    if isinstance(e, FeedbackError):
        return HTTPException(503, str(e))
    return HTTPException(500, "The vehicle list could not be read.")


@router.get("/properties")
def get_properties(category_id: str = Query(..., min_length=1, max_length=20)):
    if not configured():
        raise HTTPException(503, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API, so eBay's vehicle lists cannot be read.")
    try:
        return {"properties": properties(category_id.strip())}
    except Exception as e:
        raise _problem(e)


@router.get("/values")
def get_values(category_id: str = Query(..., min_length=1, max_length=20), property: str = Query(..., min_length=1, max_length=40),
               filters: str = Query("{}", max_length=1000)):
    try:
        f = json.loads(filters or "{}")
        if not isinstance(f, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "The filters must be a JSON object.")
    try:
        return {"values": values(category_id.strip(), property.strip(), f)}
    except Exception as e:
        raise _problem(e)


# --------------------------------------------------------------------------
# our fitment -> eBay rows, using eBay's own lists
# --------------------------------------------------------------------------
def nk(s: str) -> str:
    """Compare names without caring about case, spaces, dashes or dots (CR85R = CR-85 R)."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _pick(options: list[str], wanted: list[str]) -> Optional[str]:
    by = {}
    for o in options:
        by.setdefault(nk(o), o)
    for w in wanted:
        if nk(w) in by:
            return by[nk(w)]
    return None


def convert_with_ebay(db: Session, product_id: int, category_id: str, rows=None, names=None, vals=None, props=None) -> tuple[list[dict], list[str]]:
    """Our fitment as compatibility rows made only of values eBay lists for this category. Returns (rows, notes).

    Make and Model are matched with eBay's lists (the spellings the compat conversion mapped to our model first, then ours);
    Year must be one eBay lists for that make + model. Other properties (Trim, Engine) are not set. A model or year eBay
    does not list is left out and named in the notes."""
    from concurrent.futures import ThreadPoolExecutor

    from app.ebay_newdraft import MAX_COMPAT_ROWS, _ebay_names, _fitment_rows, _years_text
    rows = _fitment_rows(db, product_id) if rows is None else rows
    names = names or _ebay_names
    vals = vals or values
    props = props or properties
    have = props(category_id)
    year_name = next((p for p in have if p.lower() == "year"), "Year")
    make_name = next((p for p in have if p.lower() == "make"), "Make")
    model_name = next((p for p in have if p.lower() == "model"), "Model")

    groups: dict[tuple, set] = {}
    no_year = 0
    for r in rows:
        if r.year is None:
            no_year += 1
            continue
        groups.setdefault((r.model_id, r.make, r.model), set()).add(int(r.year))
    notes: list[str] = []
    if no_year:
        notes.append(f"{no_year} fitment {'entry has' if no_year == 1 else 'entries have'} no year, and eBay needs one, so {'it was' if no_year == 1 else 'they were'} left out.")
    makes = vals(category_id, make_name, {})
    out: list[dict] = []
    seen: set = set()
    # pass 1: match each of our models to eBay's make + model names (one list per make, cached)
    todo = []
    for (mid, make, model), years in sorted(groups.items(), key=lambda kv: (str(kv[0][1]), str(kv[0][2]))):
        cands = names(db, mid, make, model)
        ebay_make = _pick(makes, [m for m, _ in cands])
        if not ebay_make:
            notes.append(f"{make} {model}: eBay has no make called '{make}' for this category, so it was left out.")
            continue
        models = vals(category_id, model_name, {make_name: ebay_make})
        ebay_model = _pick(models, [mo for m, mo in cands if nk(m) == nk(ebay_make)] + [model])
        if not ebay_model:
            notes.append(f"{make} {model}: eBay has no model with that name under {ebay_make}, so it was left out.")
            continue
        todo.append((make, model, years, ebay_make, ebay_model))
    # pass 2: the years eBay lists for each model, several at a time (answers are cached for hours)
    def year_list(t):
        try:
            return set(vals(category_id, year_name, {make_name: t[3], model_name: t[4]}))
        except Exception as e:  # one model failing must not lose the rest
            return e

    with ThreadPoolExecutor(max_workers=8) as pool:
        year_sets = list(pool.map(year_list, todo))
    for (make, model, years, ebay_make, ebay_model), ok_years in zip(todo, year_sets):
        if isinstance(ok_years, Exception):
            notes.append(f"{make} {model}: eBay's year list could not be read ({ok_years}), so it was left out.")
            continue
        keep = sorted(y for y in years if str(y) in ok_years)
        missing = sorted(y for y in years if str(y) not in ok_years)
        for y in keep:
            pairs = [[make_name, ebay_make], [model_name, ebay_model], [year_name, str(y)]]
            key = tuple(map(tuple, pairs))
            if key not in seen:
                seen.add(key)
                out.append({"pairs": pairs, "notes": ""})
        if missing:
            notes.append(f"{make} {model}: {_years_text(missing)} {'is' if len(missing) == 1 else 'are'} not in eBay's list for {ebay_make} {ebay_model}, so left out.")
    if len(out) > MAX_COMPAT_ROWS:
        notes.append(f"eBay takes at most {MAX_COMPAT_ROWS} compatibility rows; {len(out) - MAX_COMPAT_ROWS} were cut.")
        out = out[:MAX_COMPAT_ROWS]
    return out, notes


class ConvertIn(BaseModel):
    product_id: int
    category_id: str


@router.post("/convert")
def convert(body: ConvertIn, db: Session = Depends(get_db)):
    if not configured():
        raise HTTPException(503, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set on the API, so eBay's vehicle lists cannot be read.")
    try:
        rows, notes = convert_with_ebay(db, body.product_id, body.category_id.strip())
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        if isinstance(e, (VocabError, FeedbackError)):
            raise _problem(e)
        log.exception("could not convert fitment of product %s with eBay's lists", body.product_id)
        raise HTTPException(500, "The fitment could not be converted (see the server log).")
    return {"rows": rows, "notes": notes}
