"""
Crunches the eBay order history into "insights": seasonality, product mix, buyer behaviour, and a diagnosis of
odd months. No web or database code in here, so it can be tested on its own (see analytics_insights.py for the
route that feeds it).

Inputs
  lines  one dict per order LINE (an item in an order) from orders that were actually paid (total_price > 0):
         store, order_number, sale_date (date), buyer_username, state, item_number, item_title, quantity, unit_price
  days   one dict per sale date: sale_date, orders, net_sales (all stores in the filter combined)

Part type, vehicle make, vehicle type and part brand are GUESSED FROM THE ITEM TITLE with keyword rules (below).
They are good enough to show trends, they are not a catalogue. Every table says how much it could not classify.
"""
from __future__ import annotations

import calendar
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from itertools import combinations
from typing import Any, Iterable, Optional

MON3 = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# --------------------------------------------------------------------------------------------------------------
# Title classifiers. First rule that matches wins, so the order matters.
# --------------------------------------------------------------------------------------------------------------
PART_RULES: list[tuple[str, str]] = [
    ("Top-end rebuild kits", r"top[\s-]?end|rebuild kit|piston kit|big bore kit"),
    ("Pistons & rings", r"\bpistons?\b|piston rings?|\bring set"),
    ("Gaskets & seals", r"gasket|o-?rings?\b|oil seal|seal kit|crank seal|\bseals?\b"),
    ("Axles, CV & wheels", r"\baxle|cv[\s-]?(joint|boot)|wheel bearing|\bhubs?\b|\bwheels?\b|\brims?\b|\btires?\b|\btyres?\b"),
    ("Brakes", r"brake|rotor|caliper|\bpads?\b"),
    ("Crank, rods & bearings", r"crank|connecting rod|con rod|rod kit|bearing"),
    ("Cylinders, heads & valves", r"cylinder|\bheads?\b|valve|camshaft|cam chain|rocker"),
    ("Carburetor & fuel", r"carb|jet kit|needle|fuel pump|petcock|fuel filter|throttle body|injector|float"),
    ("Ignition & electrical", r"\bcdi\b|stator|ignition|\bcoils?\b|regulator|rectifier|spark plug|battery|starter|solenoid|relay|wiring|flywheel|pickup"),
    ("Clutch & drive belts", r"clutch|drive belt|\bbelts?\b|variator|sheave|\broller"),
    ("Suspension & steering", r"a[\s-]?arm|ball joint|tie rod|bushing|shock|swingarm|steering|suspension|\bsprings?\b"),
    ("Chain & sprockets", r"chain|sprocket"),
    ("Filters & fluids", r"filter|\boil\b|lube|grease|coolant|antifreeze"),
    ("Exhaust", r"exhaust|muffler|silencer|header|\bpipe\b"),
    ("Skis, track & carbides", r"\bskis?\b|carbide|runner|\btrack\b|\bstuds?\b"),
    ("Body, plastics & graphics", r"plastic|fender|fairing|graphic|decal|\bseat|mud flap|\bbody\b|\bcover\b"),
    ("Controls & cables", r"cable|throttle|lever|handlebar|\bgrips?\b|pedal|shifter"),
    ("Accessories & gear", r"winch|bumper|skid|nerf|\bbags?\b|\brack|light|mirror|helmet|jacket|glove|tool"),
]
_PART_RE = [(name, re.compile(rx, re.I)) for name, rx in PART_RULES]

MAKE_RULES: list[tuple[str, str]] = [
    ("Ski-Doo", r"ski[\s-]?doo|skidoo"), ("Sea-Doo", r"sea[\s-]?doo|seadoo"), ("Arctic Cat", r"arctic\s?cat"),
    ("Can-Am", r"can[\s-]?am"), ("Polaris", r"polaris"), ("Honda", r"honda"), ("Yamaha", r"yamaha"),
    ("Kawasaki", r"kawasaki"), ("Suzuki", r"suzuki"), ("KTM", r"\bktm\b"), ("Husqvarna", r"husqvarna"),
    ("Gas Gas", r"gas[\s-]?gas"), ("Kubota", r"kubota"), ("John Deere", r"john deere"), ("Lynx", r"\blynx\b"),
    ("Cub Cadet", r"cub cadet"),
]
_MAKE_RE = [(name, re.compile(rx, re.I)) for name, rx in MAKE_RULES]

