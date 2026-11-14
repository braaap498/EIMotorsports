"""
Fitment research for products that have no vehicle fitment (the distributor catalog carries none).

A fourth way, "paste", skips the waiting list: copied catalog fitment text is previewed, then applied straight to one product
(makes/models created when missing, years added, product_fitment rows made, which also feeds the eBay compatible vehicles).

Three sources feed a WAITING LIST of suggestions; nothing touches product_fitment until a person approves it:
  1. names  - machines and years named in the product's own title (free, instant, runs on many products)
  2. ai     - Claude, with web search and any matching catalog PDF pages as evidence (one product at a time)
  3. pdf    - catalog PDFs uploaded here, read page by page so the part number can be looked up

Mounted at /admin/fitment-research/* via admin_router in main.py.
Needs: run supabase/migrations/fitment_research.sql first; env ANTHROPIC_API_KEY for the AI step;
`pypdf` in requirements.txt for PDF upload (everything else works without it).
"""
import json
import logging
import re
import threading
import urllib.error
import urllib.request
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthUser, current_user, needs, require_route_access
from app.config import get_settings
from app.database import _session_factory, get_db
from app.fitment_match import OPEN_END, _ALIASES, _norm, extract_json, find_machines, match_model, parse_fitment_paste

log = logging.getLogger("fitment_research")
router = APIRouter(prefix="/fitment-research", tags=["fitment-research"], dependencies=[Depends(require_route_access)])

_NEEDS = "(not coalesce(p.is_universal, false) and not p.fitment_exempt and not exists (select 1 from product_fitment pf where pf.product_id = p.id))"
_MPN = "nullif(btrim(p.manufacturer_part_number), '')"
_MAX_PDF = 60 * 1024 * 1024


def _lists(db: Session):
    makes = [dict(r._mapping) for r in db.execute(text("select id, name from vehicle_makes"))]
    models = [dict(r._mapping) for r in db.execute(text("select id, make_id, name from vehicle_models"))]
    return makes, models


def _save_proposal(db: Session, product_id: int, origin: str, verdict: str, notes: str, items: list[dict]) -> int:
    """Replace the product's pending list with a new one."""
    db.execute(text("delete from fitment_proposals where product_id = :p and status = 'pending'"), {"p": product_id})
    pid = db.execute(
        text("insert into fitment_proposals (product_id, origin, verdict, notes) values (:p, :o, :v, :n) returning id"),
        {"p": product_id, "o": origin, "v": verdict, "n": notes},
    ).scalar()
    for it in items:
        db.execute(
            text(
                "insert into fitment_proposal_items (proposal_id, make_text, model_text, year_from, year_to, years_note, make_id, model_id, "
                "source_kind, source_ref, evidence, confidence) values (:pid, :mk, :mo, :yf, :yt, :yn, :mkid, :moid, :sk, :sr, :ev, :cf)"
            ),
            {"pid": pid, "mk": it.get("make"), "mo": it.get("model"), "yf": it.get("year_from"), "yt": it.get("year_to"),
             "yn": it.get("years_note") or "", "mkid": it.get("make_id"), "moid": it.get("model_id"),
             "sk": it.get("source_kind", "names"), "sr": it.get("source_ref"), "ev": (it.get("evidence") or "")[:1000],
             "cf": it.get("confidence", "medium")},
        )
    return pid


# --------------------------------------------------------------------------
# 1. name matching
# --------------------------------------------------------------------------
class NamesRun(BaseModel):
    limit: int = 500
    catalog_only: bool = True   # only products made from the distributor catalog


@router.post("/names/run")
@needs("fitment.add")
def run_names(body: NamesRun, db: Session = Depends(get_db)):
    makes, models = _lists(db)
    link = "and exists (select 1 from supplier_product_links l where l.product_id = p.id)" if body.catalog_only else ""
    rows = db.execute(
        text(
            f"select p.id, p.title as name from products p where {_NEEDS} {link} "
            "and not exists (select 1 from fitment_proposals fp where fp.product_id = p.id and fp.status in ('pending', 'rejected')) "
            "order by p.id limit :n"
        ),
        {"n": max(1, min(body.limit, 5000))},
    ).all()
    made = 0
    for r in rows:
        found = find_machines(r.name or "", makes, models)
        if not found:
            continue
        for f in found:
            f.update(source_kind="names", source_ref="product name", evidence=r.name)
        _save_proposal(db, r.id, "names", "vehicle_specific", "", found)
        made += 1
    db.commit()
    return {"looked_at": len(rows), "suggestions_made": made}


