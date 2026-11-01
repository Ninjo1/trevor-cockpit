"""Board events for a later learning pass.

The board appends one record when Trevor ticks, snoozes, moves, handles
mail, taps a decision, picks an area, ticks a Robbins question, or opens
a restaurant link. It stores ids, labels, and areas. It does not store
email bodies or note text. Rows older than 180 days are dropped.
"""
import datetime as dt
import uuid

import render
from actions import AREAS, area_of

RETENTION_DAYS = 180
DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000
FILTERS = ("All",) + tuple(AREAS)
QUADRANTS = ("do_now", "schedule", "delegate", "delete")


def stamp(moment):
    """ISO UTC with milliseconds, so two taps in one second stay ordered."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    moment = moment.astimezone(dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("since must be an ISO UTC time")
    text = value.strip()
    if len(text) < 20 or "T" not in text:
        raise ValueError("since must be an ISO UTC time")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) < 6 or text[-6] not in "+-" or text[-3] != ":":
        raise ValueError("since must be an ISO UTC time")
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError as ex:
        raise ValueError("since must be an ISO UTC time") from ex
    if moment.tzinfo is None:
        raise ValueError("since must be an ISO UTC time")
    return moment.astimezone(dt.timezone.utc)


def parse_limit(value):
    if value is None or value == "":
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, str) or not value.isdecimal():
        raise ValueError("limit must be a positive integer")
    number = int(value)
    if number < 1 or number > MAX_LIMIT:
        raise ValueError("limit must be a positive integer")
    return number


def _rows(state):
    rows = state.get("events")
    if not isinstance(rows, list):
        rows = []
        state["events"] = rows
    return rows


def prune(state, now):
    """Drop events older than 180 days. Returns True when the list changes."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    cutoff = now.astimezone(dt.timezone.utc) - dt.timedelta(days=RETENTION_DAYS)
    rows = state.get("events")
    if not isinstance(rows, list):
        state["events"] = []
        return True
    kept = []
    changed = False
    for row in rows:
        if not isinstance(row, dict):
            changed = True
            continue
        try:
            moment = parse_time(row.get("ts"))
        except ValueError:
            changed = True
            continue
        if moment < cutoff:
            changed = True
            continue
        kept.append(row)
    if changed:
        state["events"] = kept
    return changed


def _label(value, limit=180):
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _event(kind, moment, item_id="", area="", data=None):
    row = {"id": "E-" + uuid.uuid4().hex[:12], "ts": stamp(moment), "type": kind, "data": data or {}}
    if item_id:
        row["item_id"] = item_id
    if area:
        row["area"] = area
    return row


def append(state, event, now):
    _rows(state).append(event)
    prune(state, now)
    return event


def _find_item(data, state, item_id):
    overlay = ((state or {}).get("todos") or {}).get(item_id) or {}
    for item in (data or {}).get("items") or []:
        if isinstance(item, dict) and item.get("id") == item_id:
            area = overlay.get("area") or item.get("area") or ""
            title = overlay.get("title") or item.get("title") or item.get("subject") or ""
            due = overlay.get("due") if "due" in overlay and overlay.get("due") is not None else item.get("due") or ""
            return {
                "area": area_of(area) if area else "",
                "label": _label(title),
                "kind": item.get("kind") or "task",
                "quadrant": overlay.get("quadrant") or item.get("quadrant"),
                "section": item.get("section") or "open",
                "due": due or "",
                "priority": item.get("priority"),
                "important": item.get("important"),
                "owner": overlay.get("owner") or item.get("owner") or "",
            }
    for custom in (state or {}).get("custom") or []:
        if isinstance(custom, dict) and custom.get("id") == item_id:
            return {
                "area": area_of(custom.get("area") or ""),
                "label": _label(custom.get("title") or ""),
                "kind": "task",
                "quadrant": overlay.get("quadrant") or custom.get("quadrant"),
                "section": custom.get("section") or "open",
                "due": custom.get("due") or "",
                "priority": custom.get("priority"),
                "important": custom.get("important"),
                "owner": custom.get("owner") or "",
            }
    return None