BRAND_RULES: list[tuple[str, str]] = [
    ("Wiseco", r"wiseco"), ("Vertex", r"vertex"), ("Namura", r"namura"), ("ProX", r"pro[\s-]?x\b"),
    ("All Balls", r"all balls"), ("Tusk", r"\btusk\b"), ("EPI", r"\bepi\b"), ("Moose", r"\bmoose\b"),
    ("WSM", r"\bwsm\b"), ("Athena", r"athena"), ("Hot Rods", r"hot rods"), ("Cometic", r"cometic"),
    ("EBC", r"\bebc\b"), ("NGK", r"\bngk\b"), ("DID", r"\bdid\b"), ("Quadboss", r"quad ?boss"),
    ("Kimpex", r"kimpex"), ("SPI", r"\bspi\b|sports parts"), ("OEM / genuine", r"\boem\b|genuine"),
]
_BRAND_RE = [(name, re.compile(rx, re.I)) for name, rx in BRAND_RULES]

_SNOW = re.compile(
    r"snowmobile|\bmxz\b|\bsummit\b|skandic|tundra|formula|mach z|freeride|\bindy\b|\brmk\b|switchback|widetrak|bearcat|"
    r"\bzr\b|sno ?pro|proclimb|\bm ?8000|\bm ?1000|phazer|nytro|\bapex\b|\bvector\b|\bventure\b|\bsrx\b|exciter|\bviper\b|"
    r"expedition|\blegend\b|grand touring|crossfire|firecat|sabercat|powder special|\bcheetah\b|\bz1\b|touring|\bsv\b|\bgsx\b",
    re.I,
)
_PWC = re.compile(r"wave ?runner|jet ?ski|\bgtx\b|\brxp\b|\brxt\b|\bgti\b|super ?jet|\bsx-?r\b|\bgtr\b|\bspark\b|personal watercraft|\bpwc\b", re.I)
_UTV = re.compile(
    r"\branger\b|\brzr\b|\brhino\b|teryx|\bmule\b|maverick|commander|defender|pioneer|\btalon\b|\bgenerals?\b|gator|viking|prowler|"
    r"wildcat|\bx3\b|\butv\b|side by side|sxs|\bkrx\b|\bkrf\b|wolverine",
    re.I,
)
_ATV = re.compile(
    r"\btrx|foreman|rancher|recon|rubicon|fourtrax|sportsman|scrambler|outlander|renegade|grizzly|kodiak|bruin|big bear|banshee|"
    r"blaster|warrior|raptor|\byfz|\bltz|\bltf|\bkfx|\blt-?\d|mojave|prairie|brute force|bayou|\bmagnum\b|trail boss|xplorer|"
    r"\batv\b|quad|\bklf|\bkef|king quad|\bmoto-?4|\bttr-?\d{3}\b.*atv|\bdvx|\bkvf",
    re.I,
)
_DIRT = re.compile(
    r"\bcr\s?\d{2,3}|\bcrf\s?\d|\bxr\s?\d|\byz\s?\d|\bwr\s?\d|\bkx\s?\d|\brm\s?\d|\brm-?z|\bklx|\bdr-?z|\bttr|\bxt\s?\d|\bsxf?\b|\bexc\b|"
    r"\bxc-?w?\b|\bxl\s?\d|\bxlr|\bdirt bike|motocross|\bkdx|\bdr\s?\d{3}",
    re.I,
)
_MOTO = re.compile(r"motorcycle|\bcb\s?\d|\bcbr|\bvfr|\bgsx-?r|\bninja|\bduke\b|\bvtx|\bshadow\b|\bsportster", re.I)


def classify_part(title: str) -> str:
    for name, rx in _PART_RE:
        if rx.search(title):
            return name
    return "Other / unclassified"


def classify_make(title: str) -> str:
    best: Optional[tuple[int, str]] = None
    for name, rx in _MAKE_RE:
        m = rx.search(title)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    return best[1] if best else "Unknown"


def classify_brand(title: str) -> str:
    for name, rx in _BRAND_RE:
        if rx.search(title):
            return name
    return "Other / unbranded"


def classify_type(title: str, make: str) -> str:
    if make == "Ski-Doo" or make == "Lynx":
        return "Snowmobile"
    if make == "Sea-Doo":
        return "Watercraft (PWC)"
    if _PWC.search(title):
        return "Watercraft (PWC)"
    if make in ("Arctic Cat", "Polaris", "Yamaha", "Unknown", "Honda", "Suzuki", "Kawasaki") and _SNOW.search(title):
        # "Formula"/"Touring"/"Legend" can appear on other things, but with these makes it is nearly always a sled.
        if make in ("Honda", "Kawasaki", "Suzuki"):
            pass  # these make no snowmobiles (Suzuki's old Arctic Cat links aside); fall through
        else:
            return "Snowmobile"
    if _UTV.search(title):
        return "UTV / side-by-side"
    if _DIRT.search(title):
        return "Dirt bike"
    if _ATV.search(title):
        return "ATV"
    if _MOTO.search(title):
        return "Motorcycle"
    return "Unknown"


