"""Domain watches. The countdown target lives in state, not in the deploy.

A morning ingest that omits `watches` leaves the saved rows alone. A payload
that includes `watches` updates only the ids it names. POST /api/watches/<id>
does the same for one row. With nothing saved, the board shows the fifi4.com
default.
"""
import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}\Z")
_FIELDS = ("title", "target_iso", "target_tz", "status", "note", "url")
PT = "America/Los_Angeles"

DEFAULT = {
    "id": "fifi4.com",
    "title": "fifi4.com",
    "target_iso": "2026-11-05T00:00:00",
    "target_tz": PT,
    "status": "Expired at Dynadot Oct 6. Watching every 2 h for auction or renewal.",
    "note": "Nov 5 is an estimate based on Dynadot's 30-day grace period.",
    "url": "https://fifi4.com",
}


def check_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("id is not valid")
    return value


def _zone(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("target_tz must be an IANA time zone")
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError) as ex:
        raise ValueError("target_tz must be an IANA time zone") from ex


def _text(value, field, limit):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = " ".join(value.split())
    if len(text) > limit:
        raise ValueError(f"{field} is too long")
    return text


def _url(value):
    if value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError("url must be an http or https url")
    text = value.strip()
    if not (text.startswith("https://") or text.startswith("http://")):
        raise ValueError("url must be an http or https url")
    if any(char in text for char in " \n\r\t\"<>"):
        raise ValueError("url must be an http or https url")
    return text


def parse_instant(target_iso, target_tz):
    """UTC instant. A value with no offset is local time in target_tz."""
    if not isinstance(target_iso, str) or not target_iso.strip():
        raise ValueError("target_iso is required")
    text = target_iso.strip()
    zone = _zone(target_tz or PT)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) == 16 and text[10] == "T":
        text += ":00"
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError as ex:
        raise ValueError("target_iso must be an ISO time") from ex
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(dt.timezone.utc)


def _blank(watch_id):
    return {
        "id": watch_id,
        "title": watch_id,
        "target_iso": "",
        "target_tz": PT,
        "status": "",
        "note": "",
        "url": "",
    }


def _rows(state):
    rows = state.get("watches")
    if not isinstance(rows, dict):
        rows = {}
        state["watches"] = rows
    return rows


def _clean_row(watch_id, raw, base):
    row = dict(base)
    row["id"] = watch_id
    if not isinstance(raw, dict):
        raise ValueError("watch must be an object")
    if raw.get("target_tz") is not None:
        _zone(raw["target_tz"])
        row["target_tz"] = raw["target_tz"].strip()
    for key in _FIELDS:
        if key == "target_tz" or key not in raw or raw[key] is None:
            continue
        if key == "url":
            row[key] = _url(raw[key])
        elif key == "target_iso":
            text = _text(raw[key], key, 40)
            row[key] = text
        elif key == "title":
            row[key] = _text(raw[key], key, 80) or watch_id
        else:
            row[key] = _text(raw[key], key, 240)
    if row.get("target_iso"):
        parse_instant(row["target_iso"], row.get("target_tz") or PT)
    return row


def apply_payload(state, rows):
    """Update named watches. Does not delete rows the payload left out."""
    if not isinstance(rows, list):
        raise ValueError("watches must be a list")
    if len(rows) > 20:
        raise ValueError("watches is too long")
    saved = _rows(state)
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("watch must be an object")
        watch_id = check_id(raw.get("id"))
        base = dict(saved.get(watch_id) or (DEFAULT if watch_id == DEFAULT["id"] else _blank(watch_id)))
        saved[watch_id] = _clean_row(watch_id, raw, base)
    return saved


def update(state, watch_id, body):
    watch_id = check_id(watch_id)
    if not isinstance(body, dict) or not any(key in body for key in _FIELDS):
        raise ValueError("body must include a field to change")
    saved = _rows(state)
    base = dict(saved.get(watch_id) or (DEFAULT if watch_id == DEFAULT["id"] else _blank(watch_id)))
    saved[watch_id] = _clean_row(watch_id, body, base)
    return dict(saved[watch_id])


def visible(state):
    """Rows to draw. The fifi4.com default is used until something saves it."""
    saved = state.get("watches") if isinstance(state, dict) and isinstance(state.get("watches"), dict) else {}
    rows = []
    if DEFAULT["id"] not in saved:
        rows.append(dict(DEFAULT))
    for watch_id, raw in saved.items():
        if isinstance(raw, dict):
            row = dict(raw)
            row["id"] = watch_id
            rows.append(row)
    return rows


def remaining_phrase(seconds, with_seconds=True):
    if seconds <= 0:
        return "Target reached, checking status"
    whole = int(seconds)
    days, rem = divmod(whole, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    if with_seconds:
        parts.append(f"{secs}s")
    return " ".join(parts)