# --------------------------------------------------------------------------
# 2. AI research (one product at a time)
# --------------------------------------------------------------------------
def _pdf_context(db: Session, part_numbers: list[str], limit: int = 4) -> list[dict]:
    out = []
    for pn in part_numbers:
        norm = re.sub(r"[^A-Za-z0-9]", "", pn or "").upper()
        if len(norm) < 4:
            continue
        for r in db.execute(
            text(
                "select f.name, g.page, g.body from fitment_pdf_pages g join fitment_pdfs f on f.id = g.pdf_id "
                "where g.body_norm like :q limit :n"
            ),
            {"q": f"%{norm}%", "n": limit},
        ):
            out.append({"catalog": r.name, "page": r.page, "text": r.body[:6000]})
    return out[:limit]


def _call_claude(system: str, user: str) -> str:
    s = get_settings()
    if not s.anthropic_api_key:
        raise HTTPException(503, "Set ANTHROPIC_API_KEY on the server to use AI research.")
    payload = {
        "model": s.fitment_ai_model, "max_tokens": 2500, "system": system,
        "messages": [{"role": "user", "content": user}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode(), method="POST",
        headers={"x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"AI service said {e.code}: {e.read()[:300].decode(errors='replace')}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"AI service could not be reached: {e}")
    return "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


_SYSTEM = (
    "You research which powersports vehicles (ATV, UTV, dirt bike, snowmobile, etc.) an aftermarket or OEM part fits. "
    "Use the catalog pages given and web search; say so when you are guessing. Never invent fitment: if you cannot find it, "
    'answer verdict "unknown" with no machines. Reply with ONE JSON object only: '
    '{"verdict":"vehicle_specific|universal|not_vehicle|unknown","notes":"short","machines":[{"make":"","model":"","year_from":2015,'
    '"year_to":2020,"source_kind":"pdf|web|knowledge","source_ref":"url or catalog+page","evidence":"short quote or reason",'
    '"confidence":"high|medium|low"}]}. year_to may be 9999 for "and newer". One entry per model; use the exact model names the part maker uses.'
)


@router.post("/products/{product_id}/ai")
@needs("fitment.add")
def research_product(product_id: int, db: Session = Depends(get_db)):
    p = db.execute(
        text(f"select p.id, p.title as name, {_MPN} as mpn, p.custom_label_sku as cls from products p where p.id = :i"),
        {"i": product_id},
    ).first()
    if not p:
        raise HTTPException(404, "Product not found")
    sup = db.execute(
        text("select s.sku, s.mfr_number, s.manufacturer, s.name from supplier_product_links l join supplier_items s "
             "on s.supplier = l.supplier and s.sku = l.supplier_sku where l.product_id = :i limit 1"), {"i": product_id}).first()
    pns = [x for x in [p.mpn, sup.mfr_number if sup else None, sup.sku if sup else None] if x]
    pages = _pdf_context(db, pns)
    brief = f"Product: {p.name}\nBrand: {(sup.manufacturer if sup else '') or ''}\nPart numbers: {', '.join(pns) or 'none'}\n"
    if sup:
        brief += f"Distributor listing: {sup.name}\n"
    if pages:
        brief += "\nCatalog pages that mention this part number:\n" + "\n---\n".join(
            f"[{c['catalog']} p.{c['page']}]\n{c['text']}" for c in pages)
    data = extract_json(_call_claude(_SYSTEM, brief))
    if not isinstance(data, dict):
        raise HTTPException(502, "The AI reply could not be read. Try again.")
    makes, models = _lists(db)
    items = []
    for m in data.get("machines") or []:
        mk, mo = match_model(m.get("make", ""), m.get("model", ""), makes, models)
        items.append({
            "make": (mk or {}).get("name") or m.get("make"), "model": (mo or {}).get("name") or m.get("model"),
            "make_id": (mk or {}).get("id"), "model_id": (mo or {}).get("id"),
            "year_from": m.get("year_from"), "year_to": m.get("year_to"),
            "years_note": "" if mo else "model is not in the site's lists yet",
            "source_kind": m.get("source_kind") if m.get("source_kind") in ("pdf", "web", "knowledge") else "knowledge",
            "source_ref": m.get("source_ref"), "evidence": m.get("evidence"),
            "confidence": m.get("confidence") if m.get("confidence") in ("high", "medium", "low") else "low",
        })
    verdict = data.get("verdict") if data.get("verdict") in ("vehicle_specific", "universal", "not_vehicle", "unknown") else "unknown"
    pid = _save_proposal(db, product_id, "ai", verdict, str(data.get("notes") or "")[:1000], items)
    db.commit()
    return {"proposal_id": pid, "verdict": verdict, "machines": len(items), "used_catalog_pages": len(pages)}


# --------------------------------------------------------------------------
# review queue
# --------------------------------------------------------------------------
@router.get("/queue")
@needs("fitment.view")
def queue(status: str = Query("pending"), q: Optional[str] = None, limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    like = f"%{q.strip()}%" if q and q.strip() else None
    rows = db.execute(
        text(
            "select fp.id, fp.product_id, fp.origin, fp.verdict, fp.notes, fp.created_at, p.title as name, p.sku, "
            "(select count(*) from fitment_proposal_items i where i.proposal_id = fp.id) as items "
            "from fitment_proposals fp join products p on p.id = fp.product_id "
            "where fp.status = :s and (cast(:q as text) is null or p.title ilike cast(:q as text) or p.sku ilike cast(:q as text)) "
            "order by fp.id desc limit :l offset :o"
        ),
        {"s": status, "q": like, "l": min(limit, 500), "o": offset},
    ).all()
    return [dict(r._mapping) for r in rows]


@router.get("/products/{product_id}/proposal")
@needs("fitment.view")
def get_proposal(product_id: int, db: Session = Depends(get_db)):
    fp = db.execute(text("select * from fitment_proposals where product_id = :p and status = 'pending'"), {"p": product_id}).first()
    if not fp:
        return {"proposal": None}
    items = db.execute(text("select * from fitment_proposal_items where proposal_id = :i order by id"), {"i": fp.id}).all()
    return {"proposal": dict(fp._mapping), "items": [dict(i._mapping) for i in items]}


class ApplyItem(BaseModel):
    item_id: int
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    model_id: Optional[int] = None   # lets the reviewer pick the site model when none matched


class ApplyBody(BaseModel):
    items: list[ApplyItem] = []
    mark_universal: bool = False


@router.post("/proposals/{proposal_id}/apply")
@needs("fitment.add")
def apply(proposal_id: int, body: ApplyBody, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    fp = db.execute(text("select id, product_id, status from fitment_proposals where id = :i"), {"i": proposal_id}).first()
    if not fp or fp.status != "pending":
        raise HTTPException(404, "That suggestion is no longer waiting")
    added = skipped = 0
    for it in body.items:
        row = db.execute(text("select * from fitment_proposal_items where id = :i and proposal_id = :p"), {"i": it.item_id, "p": proposal_id}).first()
        if not row:
            continue
        model_id = it.model_id or row.model_id
        yf = it.year_from if it.year_from is not None else row.year_from
        yt = it.year_to if it.year_to is not None else row.year_to
        if not model_id or yf is None:
            skipped += 1
            continue
        added += db.execute(
            text(
                "insert into product_fitment (product_id, vehicle_id, notes, source) "
                "select :p, v.id, :n, 'research' from vehicles v where v.model_id = :m and v.year between :a and :b "
                "and not exists (select 1 from product_fitment pf where pf.product_id = :p and pf.vehicle_id = v.id)"
            ),
            {"p": fp.product_id, "m": model_id, "a": yf, "b": min(yt or OPEN_END, OPEN_END), "n": f"{row.source_kind}: {row.source_ref or ''}"[:200]},
        ).rowcount
    if body.mark_universal:
        db.execute(text("update products set is_universal = true where id = :p"), {"p": fp.product_id})
    db.execute(text("update fitment_proposals set status = 'applied', reviewed_at = now(), reviewed_by = :u where id = :i"),
               {"u": user.email or user.user_id, "i": proposal_id})
    _refresh(db)
    db.commit()
    return {"added": added, "skipped": skipped}


@router.post("/proposals/{proposal_id}/dismiss")
@needs("fitment.add")
def dismiss(proposal_id: int, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    n = db.execute(text("update fitment_proposals set status = 'rejected', reviewed_at = now(), reviewed_by = :u "
                        "where id = :i and status = 'pending'"), {"u": user.email or user.user_id, "i": proposal_id}).rowcount
    db.commit()
    return {"dismissed": n}


def _refresh(db: Session) -> None:
    if db.execute(text("select 1 from pg_matviews where schemaname = 'public' and matviewname = 'fitment_catalog'")).first():
        db.execute(text("refresh materialized view fitment_catalog"))


# --------------------------------------------------------------------------
# 3. catalog PDFs
# --------------------------------------------------------------------------
def _index_pdf(pdf_id: int, data: bytes) -> None:
    db = _session_factory()()
    try:
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        n = len(reader.pages)
        db.execute(text("update fitment_pdfs set pages = :n where id = :i"), {"n": n, "i": pdf_id})
        db.commit()
        for i, page in enumerate(reader.pages, start=1):
            try:
                body = (page.extract_text() or "").strip()
            except Exception:  # noqa: BLE001
                body = ""
            if body:
                db.execute(text("insert into fitment_pdf_pages (pdf_id, page, body, body_norm) values (:p, :n, :b, :bn) on conflict do nothing"),
                           {"p": pdf_id, "n": i, "b": body, "bn": re.sub(r"[^A-Za-z0-9]", "", body).upper()})
            if i % 10 == 0 or i == n:
                db.execute(text("update fitment_pdfs set pages_done = :d where id = :i"), {"d": i, "i": pdf_id})
                db.commit()
        db.execute(text("update fitment_pdfs set status = 'ready', pages_done = pages where id = :i"), {"i": pdf_id})
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.exception("pdf index failed")
        db.execute(text("update fitment_pdfs set status = 'failed', error = :e where id = :i"), {"e": str(e)[:500], "i": pdf_id})
        db.commit()
    finally:
        db.close()


@router.post("/pdfs")
@needs("fitment.add")
async def upload_pdf(request: Request, name: str = Query(...), brand: str = Query(""), db: Session = Depends(get_db)):
    """Send the PDF file itself as the request body (raw bytes); name and brand go in the address."""
    try:
        import pypdf  # noqa: F401
    except ImportError:
        raise HTTPException(503, "PDF reading isn't installed on the server yet: add pypdf to requirements.txt.")
    data = await request.body()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "That file is not a PDF")
    if len(data) > _MAX_PDF:
        raise HTTPException(413, "That PDF is over 60 MB; split it first")
    pdf_id = db.execute(text("insert into fitment_pdfs (name, brand) values (:n, :b) returning id"), {"n": name.strip()[:200], "b": brand.strip()[:100]}).scalar()
    db.commit()
    threading.Thread(target=_index_pdf, args=(pdf_id, data), daemon=True, name=f"fitment-pdf-{pdf_id}").start()
    return {"id": pdf_id, "status": "indexing"}


@router.get("/pdfs")
@needs("fitment.view")
def list_pdfs(db: Session = Depends(get_db)):
    return [dict(r._mapping) for r in db.execute(text("select id, name, brand, status, pages, pages_done, error, created_at from fitment_pdfs order by id desc"))]


@router.delete("/pdfs/{pdf_id}")
@needs("fitment.add")
def delete_pdf(pdf_id: int, db: Session = Depends(get_db)):
    db.execute(text("delete from fitment_pdfs where id = :i"), {"i": pdf_id})
    db.commit()
    return {"deleted": True}


# --------------------------------------------------------------------------
# 4. paste copied catalog fitment (Make / year / models ...) straight onto one product
# --------------------------------------------------------------------------
def _slugify(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (v or "").lower()).strip("-")


def _make_key(name: str) -> str:
    return _norm(_ALIASES.get((name or "").lower().strip(), name))


@router.get("/paste/products")
@needs("fitment.view")
def paste_find_products(q: str = Query(..., min_length=1), db: Session = Depends(get_db)):
    """Find the product a pasted list belongs to: by id, our SKU, manufacturer part number or the distributor's part number."""
    raw = q.strip()
    norm = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if not norm:
        return []
    rows = db.execute(
        text(
            "select p.id, p.title, p.sku, " + _MPN + " as mpn, "
            "(select count(distinct pf.vehicle_id) from product_fitment pf where pf.product_id = p.id) as fitment_rows "
            "from products p where p.id = :id or p.sku ilike :like or regexp_replace(upper(coalesce(p.manufacturer_part_number, '')), '[^A-Z0-9]', '', 'g') = :n "
            "or exists (select 1 from supplier_product_links l join supplier_items s on s.supplier = l.supplier and s.sku = l.supplier_sku "
            "          where l.product_id = p.id and (regexp_replace(upper(coalesce(s.sku, '')), '[^A-Z0-9]', '', 'g') = :n "
            "          or regexp_replace(upper(coalesce(s.mfr_number, '')), '[^A-Z0-9]', '', 'g') = :n)) "
            "order by p.id limit 15"
        ),
        {"id": int(raw) if raw.isdigit() else -1, "like": raw, "n": norm},
    ).all()
    return [dict(r._mapping) for r in rows]


class PasteIn(BaseModel):
    text: str
    vehicle_type_id: Optional[int] = None


def _paste_plan(db: Session, text_in: str):
    parsed = parse_fitment_paste(text_in, [m["name"] for m in db.execute(text("select name from vehicle_makes")).mappings()])
    makes = [dict(r._mapping) for r in db.execute(text("select id, name from vehicle_makes"))]
    models = [dict(r._mapping) for r in db.execute(text("select id, make_id, name, vehicle_type_id from vehicle_models"))]
    plan = []
    for pm in parsed["makes"]:
        key = _make_key(pm["make"])
        mk = next((m for m in makes if _norm(m["name"]) == key), None)
        mine = [m for m in models if mk and m["make_id"] == mk["id"]]
        pm_models = []
        for mo in pm["models"]:
            n = _norm(mo["model"])
            ex = next((m for m in mine if _norm(m["name"]) == n), None)
            similar = []
            if not ex and len(n) >= 3:
                similar = [m["name"] for m in mine if _norm(m["name"]) and (n.startswith(_norm(m["name"])) or _norm(m["name"]).startswith(n))][:3]
            have = set()
            if ex:
                have = {r.year for r in db.execute(text("select year from vehicles where model_id = :m"), {"m": ex.get("id")})}
            pm_models.append({
                "model": mo["model"], "years": mo["years"], "model_id": ex["id"] if ex else None,
                "model_exists": bool(ex), "type_id": ex["vehicle_type_id"] if ex else None,
                "new_years": len([y for y in mo["years"] if y not in have]), "similar": similar,
            })
        plan.append({"make": pm["make"], "make_id": mk["id"] if mk else None, "make_exists": bool(mk), "models": pm_models})
    return plan, parsed["warnings"]


@router.post("/paste/preview")
@needs("fitment.view")
def paste_preview(body: PasteIn, db: Session = Depends(get_db)):
    if len(body.text) > 400_000:
        raise HTTPException(413, "That text is too long; paste one catalog part at a time.")
    plan, warnings = _paste_plan(db, body.text)
    if not plan:
        raise HTTPException(400, "No makes, years and models were found in that text.")
    return {
        "makes": plan, "warnings": warnings,
        "totals": {
            "makes": len(plan), "new_makes": sum(1 for m in plan if not m["make_exists"]),
            "models": sum(len(m["models"]) for m in plan), "new_models": sum(1 for m in plan for x in m["models"] if not x["model_exists"]),
            "model_years": sum(len(x["years"]) for m in plan for x in m["models"]),
        },
    }


class PasteApply(BaseModel):
    text: str
    vehicle_type_id: int
    skip_makes: list[str] = []           # make names unchecked in the preview
    skip_models: list[str] = []          # "make|model" pairs unchecked in the preview


@router.post("/products/{product_id}/paste/apply")
@needs("fitment.add")
def paste_apply(product_id: int, body: PasteApply, db: Session = Depends(get_db)):
    if not db.execute(text("select 1 from products where id = :i"), {"i": product_id}).first():
        raise HTTPException(404, "Product not found")
    if not db.execute(text("select 1 from vehicle_types where id = :i"), {"i": body.vehicle_type_id}).first():
        raise HTTPException(400, "Choose the vehicle type for new models.")
    plan, _ = _paste_plan(db, body.text)
    skip_mk = {_norm(x) for x in body.skip_makes}
    skip_mo = set(body.skip_models)
    c = {"makes_created": 0, "models_created": 0, "types_filled": 0, "years_created": 0, "fitment_added": 0, "already_had": 0}
    note = "catalog paste"
    made: dict[tuple, int] = {}   # (make id, model key) created earlier in this paste, so two spellings never make two models
    try:
        for pm in plan:
            if _norm(pm["make"]) in skip_mk:
                continue
            make_id = pm["make_id"]
            if not make_id:
                nm = pm["make"].strip()
                slug, n = _slugify(nm) or "make", 1
                while db.execute(text("select 1 from vehicle_makes where slug = :s"), {"s": slug}).first():
                    n += 1
                    slug = f"{_slugify(nm)}-{n}"
                make_id = db.execute(text("insert into vehicle_makes (name, slug) values (:n, :s) returning id"), {"n": nm, "s": slug}).scalar()
                c["makes_created"] += 1
            for mo in pm["models"]:
                if f"{pm['make']}|{mo['model']}" in skip_mo:
                    continue
                model_id = mo["model_id"] or made.get((make_id, _norm(mo["model"])))
                if not model_id:
                    base = _slugify(mo["model"]) or "model"
                    slug, n = base, 1
                    while db.execute(text("select 1 from vehicle_models where make_id = :m and slug = :s"), {"m": make_id, "s": slug}).first():
                        n += 1
                        slug = f"{base}-{n}"
                    model_id = db.execute(
                        text("insert into vehicle_models (make_id, vehicle_type_id, name, slug) values (:m, :t, :n, :s) returning id"),
                        {"m": make_id, "t": body.vehicle_type_id, "n": mo["model"].strip(), "s": slug}).scalar()
                    made[(make_id, _norm(mo["model"]))] = model_id
                    c["models_created"] += 1
                elif mo["model_id"] and mo["type_id"] is None:
                    db.execute(text("update vehicle_models set vehicle_type_id = :t where id = :m and vehicle_type_id is null"),
                               {"t": body.vehicle_type_id, "m": model_id})
                    c["types_filled"] += 1
                ys = ",".join(str(y) for y in mo["years"])
                c["years_created"] += db.execute(
                    text("insert into vehicles (model_id, year) select :m, y from unnest(cast(string_to_array(:ys, ',') as int[])) as y "
                         "where not exists (select 1 from vehicles v where v.model_id = :m and v.year = y)"),
                    {"m": model_id, "ys": ys}).rowcount
                added = db.execute(
                    text("insert into product_fitment (product_id, vehicle_id, notes, source) "
                         "select :p, v.id, :n, 'research' from vehicles v where v.model_id = :m "
                         "and v.year = any(cast(string_to_array(:ys, ',') as int[])) "
                         "and not exists (select 1 from product_fitment pf where pf.product_id = :p and pf.vehicle_id = v.id)"),
                    {"p": product_id, "m": model_id, "ys": ys, "n": note}).rowcount
                c["fitment_added"] += added
                c["already_had"] += len(mo["years"]) - added
        _refresh(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return c