_cache: dict[str, tuple[str, str, str, str]] = {}


def classify(title: Optional[str]) -> tuple[str, str, str, str]:
    """(part type, make, vehicle type, brand) for an item title."""
    t = (title or "").strip()
    hit = _cache.get(t)
    if hit is None:
        make = classify_make(t)
        hit = (classify_part(t), make, classify_type(t, make), classify_brand(t))
        if len(_cache) < 50000:
            _cache[t] = hit
    return hit


# --------------------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------------------
def ym(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def month_list(first: str, last: str) -> list[str]:
    y, m = map(int, first.split("-"))
    ey, em = map(int, last.split("-"))
    out: list[str] = []
    while (y, m) <= (ey, em):
        out.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def prev_year(month: str) -> str:
    y, m = month.split("-")
    return f"{int(y) - 1}-{m}"


def mask_user(u: Optional[str]) -> str:
    u = (u or "").strip()
    return (u[:2] + "***") if u else "(unknown)"


def _r(x: float, n: int = 2) -> float:
    return round(float(x), n)


def pearson(a: list[float], b: list[float]) -> Optional[float]:
    if len(a) != len(b) or len(a) < 4:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va ** 0.5 * vb ** 0.5)


# --------------------------------------------------------------------------------------------------------------
# seasonality
# --------------------------------------------------------------------------------------------------------------
def seasonal_rows(monthly: dict[str, dict[str, float]], complete: list[str], min_total: float, limit: int) -> list[dict]:
    """
    One row per key (part type, vehicle type, ...). `monthly[key][YYYY-MM]` is units.
    index[m] = (average units in calendar month m over the complete months we have) / (average month overall).
    1.0 = a normal month, 1.5 = 50% busier than normal, 0.5 = half as busy.
    repeat = correlation of the latest two 12-month windows lined up by calendar month: high means the same
    months were busy both years (a real season), low or negative means the pattern did not repeat.
    """
    rows = []
    n_months = len(complete)
    windows: Optional[tuple[list[str], list[str]]] = None
    if n_months >= 24:
        last24 = complete[-24:]
        windows = (last24[:12], last24[12:])
    for key, by in monthly.items():
        total = sum(by.get(m, 0.0) for m in complete)
        if total < min_total or n_months == 0:
            continue
        avg_all = total / n_months
        idx: list[Optional[float]] = []
        for mo in range(1, 13):
            vals = [by.get(m, 0.0) for m in complete if int(m[5:]) == mo]
            idx.append(_r(sum(vals) / len(vals) / avg_all, 2) if vals else None)
        have = [(i, v) for i, v in enumerate(idx) if v is not None]
        peak = max(have, key=lambda t: t[1]) if have else None
        low = min(have, key=lambda t: t[1]) if have else None
        repeat = None
        if windows:
            a = [by.get(m, 0.0) for m in windows[0]]
            b_by_mo = {int(m[5:]): by.get(m, 0.0) for m in windows[1]}
            b = [b_by_mo.get(int(m[5:]), 0.0) for m in windows[0]]
            c = pearson(a, b)
            repeat = _r(c, 2) if c is not None else None
        rows.append({
            "key": key, "units": _r(total, 0), "index": idx,
            "peak_month": MON3[peak[0]] if peak else None, "peak_index": peak[1] if peak else None,
            "low_month": MON3[low[0]] if low else None, "low_index": low[1] if low else None,
            "repeat": repeat,
        })
    rows.sort(key=lambda r: -r["units"])
    return rows[:limit]


