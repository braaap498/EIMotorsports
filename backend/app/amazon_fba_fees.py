"""FBA fee estimate and floor price, for the Buy Box checks (new file, goes next to amazon_buybox.py).

An FBA listing is shipped by Amazon, so its floor price must not use our shipping estimate. Instead it uses Amazon's own fee estimate
(SP-API Product Fees, getMyFeesEstimateForSKU with IsAmazonFulfilled = true). Referral fee scales with price; the other fees (FBA
fulfillment, storage-type fees) are treated as fixed, which is accurate enough for a floor.

Not yet used by amazon_buybox.py: that file was not uploaded. To use it, for an FBA row call
    floor_price(db, account, marketplace, sku, cost, ref_price, margin_pct)
instead of the merchant-fulfilled floor (cost + shipping estimate + referral + margin). Compiles; the fee-call response shape is
unverified against Amazon, the floor math is covered by the self-test at the bottom (run: python amazon_fba_fees.py).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.amazon_core import MARKETPLACES, NA_ENDPOINT, access_token

CURRENCY = {"US": "USD", "CA": "CAD", "MX": "MXN", "BR": "BRL"}

CACHE_SECONDS = 24 * 3600        # a fee estimate is reused for a day: it is one API call per listing and the limit is low
FAIL_SECONDS = 15 * 60           # after a failed estimate, do not ask Amazon again for this SKU for 15 minutes (a bad SKU would otherwise cost one throttled call per Buy Box run)
MIN_GAP_SECONDS = 1.1            # getMyFeesEstimateForSKU allows about one call a second
_cache: dict[tuple, tuple[float, float, float]] = {}   # (account, marketplace, sku) -> (referral rate, fixed fees, saved at); per process
_failed: dict[tuple, float] = {}   # same key -> time of the last failed estimate; per process
_lock = threading.Lock()
_last_call = 0.0


def _throttle() -> None:
    """Waits so two estimate calls never go out less than MIN_GAP_SECONDS apart (this process only)."""
    global _last_call
    with _lock:
        wait = MIN_GAP_SECONDS - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def split_fees(result: dict, ref_price: float) -> tuple[float, float]:
    """From one FeesEstimateResult returns (referral_rate, fixed_fees). Referral rate = referral fee / price; fixed = every other fee."""
    est = (result or {}).get("FeesEstimate") or {}
    total = float(((est.get("TotalFeesEstimate") or {}).get("Amount")) or 0)
    referral = 0.0
    for f in est.get("FeeDetailList") or []:
        if str(f.get("FeeType", "")).lower() == "referralfee":
            referral += float(((f.get("FeeAmount") or {}).get("Amount")) or 0)
    if ref_price <= 0 or total <= 0:
        raise ValueError("No usable fee estimate.")
    return referral / ref_price, max(total - referral, 0.0)


def floor_from_fees(cost: float, referral_rate: float, fixed_fees: float, margin_pct: float) -> float:
    """Lowest price that still leaves margin_pct of the price after cost, the referral fee and the fixed (FBA) fees."""
    keep = 1.0 - referral_rate - margin_pct
    if keep <= 0.05:
        raise ValueError("Fees plus margin leave nothing to price with; lower the margin.")
    return round((cost + fixed_fees) / keep, 2)


def estimate(db: Session, account: str, marketplace: str, sku: str, ref_price: float) -> dict:
    """Asks Amazon what it would charge to sell and ship this SKU through FBA at ref_price."""
    if marketplace not in MARKETPLACES:
        raise HTTPException(422, "Unknown marketplace.")
    body = {"FeesEstimateRequest": {
        "MarketplaceId": MARKETPLACES[marketplace], "IsAmazonFulfilled": True, "Identifier": f"fba-{sku}"[:60],
        "PriceToEstimateFees": {"ListingPrice": {"CurrencyCode": CURRENCY[marketplace], "Amount": round(float(ref_price), 2)}},
    }}
    url = f"{NA_ENDPOINT}/products/fees/v0/listings/{urllib.parse.quote(sku, safe='')}/feesEstimate"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"x-amz-access-token": access_token(db, account), "Content-Type": "application/json"})
    _throttle()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode() or "{}").get("payload") or {}
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"Amazon refused the fee estimate ({e.code}): {e.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise HTTPException(502, "Could not reach Amazon.")
    res = payload.get("FeesEstimateResult") or {}
    if str(res.get("Status", "")).lower() in ("clienterror", "serviceerror") or res.get("Error"):
        raise HTTPException(502, f"Amazon could not estimate fees for {sku}: {str((res.get('Error') or {}).get('Message', ''))[:200]}")
    return res


def fee_parts(db: Session, account: str, marketplace: str, sku: str, ref_price: float, use_cache: bool = True) -> Optional[tuple[float, float]]:
    """(referral rate, fixed fees) for one FBA listing, from the cache when it is under a day old, otherwise from Amazon.
    None when Amazon gave no usable estimate or could not be reached."""
    key = (account, marketplace, sku)
    now = time.time()
    hit = _cache.get(key)
    if use_cache and hit and now - hit[2] < CACHE_SECONDS:
        return hit[0], hit[1]
    if use_cache and now - _failed.get(key, 0.0) < FAIL_SECONDS:
        return None                   # failed a moment ago: do not call Amazon again yet
    try:
        rate, fixed = split_fees(estimate(db, account, marketplace, sku, ref_price), ref_price)
    except ValueError:
        _failed[key] = now
        return None
    except HTTPException as e:
        if e.status_code == 422:      # unknown marketplace: a bug in the caller, not an Amazon problem
            raise
        _failed[key] = now
        return None                   # Amazon refused or was unreachable: the caller must not lower the price
    _failed.pop(key, None)
    _cache[key] = (rate, fixed, now)
    return rate, fixed


def floor_price(db: Session, account: str, marketplace: str, sku: str, cost: float, ref_price: float, margin_pct: float = 0.10,
                use_cache: bool = True) -> Optional[float]:
    """The FBA floor for one listing, or None when Amazon gave no usable estimate (callers must then NOT lower the price)."""
    parts = fee_parts(db, account, marketplace, sku, ref_price, use_cache)
    if parts is None:
        return None
    try:
        return floor_from_fees(cost, parts[0], parts[1], margin_pct)
    except ValueError:
        return None


if __name__ == "__main__":      # self-test of the pure math, no network
    fake = {"FeesEstimate": {"TotalFeesEstimate": {"Amount": 8.0}, "FeeDetailList": [
        {"FeeType": "ReferralFee", "FeeAmount": {"Amount": 3.0}}, {"FeeType": "FBAFees", "FeeAmount": {"Amount": 5.0}}]}}
    rate, fixed = split_fees(fake, 20.0)
    assert (rate, fixed) == (0.15, 5.0), (rate, fixed)
    fl = floor_from_fees(10.0, rate, fixed, 0.10)           # (10+5)/(1-.15-.10) = 20.0
    assert fl == 20.0, fl
    try:
        floor_from_fees(10.0, 0.5, 5.0, 0.5)
        raise SystemExit("should have refused")
    except ValueError:
        pass
    # the cache returns a stored estimate without calling Amazon
    _cache[("acct", "US", "SKU1")] = (0.15, 5.0, time.time())
    assert floor_price(None, "acct", "US", "SKU1", 10.0, 20.0) == 20.0
    _cache[("acct", "US", "SKU2")] = (0.15, 5.0, time.time() - CACHE_SECONDS - 1)       # too old: would call Amazon
    try:
        fee_parts(None, "acct", "US", "SKU2", 20.0)
        raise SystemExit("an expired entry should have gone to Amazon")
    except Exception as e:
        assert not isinstance(e, SystemExit), e
    # a failed estimate is remembered, so a second call does not reach Amazon again
    _failed[("acct", "US", "SKU3")] = time.time()
    assert fee_parts(None, "acct", "US", "SKU3", 20.0) is None      # would raise if it tried the network with db=None
    print("ok", rate, fixed, fl)
