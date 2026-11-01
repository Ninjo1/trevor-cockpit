"""Server-rendered cockpit page. The same HTML works with JavaScript off.

Clocks are where you are, Vancouver, and Amsterdam. The browser marks the
device zone Here. If that zone is unknown, the page uses today's payload
location when it includes a time zone, then HOME_TZ. Vancouver is Home.
Calendar times, due dates, and the refreshed line use DISPLAY_TZ, then
HOME_TZ, then America/Vancouver.
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from actions import AREAS, area_of
from textclean import clean_text

FALLBACK_ZONE = "America/Vancouver"
AMSTERDAM_ZONE = "Europe/Amsterdam"
CAL_WINDOW = 8  # today and the next 7 days
FLIGHT = re.compile(
    r"\b(?:DL|UA|AC|WS|AA|AS|NH|KE|TG|JL|OZ|SQ|CX|BA|LH|AF|KL)\s?\d{2,4}\b|\bflight\b",
    re.I,
)
DIRECTIONS = {"owed_to_me": "Owed to you", "i_owe": "You owe"}

e = lambda value: html.escape(clean_text(value), quote=True)
e_raw = lambda value: html.escape("" if value is None else str(value), quote=True)


def zone_city(iana):
    """Last segment of an IANA name, with underscores as spaces."""
    return str(iana).rstrip("/").split("/")[-1].replace("_", " ")


def parse_zone(name):
    text = (name or "").strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _env(env):
    return os.environ if env is None else env


def resolve_display(env=None):
    """Return (zone, label). DISPLAY_TZ, else HOME_TZ, else Vancouver."""
    env = _env(env)
    chosen = parse_zone(env.get("DISPLAY_TZ")) or parse_zone(env.get("HOME_TZ")) or ZoneInfo(FALLBACK_ZONE)
    key = getattr(chosen, "key", None) or FALLBACK_ZONE
    return chosen, zone_city(key)


def home_clock(env=None):
    """Return (label, iana key) for the current-location clock.

    HOME_TZ and HOME_LABEL are Trevor's current location, the Here clock,
    not his home. Vancouver is always the Home clock. Do not rename these
    env vars.
    """
    env = _env(env)
    chosen = parse_zone(env.get("HOME_TZ")) or ZoneInfo(FALLBACK_ZONE)
    key = getattr(chosen, "key", None) or FALLBACK_ZONE
    label = (env.get("HOME_LABEL") or "").strip() or zone_city(key)
    return label, key


def _place_zone(loc):
    """Return (city label, IANA key) when a payload place names a real zone."""
    if isinstance(loc, str):
        zone = parse_zone(loc)
        if not zone:
            return None
        key = getattr(zone, "key", None) or loc.strip()
        return zone_city(key), key
    if not isinstance(loc, dict):
        return None
    zone = parse_zone(loc.get("tz") or loc.get("zone") or loc.get("timezone"))
    if not zone:
        return None
    key = getattr(zone, "key", None) or ""
    label = (loc.get("label") or loc.get("name") or loc.get("city") or "").strip() or zone_city(key)
    return label, key


def payload_here(data, today=None):
    """City for today from the payload, when that place includes a time zone.

    A restaurant location whose dates contain today wins. Otherwise a
    top-level location or here object is used. A name alone is not enough.
    """
    if not isinstance(data, dict):
        return None
    day = today or dt.datetime.now(dt.timezone.utc).date()
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    locs = data.get("restaurant_locations") if isinstance(data.get("restaurant_locations"), list) else []
    matches = []
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        start = str(loc.get("from") or "0001-01-01")
        until = str(loc.get("until") or "9999-12-31")
        if start <= iso <= until:
            parsed = _place_zone(loc)
            if parsed:
                matches.append((start, parsed))
    if matches:
        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[0][1]
    return _place_zone(data.get("location")) or _place_zone(data.get("here"))


def here_fallback(env=None, data=None, today=None):
    """Where you are when the device zone is unknown.

    Payload location, then HOME_TZ. HOME_TZ is the current location, not
    the Vancouver Home clock.
    """
    found = payload_here(data, today)
    if found:
        return found
    env = _env(env)
    if parse_zone(env.get("HOME_TZ")):
        return home_clock(env)
    return "Vancouver", FALLBACK_ZONE


def ritual_zone(env=None, data=None, moment=None):
    """Zone whose midnight resets the Robbins questions.

    This is the current-location zone the Here clock is built from: today's
    payload location when it includes a time zone, otherwise HOME_TZ.
    HOME_TZ is the current location, not Vancouver. The browser zone is not
    used, and the pinned Vancouver Home clock is not used. If neither the
    payload nor HOME_TZ names a zone, the fallback is Asia/Bangkok.
    """
    env = _env(env)
    moment = moment or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    home = parse_zone(env.get("HOME_TZ"))
    provisional = home or ZoneInfo("Asia/Bangkok")
    found = payload_here(data, moment.astimezone(provisional).date())
    if found:
        zone = parse_zone(found[1])
        if zone:
            return zone
    if home:
        return home
    return ZoneInfo("Asia/Bangkok")


def ritual_day(env=None, data=None, moment=None):
    """Return (YYYY-MM-DD, IANA key) for the Robbins day."""
    moment = moment or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    zone = ritual_zone(env, data, moment)
    key = getattr(zone, "key", None) or "Asia/Bangkok"
    return moment.astimezone(zone).date().isoformat(), key


def question_id(section, text):
    """Stable id from the section and the question text, not the list index."""
    raw = clean_text(text)
    digest = hashlib.sha256(f"{section}\n{raw}".encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:32]
    ident = f"{section}-{slug}-{digest}" if slug else f"{section}-{digest}"
    return ident[:81]


def clock_rows(env=None, data=None, today=None):
    """Here, then Vancouver (Home), then Amsterdam.

    Each row is (city, iana, mark, current, role). If Here is Vancouver or
    Amsterdam, that clock is not repeated. It is marked Here instead.
    """
    label, key = here_fallback(env, data, today)
    if key == FALLBACK_ZONE:
        return [
            ("Vancouver", FALLBACK_ZONE, "Here", True, "here home"),
            ("Amsterdam", AMSTERDAM_ZONE, "", False, "amsterdam"),
        ]
    if key == AMSTERDAM_ZONE:
        return [
            ("Amsterdam", AMSTERDAM_ZONE, "Here", True, "here amsterdam"),
            ("Vancouver", FALLBACK_ZONE, "Home", False, "home"),
        ]
    return [
        (label, key, "Here", True, "here"),
        ("Vancouver", FALLBACK_ZONE, "Home", False, "home"),
        ("Amsterdam", AMSTERDAM_ZONE, "", False, "amsterdam"),
    ]


def utc(value):
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def day_label(day):
    return day.strftime("%a %b ") + str(day.day)


def clock_time(moment):
    hour = moment.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{moment.strftime('%M %p')}"


def safe_href(url):
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not (text.startswith("https://") or text.startswith("http://") or text.startswith("mailto:")):
        return None
    if any(char in text for char in " \n\r\t\"<>"):
        return None
    return text


def dom_id(prefix, raw):
    slug = re.sub(r"[^A-Za-z0-9_-]", "", str(raw))[:24]
    digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{slug}-{digest}" if slug else f"{prefix}-{digest}"


def fx_view(data):
    fx = data.get("fx")
    as_of = ""
    source = "data file"
    raw = None
    if isinstance(fx, dict):
        raw = fx.get("usdcad", fx.get("rate"))
        as_of = str(fx.get("as_of") or fx.get("date") or "")
        source = str(fx.get("source") or "data file")
    elif isinstance(fx, (int, float)) and not isinstance(fx, bool):
        raw = fx
    elif isinstance(data.get("fx_usdcad"), (int, float)) and not isinstance(data.get("fx_usdcad"), bool):
        raw = data.get("fx_usdcad")
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        return None
    if rate <= 0:
        return None
    return {"rate": rate, "as_of": as_of, "source": source}


def merged(data, state):
    todos = state.get("todos") or {}
    emails = state.get("emails") or {}
    out = []
    for item in data.get("items") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        overlay = todos.get(item["id"]) or {}
        mail = emails.get(item["id"]) or {}
        kind = item.get("kind") or "task"
        if kind in ("email", "draft") and "handled" in mail:
            done = bool(mail.get("handled"))
            done_at = mail.get("handled_at")
        elif "done" in overlay:
            done = bool(overlay.get("done"))
            done_at = overlay.get("done_at")
        else:
            done = bool(item.get("done"))
            done_at = None
        if "due" in overlay and overlay.get("due") is not None:
            due = overlay.get("due") or ""
        else:
            due = item.get("due") or ""
        if "notes" in overlay and overlay.get("notes") is not None:
            note = overlay.get("notes") or ""
        else:
            note = item.get("note") or ""
        snooze = mail.get("snooze") if kind in ("email", "draft") else overlay.get("snooze")
        pending, done_links = _voice_flags(state)
        out.append(
            {
                "id": item["id"],
                "kind": kind,
                "section": item.get("section") or "open",
                "priority": item.get("priority"),
                "links": item.get("links") or [],
                "ledger_ids": item.get("ledger_ids") or [],
                "refs": item.get("refs") or [],
                "done_note": item.get("done_note") or "",
                "area": area_of(overlay.get("area") or item.get("area")),
                "title": overlay.get("title") or item.get("title") or "",
                "done": done,
                "done_at": done_at if done else None,
                "due": due or "",
                "snooze": snooze or None,
                "note": note,
                "custom": False,
                "quadrant": overlay.get("quadrant") or item.get("quadrant"),
                "quadrant_manual": overlay.get("quadrant") in ("do_now", "schedule", "delegate", "delete"),
                "webLink": item.get("webLink") or "",
                "attachments": item.get("attachments") if isinstance(item.get("attachments"), list) else [],
                "voice_pending": item["id"] in pending,
                "voice_weblink": done_links.get(item["id"]) or "",
                "important": item.get("important") if item.get("important") is not None else overlay.get("important"),
                "owner": overlay.get("owner") or item.get("owner") or "",
            }
        )
    for custom in state.get("custom") or []:
        if not isinstance(custom, dict) or not custom.get("id"):
            continue
        overlay = todos.get(custom["id"]) or {}
        pending, done_links = _voice_flags(state)
        out.append(
            {
                "id": custom["id"],
                "kind": "task",
                "section": custom.get("section") or "open",
                "priority": custom.get("priority"),
                "links": [],
                "ledger_ids": [],
                "refs": [],
                "done_note": "",
                "area": area_of(custom.get("area")),
                "title": custom.get("title") or "",
                "done": bool(overlay.get("done")),
                "done_at": overlay.get("done_at"),
                "due": custom.get("due") or "",
                "snooze": custom.get("snooze") or overlay.get("snooze"),
                "note": custom.get("notes") or "",
                "custom": True,
                "quadrant": overlay.get("quadrant") or custom.get("quadrant"),
                "quadrant_manual": overlay.get("quadrant") in ("do_now", "schedule", "delegate", "delete"),
                "webLink": "",
                "attachments": [],
                "voice_pending": custom["id"] in pending,
                "voice_weblink": done_links.get(custom["id"]) or "",
                "important": custom.get("important"),
                "owner": custom.get("owner") or "",
            }
        )
    return out


def due_badge(due, today):
    if not due:
        return ""
    try:
        day = dt.date.fromisoformat(due)
    except ValueError:
        return ""
    delta = (today - day).days
    if delta > 0:
        return f'<span class="b over">{delta}d overdue</span>'
    if delta == 0:
        return '<span class="b today">Today</span>'
    if delta == -1:
        return '<span class="b soon">Tomorrow</span>'
    return f'<span class="b">Due {e(day_label(day))}</span>'


def _http_href(url):
    href = safe_href(url)
    if href and href.startswith("http"):
        return href
    return None


def _short_label(text, limit=28):
    raw = clean_text(text) or "File"
    if len(raw) <= limit:
        return raw
    return raw[: limit - 3].rstrip() + "..."


def _anchor(href, label, css=""):
    klass = f' class="{css}"' if css else ""
    return (
        f'<a{klass} href="{e_raw(href)}" target="_blank" rel="noopener noreferrer">{e(label)}</a>'
    )


def _description_links(links):
    """ClickUp or Notion links that point at the full description."""
    found = []
    for link in links:
        if not isinstance(link, dict) or link.get("kind") == "draft":
            continue
        href = _http_href(link.get("href") or link.get("url"))
        if not href:
            continue
        lowered = href.lower()
        kind = link.get("kind")
        if kind in ("clickup", "notion") or "clickup.com" in lowered or "notion.so" in lowered or "notion.site" in lowered:
            found.append((href, link.get("label") or ("Notion" if "notion." in lowered else "ClickUp")))
    return found


def _open_email_href(item, links):
    direct = _http_href(item.get("webLink"))
    if direct:
        return direct
    for link in links:
        if not isinstance(link, dict):
            continue
        if link.get("kind") not in ("outlook", "mail", "email"):
            continue
        href = _http_href(link.get("href") or link.get("url"))
        if href:
            return href
    if item.get("kind") in ("email", "draft"):
        for link in links:
            if not isinstance(link, dict) or link.get("kind") == "draft":
                continue
            href = _http_href(link.get("href") or link.get("url"))
            lowered = (href or "").lower()
            if href and ("outlook." in lowered or "/mail" in lowered):
                return href
    return None


def _voice_flags(state):
    pending = set()
    done_links = {}
    for row in (state or {}).get("voice_drafts") or []:
        if not isinstance(row, dict) or not row.get("item_id"):
            continue
        if row.get("status") == "pending":
            pending.add(row["item_id"])
        elif row.get("status") == "done":
            href = _http_href(row.get("draft_weblink"))
            if href:
                done_links.setdefault(row["item_id"], href)
    return pending, done_links


def row_links_html(item):
    """One tight line: Draft ready, Open email, other links, then file names."""
    links = [link for link in (item.get("links") or []) if isinstance(link, dict)]
    parts = []
    draft_linked = False
    for link in links:
        if link.get("kind") != "draft":
            continue
        href = _http_href(link.get("url") or link.get("href"))
        if href:
            parts.append(_anchor(href, "Draft ready", "draftready"))
            draft_linked = True
            break
    if not draft_linked and item.get("voice_pending"):
        parts.append('<span class="draftwait">Draft requested</span>')
    elif not draft_linked and item.get("voice_weblink"):
        href = _http_href(item.get("voice_weblink"))
        if href:
            parts.append(_anchor(href, "Draft ready", "draftready"))
    email_href = _open_email_href(item, links)
    if email_href:
        parts.append(_anchor(email_href, "Open email"))
    used = {email_href} if email_href else set()
    for href, label in _description_links(links):
        if href in used:
            continue
        used.add(href)
        parts.append(_anchor(href, label or "Description"))
    for link in links:
        if link.get("kind") in ("draft", "outlook", "mail", "email", "clickup", "notion"):
            continue
        href = _http_href(link.get("href") or link.get("url"))
        if not href or href in used:
            continue
        lowered = href.lower()
        if "clickup.com" in lowered or "notion.so" in lowered or "notion.site" in lowered or "outlook." in lowered:
            continue
        used.add(href)
        parts.append(_anchor(href, link.get("label") or "Open"))
    files = []
    for row in item.get("attachments") or []:
        if not isinstance(row, dict):
            continue
        href = _http_href(row.get("url"))
        if not href:
            continue
        files.append(_anchor(href, _short_label(row.get("name") or "File"), "file"))
    parts.extend(files[:3])
    extra = files[3:]
    if extra:
        parts.append(
            f'<details class="morefiles"><summary>+{len(extra)} more</summary>'
            + '<span class="dot"> · </span>'.join(extra)
            + "</details>"
        )
    if not parts:
        return ""
    return '<div class="rowlinks">' + '<span class="dot"> · </span>'.join(parts) + "</div>"


def note_html(note):
    text = e(note)
    if not text:
        return ""
    return (
        f'<details class="note"><summary><span class="noteline">{text}</span></summary></details>'
    )


def link_html(link):
    if not isinstance(link, dict):
        return ""
    href = safe_href(link.get("href"))
    if not href:
        return ""
    kind = link.get("kind")
    if kind not in ("clickup", "outlook", "teams", "calendar", "mail", "web"):
        lowered = href.lower()
        if "clickup.com" in lowered:
            kind = "clickup"
        elif lowered.startswith("mailto:"):
            kind = "mail"
        elif "teams.microsoft" in lowered or "meetup-join" in lowered:
            kind = "teams"
        elif "outlook.office" in lowered or "outlook.live" in lowered:
            kind = "outlook"
        else:
            kind = "web"
    external = "" if href.startswith("mailto:") else ' target="_blank" rel="noopener noreferrer"'
    label = link.get("label") or "Open"
    return f'<a class="lk {e_raw(kind)}" href="{e_raw(href)}"{external}>{e(label)}</a>'


def _hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{e_raw(value)}">'


def item_html(item, today, area_query):
    email = item.get("kind") in ("email", "draft")
    op = "email/handled" if email else "todo/toggle"
    field = "handled" if email else "done"
    nxt = "0" if item["done"] else "1"
    if email:
        verb = "Mark not handled" if item["done"] else "Mark handled"
        label = "Undo" if item["done"] else "Handled"
    else:
        verb = "Undo done" if item["done"] else "Mark done"
        label = "Undo" if item["done"] else "Done"
    meta = f'<span class="tag t-{e_raw(str(item["area"]).lower().split()[0])}">{e(item["area"])}</span>'
    if not item["done"]:
        meta += due_badge(item.get("due"), today)
    if item.get("priority") == "low" and not item["done"]:
        meta += '<span class="b low">Low</span>'
    if item.get("snooze"):
        meta += f'<span class="b soon">Back {e(item["snooze"])}</span>'
    refs = []
    for ref in list(item.get("ledger_ids") or []) + list(item.get("refs") or []):
        if ref and ref not in refs:
            refs.append(ref)
    if refs:
        meta += f'<span class="refs">{e(" ".join(str(ref) for ref in refs))}</span>'
    if item.get("custom"):
        meta += '<span class="refs">Added by you</span>'
    if not email:
        meta += f'<span class="why">{e(quadrant_reason(item, today))}</span>'
    note = item.get("done_note") if item["done"] and item.get("done_note") else item.get("note")
    links = row_links_html(item)
    area_value = "" if area_query == "All" else area_query
    kind = "email" if email else "todo"
    edit = (
        f'<details class="fold"><summary>Edit</summary>'
        f'<form method="post" action="form/todo/edit" autocomplete="off">{_hidden("id", item["id"])}{_hidden("area_q", area_value)}'
        f'<label>Title<input name="title" required value="{e(item["title"])}"></label>'
        f'<label>Area<select name="area">{"".join(f"<option{' selected' if item['area']==a else ''}>{e(a)}</option>" for a in AREAS)}</select></label>'
        f'<label>Due<input type="date" name="due" value="{e_raw(item.get("due") or "")}"></label>'
        f'<label>Notes<textarea name="notes" rows="2">{e(item.get("note") or "")}</textarea></label>'
        f'<button class="btn" type="submit">Save edit</button></form></details>'
    )
    snooze = (
        f'<details class="fold"><summary>Snooze</summary>'
        f'<form method="post" action="form/snooze">{_hidden("id", item["id"])}{_hidden("kind", kind)}{_hidden("area_q", area_value)}'
        f'<label>Back on<input type="date" name="until" value="{e_raw(item.get("snooze") or "")}"></label>'
        f'<button class="btn" type="submit">Snooze</button>'
        f'<button class="btn ghost" type="submit" name="clear" value="1">Clear snooze</button></form></details>'
    )
    current_q = item.get("quadrant_resolved") or "do_now"
    options = "".join(
        f'<option value="{key}"{" selected" if key == current_q else ""}>{e(label)}</option>'
        for key, label in (
            ("do_now", "Do now"),
            ("schedule", "Schedule"),
            ("delegate", "Delegate"),
            ("delete", "Delete or drop"),
        )
    )
    move = (
        f'<form class="move" method="post" action="form/todo/quadrant">{_hidden("id", item["id"])}{_hidden("area_q", area_value)}'
        f'<select name="quadrant" aria-label="Move to quadrant">{options}</select>'
        f'<button class="btn ghost" type="submit">Move</button></form>'
    )
    return (
        f'<li class="it{" done" if item["done"] else ""}" id="{e_raw(dom_id("i", item["id"]))}" data-id="{e_raw(item["id"])}" data-area="{e_raw(item["area"])}">'
        f'<form class="act" method="post" action="form/{op}">{_hidden("id", item["id"])}'
        f'{_hidden(field, nxt)}{_hidden("area", area_value)}'
        f'<button class="ck{" em" if email else ""}" type="submit" aria-label="{e(verb)}">{e(label)}</button></form>'
        f'<div class="bd"><div class="ttl">{e(item["title"])}</div><div class="meta">{meta}</div>'
        + (note_html(note) if note else "")
        + links
        + (f'<div class="nt">Owner {e(item.get("owner"))}</div>' if item.get("owner") else "")
        + f'<div class="rowtools">{edit}{snooze}{move}</div></div></li>'
    )


def list_html(items, today, area_query, group):
    if not items:
        return '<p class="empty">Nothing here.</p>'
    if not group:
        return "<ul>" + "".join(item_html(item, today, area_query) for item in items) + "</ul>"
    html_out = ""
    known = [area for area in AREAS if any(item["area"] == area for item in items)]
    extra = []
    for item in items:
        if item["area"] not in AREAS and item["area"] not in extra:
            extra.append(item["area"])
    for area in known + extra:
        rows = [item for item in items if item["area"] == area]
        if not rows:
            continue
        slug = re.sub(r"[^a-z0-9]+", "", area.lower()) or "other"
        html_out += f'<h3 class="ah t-{e_raw(slug)}">{e(area)}</h3><ul>'
        html_out += "".join(item_html(item, today, area_query) for item in rows)
        html_out += "</ul>"
    return html_out


def flight_like(event):
    subject = event.get("subject") or ""
    note = event.get("note") or ""
    if event.get("clash"):
        return True
    if re.search(r"clash", str(note), re.I):
        return True
    return bool(FLIGHT.search(str(subject)))


def classify_clashes(events):
    """Return (flight_clash_ids, other_overlap_ids) for timed events that overlap."""
    flight, overlap = set(), set()
    timed = []
    for event in events:
        if not isinstance(event, dict) or event.get("allDay"):
            continue
        start = utc(event.get("start"))
        end = utc(event.get("end"))
        if start and end and start < end:
            timed.append((event, start, end))
    for index, (left, start_left, end_left) in enumerate(timed):
        for right, start_right, end_right in timed[index + 1 :]:
            if start_left < end_right and start_right < end_left:
                if flight_like(left) or flight_like(right):
                    flight.add(left.get("id"))
                    flight.add(right.get("id"))
                else:
                    overlap.add(left.get("id"))
                    overlap.add(right.get("id"))
    for event in events:
        if isinstance(event, dict) and flight_like(event) and (
            event.get("clash") or re.search(r"clash", str(event.get("note") or ""), re.I)
        ):
            flight.add(event.get("id"))
    overlap -= flight
    return flight, overlap


def drop_clones(events):
    """Drop a '(Clone)' event when a real event shares its start."""
    def clone(event):
        return (event.get("subject") or "").rstrip().endswith("(Clone)")
    real = {event.get("start") for event in events if isinstance(event, dict) and not clone(event)}
    kept = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if clone(event) and event.get("start") in real:
            continue
        kept.append(event)
    return kept


def tidy_note(note, zone_label):
    text = clean_text(note)
    if not text:
        return ""
    text = re.sub(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)?\s*(?:Bangkok|ICT)\b", "", text, flags=re.I)
    if zone_label:
        text = re.sub(rf"\b\d{{1,2}}:\d{{2}}\s*(?:AM|PM)?\s*{re.escape(str(zone_label))}\b", "", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text).strip(" ,")


def calendar_html(data, today, zone=None, zone_label=""):
    zone = zone or resolve_display()[0]
    events = drop_clones([event for event in (data.get("calendar") or []) if isinstance(event, dict)])
    flight, overlap = classify_clashes(events)
    now = dt.datetime.now(dt.timezone.utc)
    timed = [event for event in events if not event.get("allDay") and utc(event.get("start"))]
    upcoming = [
        event
        for event in sorted(timed, key=lambda event: event.get("start") or "")
        if utc(event.get("start")) and utc(event.get("start")) > now and not event.get("stale")
    ]
    nxt = upcoming[0] if upcoming else None
    blocks = []
    empty_days = []
    for offset in range(CAL_WINDOW):
        day = today + dt.timedelta(days=offset)
        iso = day.isoformat()
        if offset == 0:
            label = f"Today, {day_label(day)}"
        elif offset == 1:
            label = f"Tomorrow, {day_label(day)}"
        else:
            label = day_label(day)
        all_day = [
            event
            for event in events
            if event.get("allDay") and str(event.get("start") or "") <= iso < str(event.get("end") or "")
        ]
        same_day = []
        for event in timed:
            start = utc(event.get("start"))
            if start and start.astimezone(zone).date() == day:
                same_day.append(event)
        same_day.sort(key=lambda event: event.get("start") or "")
        if not all_day and not same_day:
            if offset == 0:
                blocks.append(f'<article class="day"><h3>{e(label)}</h3><p class="empty">Nothing scheduled.</p></article>')
            else:
                empty_days.append(day_label(day))
            continue
        rows = '<ul class="cal">'
        for event in all_day + same_day:
            start = utc(event.get("start"))
            end = utc(event.get("end"))
            if event.get("allDay"):
                when = "All day"
            elif start and end:
                when = f'{clock_time(start.astimezone(zone))}<small>to {clock_time(end.astimezone(zone))}</small>'
            else:
                when = e(event.get("start") or "")
            past = bool(end and not event.get("allDay") and end <= now)
            happening = bool(start and end and not event.get("allDay") and start <= now < end)
            classes = " ".join(
                part
                for part in (
                    "past" if past else "",
                    "stale" if event.get("stale") else "",
                    "clash" if event.get("id") in flight else "",
                    "overlap" if event.get("id") in overlap else "",
                )
                if part
            )
            title = e(event.get("subject") or "Untitled")
            web = safe_href(event.get("webLink"))
            if web:
                title = f'<a href="{e_raw(web)}" target="_blank" rel="noopener noreferrer">{title}</a>'
            tags = ""
            if happening:
                tags += '<span class="b now">Now</span>'
            if nxt is not None and nxt is event:
                tags += '<span class="b next">Next</span>'
            if event.get("id") in flight:
                tags += '<span class="b over">Flight clash</span>'
            elif event.get("id") in overlap:
                tags += '<span class="b soon">Overlap</span>'
            bits = ""
            if event.get("location"):
                bits += f'<div class="nt">{e(event["location"])}</div>'
            note = tidy_note(event.get("note"), zone_label)
            if note:
                bits += f'<div class="nt">{e(note)}</div>'
            links = []
            join = safe_href(event.get("join"))
            join_url = safe_href(event.get("join_url"))
            if join:
                links.append(f'<a class="lk teams" href="{e_raw(join)}" target="_blank" rel="noopener noreferrer">Join Teams</a>')
            if join_url and join_url != join:
                links.append(f'<a class="lk" href="{e_raw(join_url)}" target="_blank" rel="noopener noreferrer">Join</a>')
            if links:
                bits += f'<div class="lks">{"".join(links)}</div>'
            rows += (
                f'<li class="ev {classes}"><div class="tm">{when}</div><div class="bd">'
                f'<div class="et">{tags}{title}</div>{bits}</div></li>'
            )
        rows += "</ul>"
        blocks.append(f'<article class="day"><h3>{e(label)}</h3>{rows}</article>')
    empty = ""
    if empty_days:
        empty = f'<p class="empty">Nothing scheduled {e(", ".join(empty_days))}.</p>'
    return '<div class="days">' + "".join(blocks) + "</div>" + empty


VANCOUVER = ZoneInfo("America/Vancouver")


def _countdown(start, now):
    seconds = (start - now).total_seconds()
    if seconds < 60:
        return "in under a minute"
    hours = int(seconds // 3600)
    if hours < 1:
        return f"in {max(1, int(seconds // 60))} min"
    if hours < 24:
        return "in 1 hour" if hours == 1 else f"in {hours} hours"
    days = int(seconds // 86400)
    return "in 1 day" if days == 1 else f"in {days} days"


def _when_local_and_display(moment, zone, zone_label):
    local = clock_time(moment)
    shown = moment.astimezone(zone)
    if shown.utcoffset() == moment.utcoffset():
        return local
    return f'{local} <small>{clock_time(shown)} {e(zone_label)}</small>'


def _is_midnight(moment):
    return moment.hour == 0 and moment.minute == 0 and moment.second == 0 and moment.microsecond == 0


def _date_label(moment, zone, zone_label):
    label = day_label(moment.date())
    shown = moment.astimezone(zone)
    if shown.utcoffset() != moment.utcoffset() and shown.date() != moment.date():
        return f'{e(label)} <small>{e(day_label(shown.date()))} {e(zone_label)}</small>'
    return e(label)


def _moment_label(moment, zone, zone_label):
    if _is_midnight(moment):
        return _date_label(moment, zone, zone_label)
    return _when_local_and_display(moment, zone, zone_label)


def _travel_times(kind, start, end, zone, zone_label):
    if start and end and start == end:
        end = None
    if kind == "hotel":
        parts = []
        if start:
            parts.append("Check-in " + day_label(start.date()))
        if end:
            parts.append("Check-out " + day_label(end.date()))
        return e(" · ".join(parts))
    if not start:
        return _moment_label(end, zone, zone_label) if end else ""
    text = _moment_label(start, zone, zone_label)
    if end:
        text += " to " + _moment_label(end, zone, zone_label)
    return text


def friendly_day(iso, today):
    text = str(iso or "")[:10]
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        return text
    if today and day == today:
        return "Today"
    if today and day == today + dt.timedelta(days=1):
        return "Tomorrow"
    return day_label(day)


def _link_or_text(text, url):
    href = safe_href(url)
    label = e(text)
    if not href:
        return label
    return f'<a href="{e_raw(href)}" target="_blank" rel="noopener noreferrer">{label}</a>'


def weather_card(rows, today=None):
    if not rows:
        return ""
    body = ""
    any_ok = False
    for row in rows:
        if not isinstance(row, dict) or row.get("unavailable") or row.get("temp") is None:
            body += f'<p class="empty">{e(row.get("name") if isinstance(row, dict) else "")}: weather unavailable</p>'
            continue
        any_ok = True
        line = f'{row.get("temp")} C, {row.get("sky") or ""}'
        if row.get("wind") is not None:
            line += f', wind {row["wind"]} km/h'
        outlook = []
        for day in row.get("days") or []:
            if not isinstance(day, dict):
                continue
            high = day.get("high")
            low = day.get("low")
            span = ""
            if high is not None and low is not None:
                span = f"{high}/{low} C"
            elif high is not None:
                span = f"{high} C"
            outlook.append(
                f'<li><b>{e(friendly_day(day.get("date"), today))}</b> {e(day.get("sky") or "")} {e(span)}</li>'
            )
        body += (
            f'<div class="wx"><h3 class="place">{e(row.get("name"))}</h3>'
            f'<p class="wxnow">{e(line)}</p>'
            + (f'<ul class="wxdays">{"".join(outlook)}</ul>' if outlook else "")
            + "</div>"
        )
    if not any_ok and rows:
        return '<article class="card glance-card" id="weather"><h2>Weather</h2><p>weather unavailable</p></article>'
    return f'<article class="card glance-card" id="weather"><h2>Weather</h2>{body}</article>'


def _flight_codes(text):
    found = re.findall(r"\b([A-Za-z]{2})\s*(\d{2,4})\b", str(text or ""))
    return {f"{airline.upper()}{number}" for airline, number in found}


def _route_airports(route):
    parts = re.split(r"[^A-Za-z]+", str(route or "").upper())
    return [part for part in parts if len(part) == 3]


def _upgrade_match(upgrade, trip, start):
    """True when this upgrade belongs on this trip.

    A flight number match wins. A route match also counts when the dates agree.
    """
    codes = _flight_codes(upgrade.get("flight"))
    trip_codes = _flight_codes(trip.get("title")) | _flight_codes(trip.get("confirmation"))
    date = str(upgrade.get("date") or "")[:10]
    trip_date = start.date().isoformat() if start else ""
    dated = not date or date == trip_date
    if codes and codes & trip_codes and dated:
        return True
    airports = _route_airports(upgrade.get("route"))
    if len(airports) >= 2 and date and date == trip_date:
        origin = str(trip.get("from") or "").upper()
        dest = str(trip.get("to") or "").upper()
        if airports[0] in origin and airports[1] in dest:
            return True
    return False


def _upgrade_html(row, zone):
    title = _link_or_text(row.get("flight") or "Flight", row.get("url"))
    bits = [e(part) for part in (row.get("date"), row.get("route"), row.get("requested_cabin")) if part]
    checked = row.get("checked_at")
    moment = utc(checked) if isinstance(checked, str) else None
    if moment:
        checked_text = "Checked " + day_label(moment.astimezone(zone).date()) + ", " + clock_time(moment.astimezone(zone))
    elif checked:
        checked_text = "Checked " + clean_text(checked)
    else:
        checked_text = ""
    position = row.get("position")
    pos = f"Position {position}" if position not in (None, "") else ""
    return (
        f'<div class="up"><div class="ttl">{title}</div>'
        f'<div class="meta"><span class="b">{e(row.get("status") or "")}</span>'
        + (f"<span>{e(pos)}</span>" if pos else "")
        + "</div>"
        + (f'<div class="nt">{" · ".join(bits)}</div>' if bits else "")
        + (f'<div class="src">{e(checked_text)}</div>' if checked_text else "")
        + "</div>"
    )


def _upgrade_rows(data):
    rows = data.get("upgrades") if isinstance(data, dict) and isinstance(data.get("upgrades"), list) else None
    if not rows:
        return []
    kept = [row for row in rows if isinstance(row, dict)]
    return kept[:12]


def travel_card(data, zone, zone_label, now):
    rows = data.get("travel") if isinstance(data, dict) and isinstance(data.get("travel"), list) else None
    upgrades = _upgrade_rows(data)
    upcoming = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        end = utc(row.get("end"))
        start = utc(row.get("start"))
        if end and end <= now:
            continue
        upcoming.append((start or end or now, row, start, end))
    upcoming.sort(key=lambda item: item[0])
    if not upcoming and not upgrades:
        return ""
    assigned = {index: [] for index in range(len(upcoming))}
    loose = []
    for upgrade in upgrades:
        match_at = None
        for index, (_sort, trip, start, _end) in enumerate(upcoming):
            if _upgrade_match(upgrade, trip, start):
                match_at = index
                break
        if match_at is None:
            loose.append(upgrade)
        else:
            assigned[match_at].append(upgrade)

    def one_trip(index, row, start, end):
        kind = row.get("kind") or "other"
        route = " to ".join(part for part in (clean_text(row.get("from")), clean_text(row.get("to"))) if part)
        times = _travel_times(kind, start, end, zone, zone_label)
        flags = []
        if start and start > now:
            flags.append(f'<span class="b soon">{e(_countdown(start, now))}</span>')
        elif start and end and start <= now < end:
            flags.append('<span class="b now">Underway</span>')
        if kind == "flight" and start and end and end > now and 0 < (start - now).total_seconds() <= 24 * 3600:
            flags.append('<span class="b today">Check-in open</span>')
        if row.get("status"):
            flags.append(f'<span class="b">{e(row.get("status"))}</span>')
        title = _link_or_text(row.get("title") or "Trip", row.get("url"))
        extra = ""
        if row.get("confirmation"):
            extra += f'<div class="nt">Confirmation {e(row.get("confirmation"))}</div>'
        details = clean_text(row.get("details") or "")
        if details:
            if " · " in str(row.get("details") or "") or len(details) > 80:
                extra += f'<details class="fold"><summary>Details</summary><div class="nt">{e(details)}</div></details>'
            else:
                extra += f'<div class="nt">{e(details)}</div>'
        extra += "".join(_upgrade_html(item, zone) for item in assigned[index])
        return (
            f'<div class="trip"><div class="meta"><span class="tag">{e(kind)}</span>{"".join(flags)}</div>'
            f'<div class="ttl">{title}</div>'
            + (f'<div class="nt">{e(route)}</div>' if route else "")
            + (f'<div class="tm">{times}</div>' if times else "")
            + extra
            + "</div>"
        )

    shown = "".join(one_trip(index, row, start, end) for index, (_sort, row, start, end) in enumerate(upcoming[:5]))
    hidden = "".join(one_trip(index, row, start, end) for index, (_sort, row, start, end) in enumerate(upcoming[5:], start=5))
    if loose:
        shown += "".join(_upgrade_html(item, zone) for item in loose)
    if hidden:
        shown += f'<details class="fold"><summary>Show all</summary>{hidden}</details>'
    return f'<article class="card glance-card" id="next-up"><h2>Next up</h2>{shown}</article>'


def today_meetings_card(data, zone, zone_label, now):
    events = drop_clones([event for event in (data.get("calendar") or []) if isinstance(event, dict)])
    if not events:
        return ""
    today = now.astimezone(zone).date()
    horizon = now + dt.timedelta(hours=24)
    chosen = []
    for event in events:
        start = utc(event.get("start")) if not event.get("allDay") else None
        end = utc(event.get("end")) if not event.get("allDay") else None
        if event.get("allDay"):
            start_day = str(event.get("start") or "")
            end_day = str(event.get("end") or "")
            if start_day <= today.isoformat() < end_day:
                chosen.append((start_day, event))
            continue
        if not start:
            continue
        if end and end <= now:
            continue
        on_today = start.astimezone(zone).date() == today
        soon = now <= start <= horizon
        if on_today or soon:
            chosen.append((start.isoformat(), event))
    if not chosen:
        return ""
    chosen.sort(key=lambda item: item[0])
    body = ""
    for _key, event in chosen:
        start = utc(event.get("start"))
        end = utc(event.get("end"))
        if event.get("allDay") or not start:
            when = "All day"
            van = ""
        else:
            when = clock_time(start.astimezone(zone))
            if end:
                when += f" to {clock_time(end.astimezone(zone))}"
            van_time = clock_time(start.astimezone(VANCOUVER))
            van = f'<small class="van">{e(van_time)} Vancouver</small>'
        href = safe_href(event.get("join_url") or event.get("join"))
        join = ""
        if href:
            join = (
                f'<div class="lks"><a class="lk joinbig" href="{e_raw(href)}" target="_blank" '
                f'rel="noopener noreferrer">Join</a></div>'
            )
        title = e(event.get("subject") or "Meeting")
        web = safe_href(event.get("webLink"))
        if web:
            title = f'<a href="{e_raw(web)}" target="_blank" rel="noopener noreferrer">{title}</a>'
        body += (
            f'<div class="meet"><div class="tm">{when}{van}</div>'
            f'<div class="ttl">{title}</div>{join}</div>'
        )
    return body


def glance_html(data, zone, zone_label, weather=None, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    places = data.get("weather_places") if isinstance(data, dict) else None
    weather_rows = weather
    if isinstance(places, list) and places and weather_rows is None:
        try:
            from weather import get_forecasts

            weather_rows = get_forecasts(places)
        except Exception:
            weather_rows = [{"name": "", "unavailable": True}]
    cards = []
    if isinstance(places, list) and places:
        cards.append(weather_card(weather_rows or [{"name": "", "unavailable": True}], now.astimezone(zone).date()))
    if isinstance(data, dict):
        cards.append(travel_card(data, zone, zone_label, now))
    cards = [card for card in cards if card]
    if not cards:
        return ""
    return f'<section class="glance" aria-label="At a glance">{"".join(cards)}</section>'


def pick_restaurant_place(data, today):
    """Location whose date range contains today. Flat restaurants are the fallback."""
    locs = data.get("restaurant_locations") if isinstance(data.get("restaurant_locations"), list) else []
    iso = today.isoformat() if today else ""
    matches = []
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        start = str(loc.get("from") or "0001-01-01")
        until = str(loc.get("until") or "9999-12-31")
        if start <= iso <= until:
            matches.append(loc)
    if matches:
        matches.sort(key=lambda loc: str(loc.get("from") or ""), reverse=True)
        chosen = matches[0]
        return {
            "name": chosen.get("name") or "Here",
            "currency": (chosen.get("currency") or "").upper(),
            "eat": chosen.get("eat") or [],
            "delivery": chosen.get("delivery") or [],
        }
    restaurants = data.get("restaurants") if isinstance(data.get("restaurants"), dict) else {}
    if restaurants.get("eat") or restaurants.get("delivery"):
        return {
            "name": restaurants.get("city") or "Here",
            "currency": "CAD",
            "as_of": restaurants.get("as_of") or "",
            "eat": restaurants.get("eat") or [],
            "delivery": restaurants.get("delivery") or [],
        }
    return None


def cad_price_hint(price, currency, rates):
    if not rates or not currency or currency == "CAD":
        return ""
    per = (rates.get("cad_per") or {}).get(currency)
    match = re.search(r"\d+(?:\.\d+)?", str(price or ""))
    if not per or not match:
        return ""
    amount = float(match.group(0)) * float(per)
    shown = f"{amount:.0f}" if amount >= 10 else f"{amount:.2f}"
    return f" · about {shown} CAD"


def restaurants_html(data, today=None, rates=None):
    place = pick_restaurant_place(data, today or dt.date.today())
    if not place:
        return ""
    city = place.get("name") or ""
    as_of = place.get("as_of") or ""
    currency = place.get("currency") or ""
    eat = place.get("eat") if isinstance(place.get("eat"), list) else []
    delivery = place.get("delivery") if isinstance(place.get("delivery"), list) else []
    sub = ", ".join(part for part in (clean_text(city), clean_text(as_of)) if part)
    summary = "Restaurants" + (f" <small>{e(sub)}</small>" if sub else "")

    def eat_card(row):
        if not isinstance(row, dict):
            return ""
        href = safe_href(row.get("map_url"))
        link = (
            f'<a class="lk" href="{e_raw(href)}" target="_blank" rel="noopener noreferrer">Map</a>'
            if href
            else ""
        )
        price = row.get("price")
        hint = cad_price_hint(price, currency, rates)
        price_html = f'<span class="b">{e(str(price or "") + hint)}</span>' if price not in (None, "") else ""
        return (
            f'<article class="card"><h3 class="place">{e(row.get("name") or "Untitled")}</h3>'
            f'<div class="meta"><span class="tag">{e(row.get("area") or "")}</span>'
            f'<span>{e(row.get("cuisine") or "")}</span>{price_html}</div>'
            f'<p>{e(row.get("why") or "")}</p>'
            + (f'<div class="lks">{link}</div>' if link else "")
            + "</article>"
        )

    def delivery_card(row):
        if not isinstance(row, dict):
            return ""
        href = safe_href(row.get("url"))
        link = (
            f'<a class="lk" href="{e_raw(href)}" target="_blank" rel="noopener noreferrer">Open {e(row.get("app") or "app")}</a>'
            if href
            else ""
        )
        return (
            f'<article class="card"><h3 class="place">{e(row.get("name") or "Untitled")}</h3>'
            f'<div class="meta"><span class="b">{e(row.get("app") or "")}</span></div>'
            f'<p>{e(row.get("why") or "")}</p>'
            + (f'<div class="lks">{link}</div>' if link else "")
            + "</article>"
        )

    body = "<h3>Eat</h3>"
    body += '<div class="cards">' + "".join(eat_card(row) for row in eat) + "</div>" if eat else '<p class="empty">No places to eat in this data.</p>'
    body += "<h3>Delivery</h3>"
    body += (
        '<div class="cards">' + "".join(delivery_card(row) for row in delivery) + "</div>"
        if delivery
        else '<p class="empty">No delivery options in this data.</p>'
    )
    return f'<details id="restaurants" class="keep"><summary>{summary}</summary>{body}</details>'


def money_html(data, today, area):
    rows = []
    for row in data.get("money") or []:
        if isinstance(row, dict):
            rows.append(row)
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "money" or item.get("section") in ("owed", "money"):
            rows.append(
                {
                    "id": item.get("id"),
                    "who": item.get("title"),
                    "direction": "owed_to_me",
                    "amount": item.get("amount") or "",
                    "currency": item.get("currency") or "",
                    "area": item.get("area") or "Personal",
                    "due": item.get("due") or "",
                    "note": item.get("note") or "",
                    "href": (item.get("links") or [{}])[0].get("href") if item.get("links") else "",
                }
            )
    if area != "All":
        rows = [row for row in rows if area_of(row.get("area")) == area]
    if not rows:
        body = '<p class="empty">Nothing owed in this data.</p>'
    else:
        body = "<ul>"
        for row in rows:
            amount = row.get("amount")
            if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                shown = f"{amount:,.2f}"
            else:
                shown = clean_text(amount)
            currency = clean_text(row.get("currency") or "")
            if currency and currency not in shown:
                shown = f"{currency} {shown}".strip()
            direction = DIRECTIONS.get(row.get("direction"), "Owed")
            href = safe_href(row.get("href"))
            who = e(row.get("who") or "Unknown")
            if href:
                who = f'<a href="{e_raw(href)}" target="_blank" rel="noopener noreferrer">{who}</a>'
            body += (
                f'<li class="money" data-area="{e_raw(area_of(row.get("area")))}">'
                f'<div class="ttl">{who}</div>'
                f'<div class="meta"><span class="tag">{e(area_of(row.get("area")))}</span>'
                f'<span class="b">{e(direction)}</span><span class="amt">{e(shown)}</span>'
                f'{due_badge(row.get("due") or "", today)}</div>'
                + (f'<div class="nt">{e(row.get("note") or "")}</div>' if row.get("note") else "")
                + "</li>"
            )
        body += "</ul>"
    return f'<section id="money"><h2>Money owed <small>{len(rows)}</small></h2>{body}</section>'


DEFAULT_MUSK = {
    "title": "Musk 5-step algorithm",
    "bottleneck": "Name the one constraint. Everything else can wait.",
    "steps": [
        ["Question the requirement", "Does this need to exist?"],
        ["Delete", "Remove the step that does not change the result."],
        ["Simplify", "One list, one owner, one next action."],
        ["Accelerate", "Do the remaining step now."],
        ["Automate", "If it will happen again, make the next run automatic."],
    ],
}
DEFAULT_ROBBINS = {
    "title": "Robbins RPM",
    "rpm_result": "The outcome for today, in one sentence.",
    "rpm_purpose": "Why that outcome matters.",
    "rpm_plan": ["Name the result.", "Do the first action.", "Clear the delete list."],
    "standards": ["Important work is scheduled, not left in a pile."],
    "pain": "A day spent on other people's urgency.",
    "pleasure": "A short list that is actually finished.",
    "morning": ["What result am I committed to today?"],
    "evening": ["What did I delete or finish?"],
    "values": ["Clarity", "Follow-through"],
    "kaizen": "One less open loop than yesterday.",
}


def question_list(title, section, values, checked):
    if not isinstance(values, list) or not values:
        return ""
    rows = ""
    for item in values:
        if not isinstance(item, str) or not item.strip():
            continue
        qid = question_id(section, item)
        entry = checked.get(qid) if isinstance(checked, dict) else None
        done = bool(entry.get("done")) if isinstance(entry, dict) else False
        nxt = "0" if done else "1"
        label = "Undo" if done else "Done"
        pressed = "true" if done else "false"
        rows += (
            f'<li class="q{" done" if done else ""}">'
            f'<form class="qform" method="post" action="form/question/toggle">'
            f'{_hidden("id", qid)}{_hidden("done", nxt)}'
            f'<button class="ck qck" type="submit" aria-pressed="{pressed}">{e(label)}</button>'
            f'<span class="qtext">{e(item)}</span></form></li>'
        )
    if not rows:
        return ""
    return f'<div class="lab">{e(title)}</div><ul class="qs">{rows}</ul>'


def principles_html(data, delete_count=0, checked=None):
    checked = checked if isinstance(checked, dict) else {}
    principles = data.get("principles") if isinstance(data.get("principles"), dict) else {}
    if "principles" not in (data or {}) or not principles:
        musk, robbins = DEFAULT_MUSK, DEFAULT_ROBBINS
    else:
        musk = principles.get("musk") if isinstance(principles.get("musk"), dict) and principles.get("musk") else DEFAULT_MUSK
        robbins = principles.get("robbins") if isinstance(principles.get("robbins"), dict) and principles.get("robbins") else DEFAULT_ROBBINS

    def bullets(values, ordered=False):
        if not isinstance(values, list) or not values:
            return ""
        tag = "ol" if ordered else "ul"
        return f"<{tag}>" + "".join(f"<li>{e(item)}</li>" for item in values) + f"</{tag}>"

    html_out = (
        '<details id="principles" class="keep pr"><summary>First principles <small>Question, delete, simplify, accelerate, automate</small></summary>'
        f'<p class="attr"><a href="#quadrant-delete">Delete candidates: {int(delete_count)}</a></p><div class="prg">'
    )
    if musk:
        html_out += f'<article class="card"><div class="lab">{e(musk.get("title") or "Musk 5-step algorithm")}</div>'
        if musk.get("bottleneck"):
            html_out += f'<p class="one"><b>The one bottleneck.</b> {e(musk["bottleneck"])}</p>'
        steps = musk.get("steps") if isinstance(musk.get("steps"), list) else []
        html_out += "<ol>"
        for step in steps:
            if isinstance(step, (list, tuple)) and len(step) >= 2:
                html_out += f"<li><b>{e(step[0])}.</b> {e(step[1])}</li>"
            else:
                html_out += f"<li>{e(step)}</li>"
        html_out += "</ol></article>"
    if robbins:
        html_out += f'<article class="card"><div class="lab">{e(robbins.get("title") or "Robbins")}</div>'
        html_out += f'<p><b>Result.</b> {e(robbins.get("rpm_result") or "")}</p>'
        html_out += f'<p><b>Purpose.</b> {e(robbins.get("rpm_purpose") or "")}</p>'
        if robbins.get("rpm_plan"):
            html_out += "<p><b>Massive action plan</b></p>" + bullets(robbins.get("rpm_plan"), ordered=True)
        html_out += "</article><article class=\"card\">"
        if robbins.get("standards"):
            html_out += '<div class="lab">Raised standards</div>' + bullets(robbins.get("standards"))
        if robbins.get("pain"):
            html_out += f'<p><b>Cost of not acting.</b> {e(robbins["pain"])}</p>'
        if robbins.get("pleasure"):
            html_out += f'<p><b>Payoff of acting.</b> {e(robbins["pleasure"])}</p>'
        if robbins.get("kaizen"):
            html_out += f'<p><b>Kaizen, today\'s 1 percent.</b> {e(robbins["kaizen"])}</p>'
        html_out += "</article><article class=\"card\">"
        if robbins.get("morning"):
            html_out += question_list("Morning questions", "morning", robbins.get("morning"), checked)
        if robbins.get("evening"):
            html_out += question_list("Evening questions", "evening", robbins.get("evening"), checked)
        if robbins.get("values"):
            html_out += '<div class="lab">Top values</div>' + bullets(robbins.get("values"), ordered=True)
        html_out += "</article>"
    html_out += (
        "</div><p class=\"attr\">Principles inspired by Tony Robbins, "
        "<i>Awaken the Giant Within</i>, and Elon Musk's 5-step algorithm.</p></details>"
    )
    return html_out


def numbers_html(data):
    numbers = data.get("numbers") if isinstance(data.get("numbers"), list) else []
    if not numbers:
        return ""
    html_out = f'<details id="numbers" class="keep"><summary>Key numbers ({len(numbers)})</summary><div class="nums">'
    for number in numbers:
        if not isinstance(number, dict):
            continue
        html_out += (
            f'<article class="num"><div class="lab">{e(number.get("label"))}</div>'
            f'<div class="nv">{e(number.get("value"))}</div>'
            + (f'<div class="nt">{e(number["sub"])}</div>' if number.get("sub") else "")
            + (f'<div class="src">{e(number["source"])}</div>' if number.get("source") else "")
            + "</article>"
        )
    return html_out + "</div></details>"


def bottleneck_html(data):
    principles = data.get("principles") if isinstance(data.get("principles"), dict) else {}
    musk = principles.get("musk") if isinstance(principles.get("musk"), dict) else {}
    text = musk.get("bottleneck")
    if not text:
        return ""
    return (
        '<section class="bn"><div class="tag">Top bottleneck</div>'
        f"<p>{e(text)}</p></section>"
    )


def add_form(area):
    selected = area if area in AREAS else "Personal"
    options = "".join(
        f'<option value="{e(name)}"{" selected" if name == selected else ""}>{e(name)}</option>' for name in AREAS
    )
    area_q = "" if area == "All" else area
    return (
        f'<form id="add" class="add" method="post" action="form/todo/add" autocomplete="off">'
        f'<input type="hidden" name="area_q" value="{e_raw(area_q)}">'
        f'<input name="title" required placeholder="Add a task" aria-label="New task" maxlength="300">'
        f'<select name="area" aria-label="Area">{options}</select>'
        f'<input type="date" name="due" aria-label="Due date">'
        f'<button class="btn" type="submit">Add</button></form>'
    )


def chips_html(area):
    parts = []
    for name in ("All",) + tuple(AREAS):
        href = "./" if name == "All" else "./?area=" + quote(name)
        on = " on" if name == area else ""
        parts.append(f'<a class="chip{on}" data-area="{e(name)}" href="{href}">{e(name)}</a>')
    return f'<nav id="chips" aria-label="Filter by area">{"".join(parts)}</nav>'


def _snoozed(item, today):
    snooze = item.get("snooze")
    return bool(snooze and isinstance(snooze, str) and snooze > today.isoformat())


QUADRANTS = ("do_now", "schedule", "delegate", "delete")
QUAD_LABELS = {
    "do_now": "Do now",
    "schedule": "Schedule",
    "delegate": "Delegate",
    "delete": "Delete or drop",
}


def _quadrant_signals(item, today):
    """Same importance and urgency signals derive_quadrant uses."""
    due = item.get("due") or ""
    important = item.get("important") is True or item.get("priority") in ("high", "urgent") or item.get("section") == "now"
    urgent = item.get("section") == "now"
    days = None
    if due:
        try:
            days = (dt.date.fromisoformat(due) - today).days
            urgent = urgent or days <= 2
        except ValueError:
            due = ""
    return important, urgent, due, days


def derive_quadrant(item, today):
    explicit = item.get("quadrant")
    if explicit in QUADRANTS:
        return explicit
    important, urgent, due, _days = _quadrant_signals(item, today)
    owner = item.get("owner") or ""
    if owner and not important:
        return "delegate"
    if item.get("section") == "waiting":
        return "delegate"
    if important and urgent:
        return "do_now"
    if important:
        return "schedule"
    if urgent:
        return "delegate"
    return "delete"


def quadrant_reason(item, today):
    """One short line for why a task sits in its quadrant. No dashes."""
    if item.get("quadrant_manual"):
        return "Moved manually"
    important, urgent, due, days = _quadrant_signals(item, today)
    if important:
        lead = "Important"
    elif urgent:
        lead = "Urgent"
    else:
        lead = "Not urgent"
    if not due or days is None:
        if important and urgent:
            return "Important, urgent"
        if urgent:
            return "Urgent, no due date"
        if important:
            return "Important, no due date"
        return "Not urgent, no due date"
    if days < 0:
        count = -days
        timing = f"overdue {count} day" if count == 1 else f"overdue {count} days"
    elif days == 0:
        timing = "due today"
    elif days == 1:
        timing = "due in 1 day"
    else:
        timing = f"due in {days} days"
    return f"{lead}, {timing}"


def freshness_class(generated, now):
    if not generated or not now:
        return ""
    hours = (now - generated).total_seconds() / 3600
    if hours >= 24:
        return "stale-red"
    if hours >= 12:
        return "stale-amber"
    return ""


def _status_class(number):
    raw = str((number or {}).get("status") or "").lower()
    if raw in ("red", "amber", "green"):
        return raw
    text = f"{number.get('label') or ''} {number.get('value') or ''}".lower()
    if "overdue" in text or "critical" in text:
        return "red"
    if "watch" in text or "pending" in text:
        return "amber"
    return "green"


def jarvis_html(data, tasks, today):
    jarvis = data.get("jarvis") if isinstance(data.get("jarvis"), dict) else {}
    do_now = [item for item in tasks if derive_quadrant(item, today) == "do_now" and not item.get("done")]
    on_fire = jarvis.get("on_fire") or ""
    if not on_fire:
        principles = data.get("principles") if isinstance(data.get("principles"), dict) else {}
        musk = principles.get("musk") if isinstance(principles.get("musk"), dict) else {}
        on_fire = musk.get("bottleneck") or (do_now[0]["title"] if do_now else "Nothing is flagged on fire.")
    decision = jarvis.get("decision") or (do_now[0]["title"] if do_now else "No decision is queued for today.")
    numbers = jarvis.get("numbers") if isinstance(jarvis.get("numbers"), list) else None
    if numbers is None:
        numbers = [n for n in (data.get("numbers") or []) if isinstance(n, dict)]
    if not numbers:
        owed = [row for row in (data.get("money") or []) if isinstance(row, dict)]
        if owed:
            numbers = [{
                "label": "Money rows",
                "value": str(len(owed)),
                "status": "amber",
                "sub": "From the money list",
            }]
    cards = ""
    if numbers:
        for number in numbers[:5]:
            cards += (
                f'<article class="kpi {_status_class(number)}"><div class="lab">{e(number.get("label"))}</div>'
                f'<div class="nv">{e(number.get("value"))}</div>'
                + (f'<div class="src">{e(number.get("sub") or number.get("source") or "")}</div>' if number.get("sub") or number.get("source") else "")
                + "</article>"
            )
    else:
        cards = '<p class="empty">No key numbers in this data.</p>'
    return (
        '<section class="jarvis" id="jarvis">'
        f'<p class="fire"><span class="lab">On fire</span> {e(on_fire)}</p>'
        f'<p class="decide"><span class="lab">Decision today</span> {e(decision)}</p>'
        f'<div class="kpis">{cards}</div></section>'
    )


def quadrant_html(items, today, area, group):
    dated = [item for item in items if item.get("due")]
    undated = [item for item in items if not item.get("due")]
    html_out = list_html(dated, today, area, group) if dated else ""
    if undated:
        html_out += (
            f'<details class="keep"><summary>Undated ({len(undated)})</summary>'
            + list_html(undated, today, area, False)
            + "</details>"
        )
    return html_out


def quadrant_block(key, rows, today, area, group):
    if not rows:
        return f'<p class="quad-line" id="quadrant-{key}">{e(QUAD_LABELS[key])}: nothing.</p>'
    return (
        f'<section class="quad q-{key}" id="quadrant-{key}">'
        f'<h2>{e(QUAD_LABELS[key])} <small>{len(rows)}</small></h2>'
        + quadrant_html(rows, today, area, group)
        + "</section>"
    )


def eisenhower_blocks(tasks, today, area, group):
    buckets = {key: [] for key in QUADRANTS}
    for item in tasks:
        resolved = derive_quadrant(item, today)
        item["quadrant_resolved"] = resolved
        buckets[resolved].append(item)
    blocks = {key: quadrant_block(key, buckets[key], today, area, group) for key in QUADRANTS}
    return blocks, len(buckets["delete"])


def waiting_html(data, state, today, area):
    source = data.get("waiting_on") if isinstance(data.get("waiting_on"), list) else None
    rows = []
    if source is not None:
        raw_rows = source
    else:
        raw_rows = []
        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("section") == "waiting":
                raw_rows.append({
                    "id": item.get("id"),
                    "who": item.get("owner") or "Someone",
                    "what": item.get("title"),
                    "since": item.get("since") or item.get("due") or "",
                    "channel": "email" if item.get("kind") in ("email", "draft") else "meeting",
                    "url": ((item.get("links") or [{}])[0] or {}).get("href") if item.get("links") else "",
                    "area": item.get("area") or "Personal",
                })
    todos = (state or {}).get("todos") or {}
    for row in raw_rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        overlay = todos.get(row["id"]) or {}
        if overlay.get("done"):
            continue
        snooze = overlay.get("snooze")
        if snooze and isinstance(snooze, str) and snooze > today.isoformat():
            continue
        since = row.get("since") or ""
        try:
            days = (today - dt.date.fromisoformat(since)).days
        except ValueError:
            days = None
        rows.append((-(days if days is not None else -9999), days, row, overlay))
    rows.sort(key=lambda item: item[0])
    if not rows:
        return '<p class="quad-line" id="waiting-on">Waiting on: nothing.</p>'
    body = ""
    for _key, days, row, overlay in rows:
        hot = " hot" if days is not None and days > 5 else ""
        who = row.get("who") or "Someone"
        what = row.get("what") or row.get("title") or "Waiting"
        href = safe_href(row.get("url") or row.get("href"))
        title = e(what)
        if href:
            title = f'<a href="{e_raw(href)}" target="_blank" rel="noopener noreferrer">{title}</a>'
        day_text = f"{days}d waiting" if days is not None else "date missing"
        body += (
            f'<li class="it wait{hot}" data-id="{e_raw(row["id"])}">'
            f'<form class="act" method="post" action="form/todo/toggle">{_hidden("id", row["id"])}'
            f'{_hidden("done", "1")}<button class="ck" type="submit">Done</button></form>'
            f'<div class="bd"><div class="ttl">{title}</div>'
            f'<div class="meta"><span class="tag">{e(row.get("channel") or "email")}</span>'
            f'<span>{e(who)}</span><span class="b{" over" if hot else ""}">{e(day_text)}</span>'
            f'<span>since {e(since)}</span></div>'
            f'<details class="fold"><summary>Snooze</summary>'
            f'<form method="post" action="form/snooze">{_hidden("id", row["id"])}{_hidden("kind", "todo")}'
            f'<label>Back on<input type="date" name="until" value="{e_raw(overlay.get("snooze") or "")}"></label>'
            f'<button class="btn" type="submit">Snooze</button></form></details></div></li>'
        )
    return (
        f'<section id="waiting-on"><h2>Waiting on <small>{len(rows)}</small></h2>'
        f'<ul>{body}</ul></section>'
    )


def render_main(data, state, area="All", today=None, zone=None, zone_label=None, weather=None, now=None, rates=None, env=None):
    if zone is None:
        zone, zone_label = resolve_display()
    elif not zone_label:
        zone_label = zone_city(getattr(zone, "key", FALLBACK_ZONE))
    today = today or dt.datetime.now(zone).date()
    area = area if area in AREAS else "All"
    rows = [item for item in merged(data, state) if area == "All" or item["area"] == area]
    emails = [item for item in rows if item.get("kind") in ("email", "draft")]
    tasks = [
        item
        for item in rows
        if item.get("kind") not in ("email", "draft", "money") and item.get("section") not in ("owed", "money")
    ]
    active = [item for item in tasks if not item["done"] and not _snoozed(item, today)]
    snoozed = [item for item in tasks if not item["done"] and _snoozed(item, today)]
    done = sorted([item for item in tasks if item["done"]], key=lambda item: item.get("done_at") or "", reverse=True)
    mail_live = [item for item in emails if not item["done"] and not _snoozed(item, today)]
    mail_done = [item for item in emails if item["done"]]
    group = area == "All"
    clock = now or dt.datetime.now(dt.timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=dt.timezone.utc)
    active_tasks = [item for item in active if item.get("section") != "waiting"]
    for item in active_tasks:
        item["quadrant_resolved"] = derive_quadrant(item, today)
    blocks, delete_count = eisenhower_blocks(active_tasks, today, area, group)
    jarvis = jarvis_html(data, active_tasks, today)
    html_out = f'<div class="home-top">{blocks["do_now"]}{waiting_html(data, state, today, area)}</div>'
    html_out += (
        f'<div class="quad-rest">{blocks["schedule"]}{blocks["delegate"]}{blocks["delete"]}</div>'
    )
    meetings = today_meetings_card(data, zone, zone_label, clock)
    html_out += f'<section id="calendar"><h2>Today\'s calendar <small>{e(zone_label)}</small></h2>'
    html_out += meetings if meetings else '<p class="empty">Nothing on today.</p>'
    html_out += f'<h2>Today and the next 7 days <small>{e(zone_label)}</small></h2>'
    html_out += calendar_html(data, today, zone, zone_label) + "</section>"
    html_out += restaurants_html(data, today, rates)
    html_out += glance_html(data, zone, zone_label, weather=weather, now=clock)
    html_out += f'<section id="emails"><h2>Emails waiting <small>{len(mail_live)}</small></h2>'
    html_out += list_html(mail_live, today, area, group)
    if mail_done:
        html_out += f'<details class="keep"><summary>Handled mail ({len(mail_done)})</summary>' + list_html(mail_done, today, area, False) + "</details>"
    html_out += "</section>"
    html_out += add_form(area)
    if snoozed:
        html_out += f'<details class="keep"><summary>Snoozed ({len(snoozed)})</summary>' + list_html(snoozed, today, area, False) + "</details>"
    html_out += f'<details id="donebox" class="keep"><summary>Done ({len(done)})</summary>' + list_html(done, today, area, False) + "</details>"
    html_out += money_html(data, today, area)
    ritual, _ritual_zone = ritual_day(env, data, clock)
    questions = (state or {}).get("questions") if isinstance(state, dict) else None
    checked = questions.get(ritual) if isinstance(questions, dict) else None
    html_out += principles_html(data, delete_count, checked if isinstance(checked, dict) else {})
    generated = utc(data.get("generated_at")) if isinstance(data, dict) else None
    if generated:
        local = generated.astimezone(zone)
        when = day_label(local.date()) + ", " + clock_time(local)
    else:
        when = "unknown"
    parts = [
        f"Data refreshed {when} {zone_label}.",
        "Ticks stay on the server when data is refreshed.",
    ]
    for note in data.get("notes") or []:
        if isinstance(note, str) and note.strip():
            parts.append(clean_text(note))
    sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    connected = sources.get("connected") if isinstance(sources.get("connected"), list) else []
    needed = sources.get("needed") if isinstance(sources.get("needed"), list) else []
    if connected:
        parts.append("Connected: " + ", ".join(clean_text(item) for item in connected) + ".")
    if needed:
        parts.append("Still needed: " + ", ".join(clean_text(item) for item in needed) + ".")
    html_out += "<footer>" + e(" ".join(parts)) + "</footer>"
    do_now_count = sum(1 for item in active_tasks if item.get("quadrant_resolved") == "do_now")
    counts = f"{do_now_count} now, {len(mail_live)} email, {len(done)} done"
    return html_out, counts, jarvis


def clocks_html(moment=None, env=None, data=None, today=None):
    moment = moment or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    parts = []
    for label, zone_name, mark, current, role in clock_rows(env, data, today):
        local = moment.astimezone(ZoneInfo(zone_name))
        hour = local.hour
        weekday = local.strftime("%a")
        open_now = 8 <= hour < 18 and weekday not in ("Sat", "Sun")
        classes = "clk" + (" current" if current else "") + (" on" if open_now else "")
        role_html = f'<span class="role">{e(mark)}</span>' if mark else ""
        parts.append(
            f'<div class="{classes}" data-tz="{e_raw(zone_name)}" data-role="{e_raw(role)}">'
            f'<b>{e(label)}</b>'
            f'<span class="ct">{clock_time(local)}</span>'
            f'<span class="cd">{e(day_label(local.date()))}</span>{role_html}</div>'
        )
    return "".join(parts)


def _fx_pair(code, rates):
    cad = (rates or {}).get("cad_per") or {}
    try:
        value = float(cad.get(code))
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    shown = f"{value:.6f}".rstrip("0").rstrip(".")
    return f' <span class="fxpair">{e(code)} to CAD <b id="fx{code.lower()}">{e(shown)}</b></span>'


def fx_html(data, rates=None):
    view = fx_view(data or {})
    pairs = _fx_pair("THB", rates) + _fx_pair("JPY", rates)
    if not view:
        return (
            '<p class="fxline" id="fxchip" data-rate="" data-asof="" data-source="">'
            'USD to CAD <b id="fxr">unavailable</b>'
            + pairs
            + ' <span id="fxd"></span> <span id="fxs">Bank of Canada Valet loads when this page is online.</span></p>'
        )
    rate = f"{view['rate']:.4f}"
    return (
        f'<p class="fxline" id="fxchip" data-rate="{e_raw(rate)}" data-asof="{e_raw(view["as_of"])}" '
        f'data-source="{e(view["source"])}">USD to CAD <b id="fxr">{e(rate)}</b>'
        + pairs
        + f' <span id="fxd">{e(view["as_of"])}</span>'
        f' <span id="fxs">{e(view["source"])}</span></p>'
    )


def asof_html(data, zone=None, zone_label=None):
    if zone is None:
        zone, zone_label = resolve_display()
    elif not zone_label:
        zone_label = zone_city(getattr(zone, "key", FALLBACK_ZONE))
    generated = utc((data or {}).get("generated_at")) if isinstance(data, dict) else None
    if not generated:
        return f"Data time is not in this file. Board times use {zone_label}. Clock times are local to each city."
    local = generated.astimezone(zone)
    stamp = day_label(local.date()) + ", " + clock_time(local)
    return f"Data as of {stamp} {zone_label}. Clock times are local to each city."


def json_for_script(rates):
    payload = rates or {"cad_per": {"CAD": 1}, "usd_per": {"CAD": 1}, "source": "unavailable", "date": ""}
    return json.dumps(payload).replace("<", "\\u003c")


def fill(template, data, state, area="All", today=None, env=None, weather=None, now=None, rates=None):
    zone, zone_label = resolve_display(env)
    _label, home_key = home_clock(env)
    moment = now or dt.datetime.now(dt.timezone.utc)
    if getattr(moment, "tzinfo", None) is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    main, counts, jarvis = render_main(
        data, state, area, today=today, zone=zone, zone_label=zone_label, weather=weather, now=moment, rates=rates, env=env
    )
    clock_day = today or moment.astimezone(zone).date()
    generated = utc((data or {}).get("generated_at")) if isinstance(data, dict) else None
    fresh = freshness_class(generated, moment if getattr(moment, "tzinfo", None) else None)
    return (
        template.replace("<!--PRE_MAIN-->", main)
        .replace("<!--PRE_JARVIS-->", jarvis)
        .replace("<!--PRE_CHIPS-->", chips_html(area))
        .replace("<!--PRE_CLOCKS-->", clocks_html(moment, env=env, data=data, today=clock_day))
        .replace("<!--PRE_HOME_TZ-->", e_raw(home_key))
        .replace("<!--PRE_COUNTS-->", e(counts))
        .replace("<!--PRE_FX-->", fx_html(data, rates))
        .replace("<!--PRE_ASOF-->", e(asof_html(data, zone, zone_label)))
        .replace("<!--PRE_FRESH-->", e_raw(fresh))
        .replace("<!--PRE_FX_JSON-->", json_for_script(rates))
    )