# --------------------------------------------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------------------------------------------
def build_insights(lines: list[dict[str, Any]], days: list[dict[str, Any]], today: Optional[date] = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True}
    if not days:
        return {"ok": False, "reason": "no orders in this range"}

    today = today or date.today()
    day_dates = sorted({d["sale_date"] for d in days})
    first_date, last_date = day_dates[0], day_dates[-1]
    first_ym, last_ym = ym(first_date), ym(last_date)

    # ---- which months are complete? The month of the last sale is incomplete if that sale is before the month's end.
    # The first month is incomplete if the data starts after the 1st (an export that begins mid-month).
    partial: Optional[dict] = None
    all_months = month_list(first_ym, last_ym)
    incomplete: set[str] = set()
    if last_date < month_end(last_date):
        incomplete.add(last_ym)
        partial = {"month": last_ym, "through": last_date.isoformat(), "days": last_date.day, "days_in_month": month_end(last_date).day}
    if first_date.day > 3:
        incomplete.add(first_ym)
    complete = [m for m in all_months if m not in incomplete]
    out["coverage"] = {
        "first_date": first_date.isoformat(), "last_date": last_date.isoformat(),
        "months": len(all_months), "complete_months": len(complete), "partial_month": partial,
        "first_month_partial": first_ym in incomplete and first_ym != last_ym,
    }

    # ---- per-month health: orders, active days, orders per active day, average order value
    by_day: dict[date, dict[str, float]] = defaultdict(lambda: {"orders": 0, "net": 0.0})
    for d in days:
        by_day[d["sale_date"]]["orders"] += int(d["orders"])
        by_day[d["sale_date"]]["net"] += float(d["net_sales"] or 0)
    mon_orders: dict[str, int] = defaultdict(int)
    mon_net: dict[str, float] = defaultdict(float)
    mon_active: dict[str, int] = defaultdict(int)
    for dt, v in by_day.items():
        k = ym(dt)
        mon_orders[k] += v["orders"]
        mon_net[k] += v["net"]
        if v["orders"] > 0:
            mon_active[k] += 1

    def dim(k: str) -> int:
        y, m = map(int, k.split("-"))
        return calendar.monthrange(y, m)[1]

    months_tbl = []
    for k in all_months:
        o, n, a = mon_orders.get(k, 0), mon_net.get(k, 0.0), mon_active.get(k, 0)
        po, pn = mon_orders.get(prev_year(k)), mon_net.get(prev_year(k))
        months_tbl.append({
            "month": k, "orders": o, "net_sales": _r(n, 0), "active_days": a, "days_in_month": dim(k),
            "orders_per_active_day": _r(o / a, 1) if a else 0, "aov": _r(n / o, 2) if o else 0,
            "complete": k not in incomplete,
            "orders_yoy": _r(o / po - 1, 3) if po else None, "net_yoy": _r(n / pn - 1, 3) if pn else None,
        })
    out["months"] = months_tbl

    # ---- days with no orders at all, inside the data span
    gaps = []
    have = set(day_dates)
    run_start: Optional[date] = None
    cur = first_date
    while cur <= last_date:
        if cur not in have:
            run_start = run_start or cur
        elif run_start:
            gaps.append((run_start, cur - timedelta(days=1)))
            run_start = None
        cur += timedelta(days=1)
    out["gaps"] = sorted(
        [{"start": a.isoformat(), "end": b.isoformat(), "days": (b - a).days + 1} for a, b in gaps if (b - a).days + 1 >= 3],
        key=lambda g: -g["days"],
    )[:10]

    # ---- line-level facts
    recs = []
    for ln in lines:
        d: date = ln["sale_date"]
        part, make, vtype, brand = classify(ln.get("item_title"))
        q = int(ln.get("quantity") or 1) or 1
        price = float(ln.get("unit_price") or 0)
        recs.append({
            "d": d, "ym": ym(d), "mo": d.month, "store": ln["store"], "order": (ln["store"], ln["order_number"]),
            "buyer": ln.get("buyer_username"), "state": ln.get("state"), "item": ln.get("item_number"),
            "title": ln.get("item_title") or "", "q": q, "rev": q * price, "part": part, "make": make, "vtype": vtype, "brand": brand,
        })
    out["lines"] = len(recs)
    if not recs:
        out["classification"] = {}
        return out

    n_unknown_type = sum(r["q"] for r in recs if r["vtype"] == "Unknown")
    n_unknown_part = sum(r["q"] for r in recs if r["part"].startswith("Other"))
    total_units = sum(r["q"] for r in recs)
    out["classification"] = {
        "units": total_units,
        "unknown_part_share": _r(n_unknown_part / total_units, 3),
        "unknown_type_share": _r(n_unknown_type / total_units, 3),
        "unknown_make_share": _r(sum(r["q"] for r in recs if r["make"] == "Unknown") / total_units, 3),
    }

    # ---- seasonality
    def monthly_by(field: str) -> dict[str, dict[str, float]]:
        m: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for r in recs:
            m[r[field]][r["ym"]] += r["q"]
        return m

    part_m, type_m, make_m = monthly_by("part"), monthly_by("vtype"), monthly_by("make")
    overall: dict[str, dict[str, float]] = {"All parts": defaultdict(float)}
    for r in recs:
        overall["All parts"][r["ym"]] += r["q"]
    out["seasonality"] = {
        "overall": seasonal_rows(overall, complete, 1, 1),
        "by_part": seasonal_rows(part_m, complete, 40, 14),
        "by_type": seasonal_rows(type_m, complete, 40, 8),
        "by_make": seasonal_rows(make_m, complete, 40, 10),
    }

    def stack(m: dict[str, dict[str, float]], top: int) -> dict:
        tot = {k: sum(v.get(x, 0) for x in all_months) for k, v in m.items()}
        keys = [k for k, _ in sorted(tot.items(), key=lambda t: -t[1])][:top]
        series = [{"key": k, "values": [m[k].get(x, 0) for x in all_months]} for k in keys]
        rest = [sum(v.get(x, 0) for kk, v in m.items() if kk not in keys) for x in all_months]
        if any(rest):
            series.append({"key": "Everything else", "values": rest})
        return {"months": all_months, "series": series}

    out["units_by_month"] = {"part": stack(part_m, 6), "type": stack(type_m, 6)}

    # ---- the biggest drop, and what it was made of
    out["diagnosis"] = _diagnose(recs, months_tbl, complete, by_day, mon_orders)

    # ---- movers: last 90 days vs the same 90 days a year earlier
    out["movers"] = _movers(recs, last_date)

    # ---- concentration
    item_rev: dict[str, float] = defaultdict(float)
    item_units: dict[str, float] = defaultdict(float)
    for r in recs:
        if r["item"]:
            item_rev[r["item"]] += r["rev"]
            item_units[r["item"]] += r["q"]
    revs = sorted(item_rev.values(), reverse=True)
    tot_rev = sum(revs) or 1
    cum, n80 = 0.0, 0
    for v in revs:
        cum += v
        n80 += 1
        if cum >= 0.8 * tot_rev:
            break
    out["concentration"] = {
        "items_sold": len(revs), "top10_share": _r(sum(revs[:10]) / tot_rev, 3), "top50_share": _r(sum(revs[:50]) / tot_rev, 3),
        "items_for_80pct": n80, "sold_once_share": _r(sum(1 for v in item_units.values() if v <= 1) / max(len(item_units), 1), 3),
    }

    # ---- spikes: an item that sold far more than usual in one month
    out["spikes"] = _spikes(recs)

    # ---- buyers
    out["buyers"] = _buyers(recs, complete, first_date, last_date)

    # ---- tables: makes and brands
    def table(field: str, limit: int) -> list[dict]:
        agg: dict[str, dict[str, float]] = defaultdict(lambda: {"units": 0, "rev": 0.0})
        for r in recs:
            agg[r[field]]["units"] += r["q"]
            agg[r[field]]["rev"] += r["rev"]
        rows = sorted(agg.items(), key=lambda t: -t[1]["rev"])[:limit]
        return [{"key": k, "units": v["units"], "revenue": _r(v["rev"], 0), "avg_price": _r(v["rev"] / v["units"], 2) if v["units"] else 0} for k, v in rows]

    out["tables"] = {"make": table("make", 10), "brand": table("brand", 10), "part": table("part", 14), "type": table("vtype", 8)}

    # ---- where each vehicle type sells (states that over-index)
    out["geo"] = _geo(recs)
    return out


