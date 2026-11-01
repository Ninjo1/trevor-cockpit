"""Queued decisions. The board records a tap and does not send it.

A later assistant, using VOICE_PICKUP_TOKEN, reads the queue and reports
done or failed. Nothing here calls Outlook, a calendar, or an airline.
"""
import datetime as dt
import re
import uuid
from urllib.parse import quote

_DECISION_ID = re.compile(r"D-[A-Za-z0-9]{8,24}\Z")
_CHOICES = {
    "draft": {"approve", "skip"},
    "meeting": {"accept", "decline"},
    "checkin": {"checkin"},
}
_OPEN = {"none", "tentative"}
DELTA_CHECKIN = "https://www.delta.com/mytrips/"


def _http(url):
    if not isinstance(url, str):
        return ""
    text = url.strip()
    if not (text.startswith("https://") or text.startswith("http://")):
        return ""
    if any(char in text for char in " \n\r\t\"<>"):
        return ""
    return text


def _drafts(data):
    found = []
    for item in (data or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        for link in item.get("links") or []:
            if not isinstance(link, dict) or link.get("kind") != "draft":
                continue
            href = _http(link.get("url") or link.get("href"))
            if not href:
                continue
            found.append({
                "type": "draft",
                "target": href,
                "label": str(item.get("subject") or item.get("title") or "Email draft"),
                "href": href,
            })
    return found


def _meetings(data):
    found = []
    for event in (data or {}).get("calendar") or []:
        if not isinstance(event, dict) or not event.get("id"):
            continue
        response = str(event.get("response") or "").strip().lower()
        if response not in _OPEN:
            continue
        found.append({
            "type": "meeting",
            "target": str(event.get("id")),
            "label": str(event.get("subject") or "Meeting"),
            "detail": response,
        })
    return found


def _checkins(data, now):
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    found = []
    for row in (data or {}).get("travel") or []:
        if not isinstance(row, dict) or (row.get("kind") or "other") != "flight":
            continue
        from render import utc

        start = utc(row.get("start"))
        if not start:
            continue
        ahead = (start - now).total_seconds()
        if ahead <= 0 or ahead > 24 * 3600:
            continue
        title = str(row.get("title") or "Flight")
        confirmation = str(row.get("confirmation") or "").strip()
        target = title if not confirmation else f"{title}|{confirmation}"
        href = DELTA_CHECKIN
        if confirmation:
            href = DELTA_CHECKIN + "?confirmationNumber=" + quote(confirmation)
        found.append({
            "type": "checkin",
            "target": target,
            "label": title,
            "detail": confirmation,
            "href": href,
        })
    return found


def proposals(data, now):
    """Decisions the board can offer right now. It does not act on them."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return _drafts(data) + _meetings(data) + _checkins(data, now)


def _rows(state):
    rows = state.get("decisions")
    if not isinstance(rows, list):
        rows = []
        state["decisions"] = rows
    return rows


def _queued(state, kind, target):
    for row in _rows(state):
        if isinstance(row, dict) and row.get("status") == "queued" and row.get("type") == kind and row.get("target") == target:
            return row
    return None


def queue_decision(state, data, body, now_iso, now):
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    kind = body.get("type")
    target = body.get("target")
    if kind not in _CHOICES:
        raise ValueError("type must be draft, meeting, or checkin")
    if not isinstance(target, str) or not target.strip() or len(target) > 500:
        raise ValueError("target is required")
    target = target.strip()
    offered = {row["target"] for row in proposals(data, now) if row["type"] == kind}
    if body.get("undo") in (True, "1", 1, "true"):
        current = _queued(state, kind, target)
        if current is None:
            raise KeyError(target)
        _rows(state).remove(current)
        return {"undone": True, "id": current.get("id"), "type": kind, "target": target}
    choice = body.get("choice")
    if choice not in _CHOICES[kind]:
        raise ValueError("choice is not valid for this type")
    if target not in offered and _queued(state, kind, target) is None:
        raise ValueError("that decision is not available")
    current = _queued(state, kind, target)
    if current is not None:
        current["choice"] = choice
        current["ts"] = now_iso
        return dict(current)
    record = {
        "id": "D-" + uuid.uuid4().hex[:12],
        "type": kind,
        "target": target,
        "choice": choice,
        "status": "queued",
        "ts": now_iso,
        "result_note": "",
    }
    _rows(state).append(record)
    return dict(record)


def list_decisions(state, status="queued"):
    return [dict(row) for row in _rows(state) if isinstance(row, dict) and row.get("status") == status]


def record_result(state, decision_id, status, note, now_iso):
    if not isinstance(decision_id, str) or not _DECISION_ID.fullmatch(decision_id):
        raise ValueError("id is not valid")
    if status not in ("done", "failed"):
        raise ValueError("status must be done or failed")
    if note not in (None, "") and not isinstance(note, str):
        raise ValueError("note must be a string")
    if isinstance(note, str) and len(note) > 500:
        raise ValueError("note is too long")
    for row in _rows(state):
        if isinstance(row, dict) and row.get("id") == decision_id:
            row["status"] = status
            row["result_note"] = (note or "").strip()
            row["ts"] = now_iso
            return dict(row)
    raise KeyError(decision_id)
