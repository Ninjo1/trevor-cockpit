"""Local voice answers and the Outlook draft queue.

The board has no Graph credentials. A spoken reply is stored here until a
separate pickup job, using VOICE_PICKUP_TOKEN, creates the Outlook draft.

answer() is the only place a later LLM backend needs to replace. Pass
backend= to skip the local intents.
"""
import datetime as dt
import re
import uuid

from actions import check_id

_REQUEST_ID = re.compile(r"V-[A-Za-z0-9]{8,24}\Z")


def _items(data):
    rows = []
    for item in (data or {}).get("items") or []:
        if isinstance(item, dict) and item.get("id"):
            rows.append(item)
    return rows


def find_item(data, item_id):
    for item in _items(data):
        if item.get("id") == item_id:
            return item
    return None


def queue_draft(state, data, item_id, text, now_iso):
    item_id = check_id(item_id)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text is required")
    if len(text.strip()) > 8000:
        raise ValueError("text is too long")
    item = find_item(data, item_id)
    if item is None:
        raise KeyError(item_id)
    drafts = state.get("voice_drafts")
    if not isinstance(drafts, list):
        drafts = []
        state["voice_drafts"] = drafts
    record = {
        "id": "V-" + uuid.uuid4().hex[:12],
        "item_id": item_id,
        "message_id": str(item.get("message_id") or ""),
        "conversation_id": str(item.get("conversation_id") or ""),
        "subject": str(item.get("subject") or item.get("title") or ""),
        "text": text.strip(),
        "status": "pending",
        "created_at": now_iso,
    }
    drafts.append(record)
    return record


def list_drafts(state, status="pending"):
    rows = []
    for row in (state or {}).get("voice_drafts") or []:
        if isinstance(row, dict) and row.get("status") == status:
            rows.append(dict(row))
    return rows


def finish_draft(state, request_id, weblink, now_iso):
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise ValueError("id is not valid")
    if weblink not in (None, ""):
        if not isinstance(weblink, str) or not (weblink.startswith("https://") or weblink.startswith("http://")):
            raise ValueError("draft_weblink must be an http or https url")
        if any(char in weblink for char in " \n\r\t\"<>"):
            raise ValueError("draft_weblink must be an http or https url")
    for row in (state or {}).get("voice_drafts") or []:
        if isinstance(row, dict) and row.get("id") == request_id:
            row["status"] = "done"
            row["done_at"] = now_iso
            if weblink:
                row["draft_weblink"] = weblink
            return dict(row)
    raise KeyError(request_id)


def board_facts(data, state, today, now, zone):
    """Facts the local answerer is allowed to use. No network calls."""
    import render

    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    rows = render.merged(data or {}, state or {})
    due = []
    do_now = []
    for item in rows:
        if item.get("done") or item.get("kind") in ("email", "draft", "money"):
            continue
        if item.get("section") == "waiting":
            continue
        if item.get("due") == today.isoformat():
            due.append(item.get("title") or "Untitled")
        if render.derive_quadrant(item, today) == "do_now":
            do_now.append(item.get("title") or "Untitled")
    waiting = []
    source = (data or {}).get("waiting_on")
    if isinstance(source, list):
        for row in source:
            if isinstance(row, dict):
                waiting.append(f"{row.get('who') or 'Someone'}: {row.get('what') or 'Waiting'}")
    else:
        for item in (data or {}).get("items") or []:
            if isinstance(item, dict) and item.get("section") == "waiting":
                waiting.append(item.get("title") or "Waiting")
    trips = []
    for row in (data or {}).get("travel") or []:
        if not isinstance(row, dict):
            continue
        end = render.utc(row.get("end"))
        if end and end <= now:
            continue
        start = render.utc(row.get("start"))
        trips.append((start or end or now, row.get("title") or "Trip", row.get("kind") or "trip"))
    trips.sort(key=lambda item: item[0])
    meetings = []
    for event in (data or {}).get("calendar") or []:
        if not isinstance(event, dict):
            continue
        if event.get("allDay"):
            start_day = str(event.get("start") or "")
            end_day = str(event.get("end") or "")
            if start_day <= today.isoformat() < end_day:
                meetings.append(event.get("subject") or "Meeting")
            continue
        start = render.utc(event.get("start"))
        end = render.utc(event.get("end"))
        if not start or (end and end <= now):
            continue
        if start.astimezone(zone).date() == today or now <= start <= now + dt.timedelta(hours=24):
            meetings.append(event.get("subject") or "Meeting")
    upgrades = []
    for row in (data or {}).get("upgrades") or []:
        if not isinstance(row, dict):
            continue
        bits = [str(row.get("flight") or "Flight"), str(row.get("status") or "unknown status")]
        if row.get("position") not in (None, ""):
            bits.append(f"position {row.get('position')}")
        upgrades.append(", ".join(bits))
    return {
        "due_today": due,
        "do_now": do_now,
        "next_trip": trips[0][1] if trips else "",
        "next_trip_kind": trips[0][2] if trips else "",
        "meetings": meetings,
        "waiting": waiting,
        "upgrades": upgrades,
    }


def _join(names, empty):
    clean = [str(name) for name in names if name]
    if not clean:
        return empty
    if len(clean) == 1:
        return clean[0]
    return ", ".join(clean[:-1]) + ", and " + clean[-1]


def answer(question, facts, backend=None):
    """Answer from board facts. backend(question, facts) can replace this later."""
    if backend is not None:
        return backend(question, facts)
    text = " ".join(str(question or "").lower().split())
    facts = facts or {}
    if "upgrade" in text:
        return _join(facts.get("upgrades") or [], "No upgrade status.")
    if "waiting" in text:
        return _join(facts.get("waiting") or [], "Nothing you are waiting on.")
    if "meeting" in text or "calendar" in text or "on today" in text:
        return _join(facts.get("meetings") or [], "Nothing on the calendar today.")
    if "flight" in text or "trip" in text or "next up" in text:
        title = facts.get("next_trip") or ""
        if not title:
            return "No upcoming trip."
        kind = facts.get("next_trip_kind") or "trip"
        return f"Next {kind} is {title}."
    if "do now" in text:
        return _join(facts.get("do_now") or [], "Nothing is in Do now.")
    if "due" in text:
        return _join(facts.get("due_today") or [], "Nothing is due today.")
    return "I can't answer that from the board yet."