# --------------------------------------------------------------------------------------------------------------
def _diagnose(recs, months_tbl, complete, by_day, mon_orders) -> Optional[dict]:
    """Pick the complete month that fell furthest below normal and say what it was made of."""
    cands = []
    cm = [m for m in months_tbl if m["complete"]]
    for i, m in enumerate(cm):
        if m["orders_yoy"] is not None:
            drop = m["orders_yoy"]
            base_label = "the same month a year earlier"
        elif i >= 3:
            prev = [x["orders"] for x in cm[i - 3:i]]
            base = sum(prev) / 3
            drop = m["orders"] / base - 1 if base else 0
            base_label = "the average of the 3 months before"
        else:
            continue
        cands.append((drop, m["month"], base_label))
    if not cands:
        return None
    drop, month, base_label = min(cands)
    if drop > -0.25:
        return None  # nothing stands out

    if base_label.startswith("the same"):
        base_months = [prev_year(month)]
    else:
        ix = [m["month"] for m in cm].index(month)
        base_months = [m["month"] for m in cm[ix - 3:ix]]

    def comp(field: str, limit: int) -> list[dict]:
        now: Counter = Counter()
        was: Counter = Counter()
        for r in recs:
            if r["ym"] == month:
                now[r[field]] += r["q"]
            elif r["ym"] in base_months:
                was[r[field]] += r["q"] / len(base_months)
        keys = sorted(set(now) | set(was), key=lambda k: -was.get(k, 0))[:limit]
        return [{"key": k, "now": now.get(k, 0), "before": _r(was.get(k, 0), 1), "change": _r(now.get(k, 0) / was[k] - 1, 3) if was.get(k, 0) >= 5 else None} for k in keys]

    by_type = comp("vtype", 6)
    by_part = comp("part", 8)
    by_store = comp("store", 4)
    fell = [r["change"] for r in by_part if r["change"] is not None]
    uniform = bool(fell) and sum(1 for c in fell if c <= -0.4) >= max(2, int(len(fell) * 0.75))

    # items that lost the most units
    item_now: Counter = Counter()
    item_was: Counter = Counter()
    titles: dict[str, str] = {}
    for r in recs:
        if not r["item"]:
            continue
        titles[r["item"]] = r["title"]
        if r["ym"] == month:
            item_now[r["item"]] += r["q"]
        elif r["ym"] in base_months:
            item_was[r["item"]] += r["q"] / len(base_months)
    lost = sorted(((item_was[i] - item_now.get(i, 0), i) for i in item_was), reverse=True)[:6]
    total_lost = sum(max(item_was[i] - item_now.get(i, 0), 0) for i in item_was) or 1
    top_items = [{"item_number": i, "title": titles.get(i, ""), "before": _r(item_was[i], 1), "now": item_now.get(i, 0), "share_of_loss": _r(l / total_lost, 3)} for l, i in lost]

    # a daily picture from a month before to a month after, to see a cliff versus a slow slide
    y, m = map(int, month.split("-"))
    start = (date(y, m, 1) - timedelta(days=31))
    end = month_end(date(y, m, 1)) + timedelta(days=31)
    daily = []
    lo_d, hi_d = min(by_day), max(by_day)
    cur = max(start, lo_d)
    while cur <= min(end, hi_d):
        daily.append({"date": cur.isoformat(), "orders": int(by_day[cur]["orders"]) if cur in by_day else 0})
        cur += timedelta(days=1)

    return {
        "month": month, "orders_change": _r(drop, 3), "baseline": base_label, "baseline_months": base_months,
        "by_type": by_type, "by_part": by_part, "by_store": by_store, "uniform": uniform, "top_items": top_items, "daily": daily,
    }


