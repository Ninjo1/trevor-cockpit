"""Current conditions and a 3-day outlook from Open-Meteo.

The browser never calls Open-Meteo. Results are cached for 30 minutes in
memory and, when a data directory is set, on disk. A failed fetch becomes
"weather unavailable" and does not raise.
"""
import json
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request

CACHE_SECONDS = 30 * 60
_MEM = {}
_LOCK = threading.Lock()

_CODES = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Fog",
    51: "Drizzle",
    53: "Drizzle",
    55: "Drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm",
    99: "Thunderstorm",
}


def sky(code):
    try:
        return _CODES.get(int(code), "Unknown")
    except (TypeError, ValueError):
        return "Unknown"


def _http_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "trevor-cockpit"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def forecast_url(lat, lon):
    query = urllib.parse.urlencode(
        {
            "latitude": f"{float(lat):.4f}",
            "longitude": f"{float(lon):.4f}",
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "forecast_days": "3",
            "timezone": "auto",
            "wind_speed_unit": "kmh",
        }
    )
    return "https://api.open-meteo.com/v1/forecast?" + query


def _round(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _place_key(places):
    parts = []
    for place in places:
        if not isinstance(place, dict):
            continue
        name = str(place.get("name") or "").strip()
        try:
            lat = round(float(place.get("lat")), 3)
            lon = round(float(place.get("lon")), 3)
        except (TypeError, ValueError):
            continue
        parts.append(f"{name}|{lat}|{lon}")
    return ";".join(parts)


def _normalize(place, payload):
    current = payload.get("current") if isinstance(payload, dict) else None
    daily = payload.get("daily") if isinstance(payload, dict) else None
    if not isinstance(current, dict) or not isinstance(daily, dict):
        raise ValueError("forecast shape")
    days = []
    times = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    for index, day in enumerate(list(times)[:3]):
        days.append(
            {
                "date": str(day),
                "high": _round(highs[index]) if index < len(highs) else None,
                "low": _round(lows[index]) if index < len(lows) else None,
                "sky": sky(codes[index]) if index < len(codes) else "",
            }
        )
    if not days:
        raise ValueError("forecast days")
    return {
        "name": str(place.get("name") or "").strip() or "Place",
        "unavailable": False,
        "temp": _round(current.get("temperature_2m")),
        "sky": sky(current.get("weather_code")),
        "wind": _round(current.get("wind_speed_10m")),
        "days": days,
    }


def _unavailable(place):
    name = ""
    if isinstance(place, dict):
        name = str(place.get("name") or "").strip()
    return {"name": name or "Place", "unavailable": True}


def _read_disk(path, key, now):
    try:
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(saved, dict) or saved.get("key") != key:
        return None
    saved_at = saved.get("saved_at")
    rows = saved.get("rows")
    if not isinstance(saved_at, (int, float)) or not isinstance(rows, list):
        return None
    if now - float(saved_at) > CACHE_SECONDS:
        return None
    return rows


def _write_disk(path, key, now, rows):
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-weather-", dir=directory)
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"key": key, "saved_at": now, "rows": rows}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def get_forecasts(places, data_dir=None, fetcher=None, clock=None, cache=None):
    """Return one row per place. Never raises.

    fetcher(url) -> dict. clock() -> unix seconds. cache is an optional dict
    used instead of the process cache, for tests.
    """
    if not isinstance(places, list) or not places:
        return []
    usable = [place for place in places if isinstance(place, dict)][:8]
    key = _place_key(usable)
    if not key:
        return [_unavailable(place) for place in usable]
    now = float(clock() if clock else time.time())
    store = _MEM if cache is None else cache
    disk = os.path.join(data_dir, "weather-cache.json") if data_dir else None
    with _LOCK:
        hit = store.get(key)
        if hit and now - float(hit[0]) <= CACHE_SECONDS:
            return hit[1]
        if disk:
            rows = _read_disk(disk, key, now)
            if rows is not None:
                store[key] = (now, rows)
                return rows
    fetch = fetcher or _http_json
    rows = []
    for place in usable:
        try:
            lat = float(place.get("lat"))
            lon = float(place.get("lon"))
        except (TypeError, ValueError):
            rows.append(_unavailable(place))
            continue
        try:
            payload = fetch(forecast_url(lat, lon))
            rows.append(_normalize(place, payload))
        except Exception:
            rows.append(_unavailable(place))
    with _LOCK:
        store[key] = (now, rows)
        if disk:
            _write_disk(disk, key, now, rows)
    return rows
