"""Exchange rates for the board and the small converter.

Bank of Canada Valet publishes CAD per USD, CAD per THB, and CAD per JPY.
USD per THB and USD per JPY are those rates divided by CAD per USD.
If Valet is missing a series, Frankfurter is the fallback. Results cache
for 30 minutes. A failed fetch does not raise.
"""
import json
import time
import urllib.request

CACHE_SECONDS = 30 * 60
_MEM = {"at": 0.0, "rates": None}
VALET = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD,FXTHBCAD,FXJPYCAD/json?recent=1"
FRANKFURTER = "https://api.frankfurter.app/latest?from=USD&to=CAD,THB,JPY"


def _http_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "trevor-cockpit"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _from_valet(payload):
    obs = (payload.get("observations") or [None])[-1] or {}
    def val(key):
        raw = obs.get(key) or {}
        try:
            return float(raw.get("v"))
        except (TypeError, ValueError):
            return None
    usd = val("FXUSDCAD")
    thb = val("FXTHBCAD")
    jpy = val("FXJPYCAD")
    if not usd or usd <= 0:
        raise ValueError("valet usd")
    cad_per = {"CAD": 1.0, "USD": usd}
    if thb and thb > 0:
        cad_per["THB"] = thb
    if jpy and jpy > 0:
        cad_per["JPY"] = jpy
    return {
        "date": obs.get("d") or "",
        "source": "Bank of Canada Valet",
        "cad_per": cad_per,
    }


def _from_frankfurter(payload, have):
    rates = payload.get("rates") or {}
    cad = float(rates["CAD"])
    cad_per = dict(have)
    cad_per["CAD"] = 1.0
    cad_per["USD"] = cad
    for code in ("THB", "JPY"):
        if code in cad_per:
            continue
        per_usd = float(rates[code])
        if per_usd <= 0:
            continue
        cad_per[code] = cad / per_usd
    return {
        "date": payload.get("date") or "",
        "source": "Frankfurter",
        "cad_per": cad_per,
    }


def usd_per(cad_per):
    usd = cad_per.get("USD") or 0
    out = {}
    if usd <= 0:
        return out
    for code, cad in cad_per.items():
        out[code] = cad / usd
    return out


def get_rates(fallback_usd_cad=None, fetcher=None, clock=None):
    """Return cad_per, usd_per, source, and date. Never raises."""
    now = float(clock() if clock else time.time())
    if _MEM["rates"] and now - _MEM["at"] <= CACHE_SECONDS and fetcher is None:
        return _MEM["rates"]
    fetch = fetcher or _http_json
    rates = None
    try:
        rates = _from_valet(fetch(VALET))
    except Exception:
        rates = None
    missing = rates is None or "THB" not in (rates or {}).get("cad_per", {}) or "JPY" not in (rates or {}).get("cad_per", {})
    if missing:
        try:
            base = (rates or {}).get("cad_per") or {}
            frank = _from_frankfurter(fetch(FRANKFURTER), base)
            if rates and rates.get("source") == "Bank of Canada Valet" and frank["source"] == "Frankfurter":
                frank["source"] = "Bank of Canada Valet and Frankfurter"
                frank["date"] = rates.get("date") or frank["date"]
            rates = frank
        except Exception:
            if rates is None and fallback_usd_cad:
                rates = {
                    "date": "",
                    "source": "data file",
                    "cad_per": {"CAD": 1.0, "USD": float(fallback_usd_cad)},
                }
    if rates is None:
        rates = {"date": "", "source": "unavailable", "cad_per": {"CAD": 1.0}}
    rates["usd_per"] = usd_per(rates["cad_per"])
    if fetcher is None:
        _MEM["at"] = now
        _MEM["rates"] = rates
    return rates