def _movers(recs, last_date: date) -> dict:
    """Last 90 days against the same 90 days a year earlier (so seasonality cancels out)."""
    cur_start = last_date - timedelta(days=89)
    prev_start, prev_end = cur_start - timedelta(days=365), last_date - timedelta(days=365)
    prior_exists = any(prev_start <= r["d"] <= prev_end for r in recs)
    res: dict[str, Any] = {"window": {"from": cur_start.isoformat(), "to": last_date.isoformat(), "has_prior_year": prior_exists}}

    def comp(field: str, limit: int) -> list[dict]:
        now: Counter = Counter()
        was: Counter = Counter()
        for r in recs:
            if cur_start <= r["d"] <= last_date:
                now[r[field]] += r["q"]
            elif prev_start <= r["d"] <= prev_end:
                was[r[field]] += r["q"]
        rows = []
        for k in set(now) | set(was):
            if now.get(k, 0) + was.get(k, 0) < 30:
                continue
            rows.append({"key": k, "now": now.get(k, 0), "before": was.get(k, 0), "change": _r(now.get(k, 0) / was[k] - 1, 3) if was.get(k, 0) else None})
        rows.sort(key=lambda r: -(r["now"] + r["before"]))
        return rows[:limit]

    res["by_part"] = comp("part", 10) if prior_exists else []
    res["by_type"] = comp("vtype", 7) if prior_exists else []

    # top sellers that stopped (stock-out or ended listing?) and items that took off
    titles: dict[str, str] = {}
    recent60 = last_date - timedelta(days=59)
    year_before = (last_date - timedelta(days=60 + 365), last_date - timedelta(days=60))
    prior_u: Counter = Counter()
    last60_u: Counter = Counter()
    now90: Counter = Counter()
    was90: Counter = Counter()
    for r in recs:
        if not r["item"]:
            continue
        titles[r["item"]] = r["title"]
        if year_before[0] <= r["d"] <= year_before[1]:
            prior_u[r["item"]] += r["q"]
        if r["d"] >= recent60:
            last60_u[r["item"]] += r["q"]
        if cur_start <= r["d"] <= last_date:
            now90[r["item"]] += r["q"]
        elif prev_start <= r["d"] <= prev_end:
            was90[r["item"]] += r["q"]
    res["stopped"] = [
        {"item_number": i, "title": titles[i], "units_before": prior_u[i]}
        for i in sorted((i for i in prior_u if prior_u[i] >= 12 and last60_u.get(i, 0) == 0), key=lambda i: -prior_u[i])[:10]
    ]
    res["risers"] = [
        {"item_number": i, "title": titles[i], "now": now90[i], "before": was90.get(i, 0)}
        for i in sorted((i for i in now90 if now90[i] >= 10 and now90[i] >= 2 * max(was90.get(i, 0), 1)), key=lambda i: -(now90[i] - was90.get(i, 0)))[:8]
    ] if prior_exists else []
    return res