def _with_label(data, label):
    if label:
        data["label"] = label
    return data


def capture(data, state, op, body, today):
    """Read the item before the action changes it."""
    if not isinstance(body, dict):
        return {}
    item_id = body.get("id") if isinstance(body.get("id"), str) else ""
    found = _find_item(data, state, item_id) if item_id else None
    captured = dict(found or {})
    if op == "todo/quadrant" and found:
        explicit = found.get("quadrant")
        item = {
            "quadrant": explicit if explicit in QUADRANTS else None,
            "important": found.get("important"),
            "priority": found.get("priority"),
            "section": found.get("section") or "open",
            "due": found.get("due") or "",
            "owner": found.get("owner") or "",
        }
        captured["from"] = render.derive_quadrant(item, today)
    if op == "question/toggle" and item_id:
        section, label = _question_label(data, item_id)
        captured["section"] = section
        captured["label"] = label
    return captured


def _question_label(data, item_id):
    principles = data.get("principles") if isinstance(data, dict) and isinstance(data.get("principles"), dict) else {}
    if "principles" not in (data or {}) or not principles:
        robbins = render.DEFAULT_ROBBINS
    else:
        robbins = principles.get("robbins") if isinstance(principles.get("robbins"), dict) and principles.get("robbins") else render.DEFAULT_ROBBINS
    for section in ("morning", "evening"):
        values = robbins.get(section) if isinstance(robbins, dict) else None
        if not isinstance(values, list):
            continue
        for text in values:
            if isinstance(text, str) and render.question_id(section, text) == item_id:
                return section, _label(text)
    return "", ""


def _decision_subject(data, kind, target):
    found = {"item_id": "", "area": "", "label": ""}
    if kind == "draft":
        for item in (data or {}).get("items") or []:
            if not isinstance(item, dict):
                continue
            for link in item.get("links") or []:
                if not isinstance(link, dict) or link.get("kind") != "draft":
                    continue
                href = str(link.get("url") or link.get("href") or "").strip()
                if href == target:
                    found["item_id"] = str(item.get("id") or "")
                    found["area"] = area_of(item.get("area") or "") if item.get("area") else ""
                    found["label"] = _label(item.get("title") or item.get("subject") or "")
                    return found
    if kind == "meeting":
        for event in (data or {}).get("calendar") or []:
            if isinstance(event, dict) and str(event.get("id") or "") == target:
                found["item_id"] = target
                found["label"] = _label(event.get("subject") or "")
                return found
    if kind == "checkin":
        title = target.split("|", 1)[0]
        confirmation = target.split("|", 1)[1] if "|" in target else ""
        for row in (data or {}).get("travel") or []:
            if not isinstance(row, dict):
                continue
            row_title = str(row.get("title") or "")
            row_conf = str(row.get("confirmation") or "").strip()
            if row_title == title and (not confirmation or row_conf == confirmation):
                found["item_id"] = str(row.get("id") or "")
                found["label"] = _label(row_title)
                return found
        found["label"] = _label(title)
    return found


def capture_decision(data, state, body):
    if not isinstance(body, dict):
        return {}
    kind = body.get("type") if isinstance(body.get("type"), str) else ""
    target = body.get("target").strip() if isinstance(body.get("target"), str) else ""
    found = _decision_subject(data, kind, target)
    for row in (state or {}).get("decisions") or []:
        if isinstance(row, dict) and row.get("status") == "queued" and row.get("type") == kind and row.get("target") == target:
            found["queued_choice"] = row.get("choice") or ""
            found["queued_id"] = row.get("id") or ""
            break
    return found


