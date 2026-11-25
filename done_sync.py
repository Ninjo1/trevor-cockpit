"""Board done-state sync.

Notion is the source of truth for the Actions database, but this process
does not call Notion. The morning job reads Notion and posts the result.
Board ticks stay in state until that job confirms it wrote them.

Pending rows are board toggles not yet acked. An ingest cannot overwrite a
pending row. After the ack, `done_ids` and `undone_ids` apply.
"""
from actions import check_id

MAX_IDS = 2000
_AT_LIMIT = 40


def _pending(state):
    pending = state.get("done_sync")
    if not isinstance(pending, dict):
        pending = {}
        state["done_sync"] = pending
    return pending


def queue(state, item_id, done, now_iso):
    """Remember a board tick or untick until the refresh job acks it."""
    _pending(state)[item_id] = {"done": bool(done), "at": now_iso}


def list_pending(state):
    rows = []
    for item_id, row in _pending(state).items():
        if not isinstance(item_id, str) or not isinstance(row, dict):
            continue
        rows.append({"id": item_id, "done": bool(row.get("done")), "at": row.get("at") or ""})
    rows.sort(key=lambda row: (row["at"], row["id"]))
    return rows


def touches(obj):
    return isinstance(obj, dict) and any(key in obj and obj.get(key) is not None for key in ("done_ids", "undone_ids", "done_ack"))


def _id_list(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of item ids")
    if len(value) > MAX_IDS:
        raise ValueError(f"{field} is too long")
    return [check_id(item) for item in value]


def _ack_at(value, field):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _AT_LIMIT:
        raise ValueError(f"{field} at must be a short string")
    return value


def ack_entries(value, field):
    """Ids, or objects `{id, at}`. `at` acks only the toggle observed at that time."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    if len(value) > MAX_IDS:
        raise ValueError(f"{field} is too long")
    rows = []
    for item in value:
        if isinstance(item, str):
            rows.append((check_id(item), None))
            continue
        if not isinstance(item, dict):
            raise ValueError(f"{field} entries must be ids or objects")
        rows.append((check_id(item.get("id")), _ack_at(item.get("at"), field)))
    return rows


def validate_payload(obj):
    """Reject a bad done-sync shape before data.json is replaced."""
    if not isinstance(obj, dict):
        return obj
    if "done_ids" in obj:
        _id_list(obj.get("done_ids"), "done_ids")
    if "undone_ids" in obj:
        _id_list(obj.get("undone_ids"), "undone_ids")
    if "done_ack" in obj:
        ack_entries(obj.get("done_ack"), "done_ack")
    return obj


def acks_from_body(body):
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    if "ids" not in body and "acks" not in body:
        raise ValueError("ids or acks is required")
    rows = []
    if "ids" in body:
        rows.extend((item_id, None) for item_id in _id_list(body.get("ids"), "ids"))
    if "acks" in body:
        rows.extend(ack_entries(body.get("acks"), "acks"))
    return rows


def ack(state, entries):
    """Drop pending rows. An `at` that does not match leaves the newer toggle."""
    pending = _pending(state)
    acked = []
    for item_id, at in entries:
        row = pending.get(item_id)
        if not isinstance(row, dict):
            continue
        if at is not None and row.get("at") != at:
            continue
        pending.pop(item_id, None)
        acked.append(item_id)
    return acked


def _set_done(state, item_id, done, now_iso):
    todos = state.setdefault("todos", {})
    entry = todos.get(item_id)
    if not isinstance(entry, dict):
        entry = {}
        todos[item_id] = entry
    previous = entry.get("done")
    changed = previous is not done
    entry["done"] = done
    if done:
        if previous is not True:
            entry["done_at"] = now_iso
    else:
        entry["done_at"] = None
    return changed


def apply_ingest(state, obj, now_iso):
    """Merge Notion's done and undone ids. Pending board toggles win.

    Ids acked in this same payload are left as the board set them. The next
    ingest can change them. An id listed in both `done_ids` and `undone_ids`
    is undone, unless it is pending or acked here.
    """
    done_ids = _id_list(obj.get("done_ids"), "done_ids") if "done_ids" in obj else []
    undone_ids = _id_list(obj.get("undone_ids"), "undone_ids") if "undone_ids" in obj else []
    acked_ids = ack(state, ack_entries(obj.get("done_ack"), "done_ack") if "done_ack" in obj else [])
    protected = set(acked_ids)
    protected.update(item_id for item_id in _pending(state))
    changed = bool(acked_ids)
    for item_id in done_ids:
        if item_id in protected:
            continue
        if _set_done(state, item_id, True, now_iso):
            changed = True
    for item_id in undone_ids:
        if item_id in protected:
            continue
        if _set_done(state, item_id, False, now_iso):
            changed = True
    return {
        "done_ids": len(done_ids),
        "undone_ids": len(undone_ids),
        "done_ack": len(acked_ids),
        "changed": changed,
    }