def _spikes(recs) -> list[dict]:
    by_item: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    titles: dict[str, str] = {}
    for r in recs:
        if r["item"]:
            by_item[r["item"]][r["ym"]] += r["q"]
            titles[r["item"]] = r["title"]
    out = []
    for item, by in by_item.items():
        total = sum(by.values())
        if total < 30 or len(by) < 3:
            continue
        typical = statistics.median(by.values())
        for k, v in by.items():
            if v >= 10 and v >= 3 * max(typical, 1):
                if by.get(prev_year(k), 0) >= 2 * max(typical, 1):
                    continue  # the same month was busy a year ago too: that is a season, not a spike
                out.append({"item_number": item, "title": titles[item], "month": k, "units": v, "typical": _r(typical, 1)})
    out.sort(key=lambda s: -(s["units"] / max(s["typical"], 1)))
    return out[:8]


def _power_row(buyer: str, lst, n_orders: int, revenue: float) -> dict:
    """One heavy buyer: who, how much, where, how long they have been buying and what they mostly buy."""
    recs = [r for _, _, rs in lst for r in rs]
    dates = [d for d, _, _ in lst]
    states = Counter(r["state"] for r in recs if r.get("state"))
    stores = sorted({r["store"] for r in recs})
    parts: Counter = Counter()
    makes: Counter = Counter()
    for r in recs:
        if not r["part"].startswith("Other"):
            parts[r["part"]] += r["rev"]
        if r["make"] != "Unknown":
            makes[r["make"]] += r["rev"]
    return {
        "who": buyer or "(unknown)",
        "orders": n_orders,
        "revenue": _r(revenue, 0),
        "avg_order": _r(revenue / n_orders, 0) if n_orders else 0,
        "state": states.most_common(1)[0][0] if states else None,
        "first": min(dates).isoformat(),
        "last": max(dates).isoformat(),
        "stores": stores,
        "buys": [k for k, _ in parts.most_common(3)],
        "make": makes.most_common(1)[0][0] if makes else None,
    }


