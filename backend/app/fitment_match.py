"""
Pure helpers for fitment research (no web framework, no database), so they can be tested on their own.

  find_machines(text, makes, models)  -> machines (make + model + years) mentioned in a product name / description
  match_model(make_text, model_text, makes, models) -> (make row, model row) from the site's lists, or (None, None)
  extract_json(text)                  -> first JSON object found in an AI reply, or None

`makes`  = [{"id", "name"}]            `models` = [{"id", "make_id", "name"}]
A year_to of 9999 means "and newer".
"""
import json
import re
from typing import Optional

OPEN_END = 9999
_ALIASES = {"canam": "can-am", "can am": "can-am", "gasgas": "gas gas", "arctic cat": "arctic cat", "arcticcat": "arctic cat"}


def _norm(s: str) -> str:
    """Lower case letters and digits only: 'Can-Am' and 'CanAm' and 'can am' all become 'canam'."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _year(tok: str) -> Optional[int]:
    tok = tok.strip()
    if len(tok) == 4 and tok.isdigit():
        y = int(tok)
        return y if 1950 <= y <= 2035 else None
    if len(tok) == 2 and tok.isdigit():
        y = int(tok)
        return 2000 + y if y <= 35 else 1900 + y
    return None


_RANGE = re.compile(r"(?<![\d])((?:19|20)\d{2}|\d{2})\s*(?:-|–|to|thru|through)\s*((?:19|20)\d{2}|\d{2})(?![\d])", re.I)
_PLUS = re.compile(r"(?<![\d])((?:19|20)\d{2})\s*\+", re.I)
_SINGLE = re.compile(r"(?<![\d-])((?:19|20)\d{2})(?![\d+-])")


def find_years(text: str) -> tuple[list[tuple[int, int]], bool]:
    """Year ranges in the text. Second value is True when the text holds several separate ranges (the person must choose)."""
    spans: list[tuple[int, int]] = []
    taken: list[tuple[int, int]] = []
    for m in _RANGE.finditer(text):
        a, b = _year(m.group(1)), _year(m.group(2))
        if a and b and a <= b:
            spans.append((a, b))
            taken.append(m.span())
    for m in _PLUS.finditer(text):
        a = _year(m.group(1))
        if a and not any(s <= m.start() < e for s, e in taken):
            spans.append((a, OPEN_END))
            taken.append(m.span())
    singles = [int(m.group(1)) for m in _SINGLE.finditer(text) if not any(s <= m.start() < e for s, e in taken)]
    if singles and not spans:
        spans.append((min(singles), max(singles)))
        if len(singles) > 1 and max(singles) - min(singles) > len(singles):
            return spans, True
    elif singles:
        spans.extend((y, y) for y in singles)
    return spans, len(spans) > 1


def _find_all(hay: str, needle: str) -> list[tuple[int, int]]:
    out, i = [], 0
    while needle:
        j = hay.find(needle, i)
        if j < 0:
            break
        out.append((j, j + len(needle)))
        i = j + 1
    return out


def find_machines(text: str, makes: list[dict], models: list[dict]) -> list[dict]:
    """Machines named in the text. A model counts when its name appears; the make raises confidence when it sits just before it."""
    low = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower().replace("-", ""))
    low = re.sub(r"\s+", " ", low)
    compact = low.replace(" ", "")
    make_by_id = {m["id"]: m for m in makes}
    make_pos = {m["id"]: _find_all(compact, _norm(m["name"])) for m in makes}
    hits = []
    for md in sorted(models, key=lambda x: -len(_norm(x["name"]))):
        n = _norm(md["name"])
        if len(n) < 3:
            if not make_pos.get(md["make_id"]):
                continue
        for start, end in _find_all(compact, n):
            # a longer model already matched at this spot swallows the shorter one (RZR 900 over RZR)
            if any(s <= start and end <= e for s, e, _ in hits):
                continue
            near = any(0 <= start - mend <= 3 for (_, mend) in make_pos.get(md["make_id"], []))
            if len(n) < 4 and not near:
                continue
            hits.append((start, end, (md, near)))
    spans, many = find_years(text or "")
    out = []
    seen = set()
    for _, _, (md, near) in sorted(hits, key=lambda h: h[0]):
        if md["id"] in seen:
            continue
        seen.add(md["id"])
        mk = make_by_id.get(md["make_id"], {})
        note = ""
        yf = yt = None
        if len(spans) == 1:
            yf, yt = spans[0]
        elif many:
            note = "several separate year ranges in the text; choose the years"
        else:
            note = "no years in the text"
        out.append({
            "make": mk.get("name"), "make_id": md["make_id"], "model": md["name"], "model_id": md["id"],
            "year_from": yf, "year_to": yt, "years_note": note,
            "confidence": "high" if near and yf else "medium",
        })
    return out


def match_model(make_text: str, model_text: str, makes: list[dict], models: list[dict]):
    """Find the site's make and model for text from the AI or a catalog. Exact normalized match first, then 'starts with'."""
    mk_n = _norm(make_text)
    mk_n = _norm(_ALIASES.get((make_text or "").lower().strip(), make_text))
    make = next((m for m in makes if _norm(m["name"]) == mk_n), None)
    if not make:
        return None, None
    mo_n = _norm(model_text)
    mine = [m for m in models if m["make_id"] == make["id"]]
    exact = next((m for m in mine if _norm(m["name"]) == mo_n), None)
    if exact:
        return make, exact
    cands = [m for m in mine if mo_n.startswith(_norm(m["name"])) and len(_norm(m["name"])) >= 3]
    if cands:
        return make, max(cands, key=lambda m: len(_norm(m["name"])))
    return None, None


def extract_json(text: str):
    """First balanced {...} object in an AI reply (ignores code fences and chatter around it)."""
    s = text or ""
    i = s.find("{")
    while i >= 0:
        depth, in_str, esc = 0, False, False
        for j in range(i, len(s)):
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[i:j + 1])
                    except ValueError:
                        break
        i = s.find("{", i + 1)
    return None