def record_action(state, op, body, result, captured, moment):
    """Append the learning event for a board action. Notes are not copied."""
    if not isinstance(result, dict):
        return None
    captured = captured or {}
    item_id = result.get("id") if isinstance(result.get("id"), str) else ""
    area = captured.get("area") or ""
    label = captured.get("label") or ""
    if op == "todo/toggle":
        data = _with_label({"done": bool(result.get("done"))}, label)
        return append(state, _event("task_tick", moment, item_id, area, data), moment)
    if op == "snooze":
        kind = "email" if isinstance(body, dict) and body.get("kind") == "email" else "todo"
        data = _with_label({"kind": kind, "until": result.get("snooze")}, label)
        return append(state, _event("snooze", moment, item_id, area, data), moment)
    if op == "todo/quadrant":
        data = _with_label({"from": captured.get("from") or "", "to": result.get("quadrant")}, label)
        if not data["from"]:
            data.pop("from")
        return append(state, _event("quadrant", moment, item_id, area, data), moment)
    if op == "email/handled":
        data = _with_label({"handled": bool(result.get("handled"))}, label)
        return append(state, _event("email_handled", moment, item_id, area, data), moment)
    if op == "question/toggle":
        data = {"done": bool(result.get("done")), "day": result.get("day") or ""}
        if captured.get("section"):
            data["section"] = captured["section"]
        _with_label(data, label)
        return append(state, _event("robbins", moment, item_id, "", data), moment)
    return None


def record_decision(state, body, result, captured, moment):
    if not isinstance(body, dict) or not isinstance(result, dict):
        return None
    captured = captured or {}
    kind = body.get("type") if body.get("type") in ("draft", "meeting", "checkin") else ""
    target = body.get("target").strip() if isinstance(body.get("target"), str) else ""
    undo = body.get("undo") in (True, "1", 1, "true")
    choice = captured.get("queued_choice") if undo else result.get("choice")
    data = {"decision_type": kind, "target": target}
    if choice:
        data["choice"] = choice
    decision_id = result.get("id") or captured.get("queued_id") or ""
    if decision_id:
        data["decision_id"] = decision_id
    if undo:
        data["undo"] = True
    _with_label(data, captured.get("label") or "")
    return append(
        state,
        _event("decision", moment, captured.get("item_id") or "", captured.get("area") or "", data),
        moment,
    )


def record_client(state, body, moment):
    """Filter chips and restaurant links. The client cannot record other types."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    kind = body.get("type")
    if kind == "filter":
        area = body.get("area")
        if area not in FILTERS:
            raise ValueError("area must be All, Echandia, FiFi4, or Personal")
        return append(state, _event("filter", moment, "", area, {"area": area}), moment)
    if kind == "restaurant":
        label = _label(body.get("label"))
        if not label:
            raise ValueError("label is required")
        place_kind = body.get("kind")
        if place_kind not in ("eat", "delivery"):
            raise ValueError("kind must be eat or delivery")
        area = _label(body.get("area"), 80)
        place = _label(body.get("place"), 80)
        data = {"label": label, "kind": place_kind}
        if place:
            data["place"] = place
        return append(state, _event("restaurant", moment, "", area, data), moment)
    raise ValueError("type must be filter or restaurant")


def _public(row):
    item = {"id": row.get("id"), "ts": row.get("ts"), "type": row.get("type"), "data": dict(row.get("data") or {})}
    if row.get("item_id"):
        item["item_id"] = row["item_id"]
    if row.get("area"):
        item["area"] = row["area"]
    return item


def _moment(row):
    try:
        return parse_time(row.get("ts"))
    except ValueError:
        return None


def select(state, since, limit, now, since_text=""):
    """Oldest first. next_since is the last returned ts, for the next poll."""
    changed = prune(state, now)
    rows = []
    for row in _rows(state):
        if isinstance(row, dict) and _moment(row) is not None:
            rows.append(row)
    rows.sort(key=lambda row: (_moment(row), row.get("id") or ""))
    if since is not None:
        rows = [row for row in rows if _moment(row) > since]
    if len(rows) > limit:
        page = rows[:limit]
        boundary = _moment(page[-1])
        if _moment(rows[limit]) == boundary:
            trimmed = [row for row in page if _moment(row) < boundary]
            page = trimmed or [row for row in rows if _moment(row) == boundary]
    else:
        page = rows
    if page:
        next_since = page[-1]["ts"]
    elif since is not None and since_text:
        next_since = since_text
    else:
        next_since = stamp(now)
    return [_public(row) for row in page], next_since, changed