def _buyers(recs, complete, first_date, last_date) -> dict:
    orders: dict[tuple, list[dict]] = defaultdict(list)
    for r in recs:
        orders[r["order"]].append(r)
    n_orders = len(orders)

    # --- basket
    lines_per = Counter()
    pair_c: Counter = Counter()
    cat_c: Counter = Counter()
    for _, rs in orders.items():
        cats = sorted({r["part"] for r in rs if not r["part"].startswith("Other")})
        lines_per["1" if len(rs) == 1 else "2" if len(rs) == 2 else "3+"] += 1
        for c in cats:
            cat_c[c] += 1
        for a, b in combinations(cats, 2):
            pair_c[(a, b)] += 1
    pairs = []
    for (a, b), c in pair_c.items():
        if c < 15:
            continue
        lift = c * n_orders / (cat_c[a] * cat_c[b])
        pairs.append({"a": a, "b": b, "orders": c, "lift": _r(lift, 2)})
    pairs.sort(key=lambda p: -(p["lift"] * min(p["orders"], 100)))

    qty = Counter()
    for r in recs:
        qty["1" if r["q"] == 1 else "2" if r["q"] == 2 else "3-4" if r["q"] <= 4 else "5+"] += 1

    # --- per buyer
    per: dict[str, list[tuple[date, tuple, list[dict]]]] = defaultdict(list)
    for key, rs in orders.items():
        b = rs[0]["buyer"]
        if b:
            per[b].append((rs[0]["d"], key, rs))
    gaps: list[int] = []
    trans: Counter = Counter()
    same_make = same_type = comparable_make = comparable_type = 0
    buyer_orders: Counter = Counter()
    buyer_rev: Counter = Counter()
    for b, lst in per.items():
        lst.sort(key=lambda t: (t[0], t[1]))
        buyer_orders[b] = len(lst)
        buyer_rev[b] = sum(r["rev"] for _, _, rs in lst for r in rs)
        for (d1, _, r1), (d2, _, r2) in zip(lst, lst[1:]):
            gaps.append((d2 - d1).days)
            top1 = max(r1, key=lambda r: r["rev"])
            top2 = max(r2, key=lambda r: r["rev"])
            if not top1["part"].startswith("Other") and not top2["part"].startswith("Other"):
                trans[(top1["part"], top2["part"])] += 1
            if top1["make"] != "Unknown" and top2["make"] != "Unknown":
                comparable_make += 1
                same_make += top1["make"] == top2["make"]
            if top1["vtype"] != "Unknown" and top2["vtype"] != "Unknown":
                comparable_type += 1
                same_type += top1["vtype"] == top2["vtype"]
    bins = [("Same week", 0, 7), ("8-30 days", 8, 30), ("1-3 months", 31, 90), ("3-12 months", 91, 365), ("Over a year", 366, 10**6)]
    gap_hist = [{"label": lab, "count": sum(1 for g in gaps if lo <= g <= hi)} for lab, lo, hi in bins]

    n_buyers = len(per)
    repeat_buyers = sum(1 for v in buyer_orders.values() if v >= 2)
    power = [b for b, n in buyer_orders.items() if n >= 5]
    tot_rev = sum(buyer_rev.values()) or 1
    top_power = sorted(power, key=lambda b: -buyer_rev[b])[:10]

    # --- day of month (spending rhythm): orders per calendar day-of-month, corrected for how many of each day exist
    dom_orders: Counter = Counter()
    for _, rs in orders.items():
        dom_orders[rs[0]["d"].day] += 1
    dom_days: Counter = Counter()
    cur = first_date
    while cur <= last_date:
        dom_days[cur.day] += 1
        cur += timedelta(days=1)
    per_day = {d: dom_orders.get(d, 0) / dom_days[d] for d in range(1, 32) if dom_days.get(d)}
    mean_pd = sum(per_day.values()) / len(per_day) if per_day else 1
    dom = [{"day": d, "index": _r(per_day[d] / mean_pd, 2) if d in per_day else None} for d in range(1, 32)]

    # --- weekday by vehicle type is skipped on purpose (too thin); weekend share only
    wk = Counter()
    for _, rs in orders.items():
        wk["weekend" if rs[0]["d"].weekday() >= 5 else "weekday"] += 1

    return {
        "orders": n_orders, "buyers": n_buyers,
        "lines_per_order": [{"label": k, "count": lines_per.get(k, 0)} for k in ("1", "2", "3+")],
        "qty_per_line": [{"label": k, "count": qty.get(k, 0)} for k in ("1", "2", "3-4", "5+")],
        "pairs": pairs[:8],
        "repeat": {
            "buyers_with_repeat": repeat_buyers, "share": _r(repeat_buyers / n_buyers, 3) if n_buyers else 0,
            "median_gap_days": statistics.median(gaps) if gaps else None, "gap_hist": gap_hist, "pairs_total": len(gaps),
            "same_make_share": _r(same_make / comparable_make, 3) if comparable_make else None,
            "same_type_share": _r(same_type / comparable_type, 3) if comparable_type else None,
            "transitions": [{"from": a, "to": b, "count": c} for (a, b), c in trans.most_common(8)],
        },
        "power": {
            "buyers": len(power), "share_of_buyers": _r(len(power) / n_buyers, 4) if n_buyers else 0,
            "share_of_revenue": _r(sum(buyer_rev[b] for b in power) / tot_rev, 3),
            "top": [_power_row(b, per[b], buyer_orders[b], buyer_rev[b]) for b in top_power],
        },
        "day_of_month": dom,
        "weekend_share": _r(wk["weekend"] / max(wk["weekend"] + wk["weekday"], 1), 3),
    }


def _geo(recs) -> list[dict]:
    """For each vehicle type: the states where it sells MORE than the store's overall mix would predict."""
    tot_state: Counter = Counter()
    by_type_state: dict[str, Counter] = defaultdict(Counter)
    seen_orders: set = set()
    for r in recs:
        key = (r["order"], r["vtype"])
        st = r["state"]
        if not st or len(st) != 2:
            continue
        if key in seen_orders:
            continue
        seen_orders.add(key)
        tot_state[st] += 1
        by_type_state[r["vtype"]][st] += 1
    grand = sum(tot_state.values()) or 1
    res = []
    for vt, c in by_type_state.items():
        n = sum(c.values())
        if vt == "Unknown" or n < 150:
            continue
        rows = []
        for st, k in c.items():
            if k < 25:
                continue
            share_type = k / n
            share_all = tot_state[st] / grand
            rows.append({"state": st, "orders": k, "share": _r(share_type, 3), "overall_share": _r(share_all, 3), "lq": _r(share_type / share_all, 2)})
        rows.sort(key=lambda r: -r["lq"])
        res.append({"type": vt, "orders": n, "states": rows[:5]})
    res.sort(key=lambda r: -r["orders"])
    return res
