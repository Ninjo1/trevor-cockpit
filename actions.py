"""State changes for ticks, custom tasks, edits, snoozes, and handled mail.

These writes touch state only. A later data ingest cannot clear them.
"""
import re
import uuid

AREAS = ("Echandia", "FiFi4", "Personal")
LEGACY = {"Personal / Own cos": "Personal", "Household": "Personal"}
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}\Z")
DUE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def area_of(value):
    if not value:
        return "Personal"
    return LEGACY.get(value, value)


def check_id(value):
    if not isinstance(value, str) or not ITEM_ID.fullmatch(value):
        raise ValueError("id is not valid")
    return value


def norm_due(value):
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not DUE.fullmatch(value):
        raise ValueError("due must be YYYY-MM-DD")
    return value


def _text(value, field, limit):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{field} is too long")
    return text


def _custom(state, item_id):
    return next(
        (row for row in state["custom"] if isinstance(row, dict) and row.get("id") == item_id),
        None,
    )


def _entry(mapping, item_id):
    current = mapping.get(item_id)
    if not isinstance(current, dict):
        current = {}
        mapping[item_id] = current
    return current


def apply(state, op, body, now_iso):
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    todos = state["todos"]
    if op == "todo/toggle":
        item_id = check_id(body.get("id"))
        if "done" not in body:
            raise ValueError("done is required")
        entry = _entry(todos, item_id)
        done = bool(body["done"])
        entry["done"] = done
        entry["done_at"] = now_iso if done else None
        return {"id": item_id, "done": done}
    if op == "todo/add":
        title = _text(body.get("title"), "title", 300)
        if not title:
            raise ValueError("title required")
        area = body.get("area") if body.get("area") in AREAS else "Personal"
        due = norm_due(body.get("due"))
        notes = _text(body.get("notes"), "notes", 2000)
        item_id = body.get("id") or ("U-" + uuid.uuid4().hex[:10])
        check_id(item_id)
        if _custom(state, item_id):
            raise ValueError("id already exists")
        item = {
            "id": item_id,
            "title": title,
            "area": area,
            "due": due,
            "notes": notes,
            "created_at": now_iso,
        }
        state["custom"].append(item)
        return item
    if op == "todo/edit":
        item_id = check_id(body.get("id"))
        custom = _custom(state, item_id)
        entry = custom if custom is not None else _entry(todos, item_id)
        if "title" in body and body["title"] is not None:
            title = _text(body.get("title"), "title", 300)
            if not title:
                raise ValueError("title required")
            entry["title"] = title
        if "notes" in body and body["notes"] is not None:
            notes = _text(body.get("notes"), "notes", 2000)
            if custom is not None:
                entry["notes"] = notes
            else:
                entry["notes"] = notes
        if "due" in body:
            due = norm_due(body.get("due"))
            entry["due"] = due
        if body.get("area"):
            if body["area"] not in AREAS:
                raise ValueError("area must be Echandia, FiFi4, or Personal")
            entry["area"] = body["area"]
        return {"id": item_id}
    if op == "email/handled":
        item_id = check_id(body.get("id"))
        if "handled" not in body:
            raise ValueError("handled is required")
        entry = _entry(state["emails"], item_id)
        handled = bool(body["handled"])
        entry["handled"] = handled
        entry["handled_at"] = now_iso if handled else None
        return {"id": item_id, "handled": handled}
    if op == "todo/quadrant":
        item_id = check_id(body.get("id"))
        quadrant = body.get("quadrant")
        if quadrant not in ("do_now", "schedule", "delegate", "delete"):
            raise ValueError("quadrant must be do_now, schedule, delegate, or delete")
        custom = _custom(state, item_id)
        entry = custom if custom is not None else _entry(todos, item_id)
        entry["quadrant"] = quadrant
        return {"id": item_id, "quadrant": quadrant}
    if op == "snooze":
        item_id = check_id(body.get("id"))
        kind = body.get("kind")
        if body.get("clear") in (True, "1", 1, "true"):
            until = None
        else:
            until = norm_due(body.get("until")) or None
        if kind == "email":
            entry = _entry(state["emails"], item_id)
        else:
            custom = _custom(state, item_id)
            entry = custom if custom is not None else _entry(todos, item_id)
        entry["snooze"] = until
        return {"id": item_id, "snooze": until}
    if op == "question/toggle":
        item_id = check_id(body.get("id"))
        day = body.get("day")
        if not isinstance(day, str) or not DUE.fullmatch(day):
            raise ValueError("day must be YYYY-MM-DD")
        if "done" not in body:
            raise ValueError("done is required")
        questions = state.get("questions")
        if not isinstance(questions, dict):
            questions = {}
            state["questions"] = questions
        bucket = questions.get(day)
        if not isinstance(bucket, dict):
            bucket = {}
            questions[day] = bucket
        entry = _entry(bucket, item_id)
        done = bool(body["done"])
        entry["done"] = done
        entry["done_at"] = now_iso if done else None
        return {"id": item_id, "done": done, "day": day}
    raise KeyError(op)


def brief_log(op, body, now_iso):
    """A short audit line. Notes are not copied into the log."""
    brief = {}
    if isinstance(body, dict):
        for key in ("id", "done", "handled", "area", "due", "until", "kind", "quadrant", "day"):
            if key in body:
                brief[key] = body[key]
        if op == "todo/add" and isinstance(body.get("title"), str):
            brief["title"] = body["title"][:180]
    return {"t": now_iso, "op": op, "body": brief}
