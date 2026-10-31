"""Server-rendered cockpit page. The same HTML works with JavaScript off."""
import datetime as dt
import hashlib
import html
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo

from actions import AREAS, area_of
from textclean import clean_text

PT = ZoneInfo("America/Vancouver")
CAL_WINDOW = 8  # today and the next 7 days
FLIGHT = re.compile(
    r"\b(?:DL|UA|AC|WS|AA|AS|NH|KE|TG|JL|OZ|SQ|CX|BA|LH|AF|KL)\s?\d{2,4}\b|\bflight\b",
    re.I,
)
CLOCKS = (
    ("Vancouver", "America/Vancouver", "Home", True),
    ("Chicago", "America/Chicago", "Central", False),
    ("New York", "America/New_York", "Eastern", False),
    ("Netherlands", "Europe/Amsterdam", "Europe", False),
    ("Sweden", "Europe/Stockholm", "Europe", False),
    ("Seoul", "Asia/Seoul", "Korea", False),
    ("Bangkok", "Asia/Bangkok", "Thailand", False),
    ("Tokyo", "Asia/Tokyo", "Japan", False),
)
DIRECTIONS = {"owed_to_me": "Owed to you", "i_owe": "You owe"}

e = lambda value: html.escape(clean_text(value), quote=True)
e_raw = lambda value: html.escape("" if value is None else str(value), quote=True)


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
            }
        )
    for custom in state.get("custom") or []:
        if not isinstance(custom, dict) or not custom.get("id"):
            continue
        overlay = todos.get(custom["id"]) or {}
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
                "snooze": custom.get("snooze"),
                "note": custom.get("notes") or "",
                "custom": True,
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
    note = item.get("done_note") if item["done"] and item.get("done_note") else item.get("note")
    links = "".join(link_html(link) for link in item.get("links") or [])
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
    return (
        f'<li class="it{" done" if item["done"] else ""}" id="{e_raw(dom_id("i", item["id"]))}" data-id="{e_raw(item["id"])}" data-area="{e_raw(item["area"])}">'
        f'<form class="act" method="post" action="form/{op}">{_hidden("id", item["id"])}'
        f'{_hidden(field, nxt)}{_hidden("area", area_value)}'
        f'<button class="ck{" em" if email else ""}" type="submit" aria-label="{e(verb)}">{e(label)}</button></form>'
        f'<div class="bd"><div class="ttl">{e(item["title"])}</div><div class="meta">{meta}</div>'
        + (f'<div class="nt">{e(note)}</div>' if note else "")
        + (f'<div class="lks">{links}</div>' if links else "")
        + f'<div class="rowtools">{edit}{snooze}</div></div></li>'
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


def calendar_html(data, today):
    events = [event for event in (data.get("calendar") or []) if isinstance(event, dict)]
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
            if start and start.astimezone(PT).date() == day:
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
                when = f'{clock_time(start.astimezone(PT))}<small>to {clock_time(end.astimezone(PT))}</small>'
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
            if event.get("note"):
                bits += f'<div class="nt">{e(event["note"])}</div>'
            join = safe_href(event.get("join"))
            if join:
                bits += (
                    f'<div class="lks"><a class="lk teams" href="{e_raw(join)}" target="_blank" '
                    f'rel="noopener noreferrer">Join Teams</a></div>'
                )
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


def restaurants_html(data):
    restaurants = data.get("restaurants") if isinstance(data.get("restaurants"), dict) else {}
    city = restaurants.get("city") or ""
    as_of = restaurants.get("as_of") or ""
    eat = restaurants.get("eat") if isinstance(restaurants.get("eat"), list) else []
    delivery = restaurants.get("delivery") if isinstance(restaurants.get("delivery"), list) else []
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
        price_html = f'<span class="b">{e(price)}</span>' if price not in (None, "") else ""
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
    return f'<details id="restaurants" class="keep" open><summary>{summary}</summary>{body}</details>'


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


def principles_html(data):
    principles = data.get("principles") if isinstance(data.get("principles"), dict) else {}
    musk = principles.get("musk") if isinstance(principles.get("musk"), dict) else {}
    robbins = principles.get("robbins") if isinstance(principles.get("robbins"), dict) else {}
    if not musk and not robbins:
        return ""

    def bullets(values, ordered=False):
        if not isinstance(values, list) or not values:
            return ""
        tag = "ol" if ordered else "ul"
        return f"<{tag}>" + "".join(f"<li>{e(item)}</li>" for item in values) + f"</{tag}>"

    html_out = '<details id="principles" class="keep pr"><summary>Principles <small>Musk 5 steps, Robbins RPM</small></summary><div class="prg">'
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
            html_out += '<div class="lab">Morning questions</div>' + bullets(robbins.get("morning"))
        if robbins.get("evening"):
            html_out += '<div class="lab">Evening questions</div>' + bullets(robbins.get("evening"))
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


def render_main(data, state, area="All", today=None):
    today = today or dt.datetime.now(PT).date()
    area = area if area in AREAS else "All"
    rows = [item for item in merged(data, state) if area == "All" or item["area"] == area]
    emails = [item for item in rows if item.get("kind") in ("email", "draft")]
    tasks = [
        item
        for item in rows
        if item.get("kind") not in ("email", "draft", "money") and item.get("section") not in ("owed", "money")
    ]
    active = [item for item in tasks if not item["done"] and not _snoozed(item, today)]
    now_rows = [item for item in active if item.get("section") == "now"]
    now_ids = {item["id"] for item in now_rows}
    open_rows = sorted(
        [item for item in active if item.get("section") == "open" and item["id"] not in now_ids],
        key=lambda item: (item.get("priority") == "low", item.get("due") or "9999"),
    )
    waiting = sorted(
        [item for item in active if item.get("section") == "waiting"],
        key=lambda item: item.get("due") or "9999",
    )
    watch = [item for item in active if item.get("section") == "watch"]
    other = [
        item
        for item in active
        if item.get("section") not in ("now", "open", "waiting", "watch") and item["id"] not in now_ids
    ]
    open_rows = other + open_rows
    snoozed = [item for item in tasks if not item["done"] and _snoozed(item, today)]
    done = sorted([item for item in tasks if item["done"]], key=lambda item: item.get("done_at") or "", reverse=True)
    mail_live = [item for item in emails if not item["done"] and not _snoozed(item, today)]
    mail_done = [item for item in emails if item["done"]]
    group = area == "All"
    html_out = chips_html(area)
    html_out += bottleneck_html(data)
    html_out += restaurants_html(data)
    html_out += f'<section id="tasks"><h2>Do now <small>{len(now_rows)}</small></h2>'
    html_out += list_html(now_rows, today, area, group) + "</section>"
    html_out += add_form(area)
    html_out += f'<section><h2>Open <small>{len(open_rows)}</small></h2>' + list_html(open_rows, today, area, group) + "</section>"
    html_out += f'<details class="keep"><summary>Waiting on others ({len(waiting)})</summary>' + list_html(waiting, today, area, group) + "</details>"
    html_out += f'<details class="keep"><summary>Background watches ({len(watch)})</summary>' + list_html(watch, today, area, group) + "</details>"
    if snoozed:
        html_out += f'<details class="keep"><summary>Snoozed ({len(snoozed)})</summary>' + list_html(snoozed, today, area, False) + "</details>"
    html_out += f'<details id="donebox" class="keep"><summary>Done ({len(done)})</summary>' + list_html(done, today, area, False) + "</details>"
    html_out += f'<section id="emails"><h2>Emails waiting <small>{len(mail_live)}</small></h2>'
    html_out += list_html(mail_live, today, area, group)
    if mail_done:
        html_out += f'<details class="keep"><summary>Handled mail ({len(mail_done)})</summary>' + list_html(mail_done, today, area, False) + "</details>"
    html_out += "</section>"
    html_out += '<section id="calendar"><h2>Today and the next 7 days <small>Pacific</small></h2>'
    html_out += calendar_html(data, today) + "</section>"
    html_out += money_html(data, today, area)
    html_out += principles_html(data)
    html_out += numbers_html(data)
    generated = utc(data.get("generated_at")) if isinstance(data, dict) else None
    if generated:
        local = generated.astimezone(PT)
        when = day_label(local.date()) + ", " + clock_time(local)
    else:
        when = "unknown"
    parts = [
        f"Data refreshed {when} Pacific.",
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
    counts = f"{len(now_rows)} now, {len(open_rows)} open, {len(mail_live)} email, {len(done)} done"
    return html_out, counts


def clocks_html(moment=None):
    moment = moment or dt.datetime.now(dt.timezone.utc)
    parts = []
    for label, zone, subtitle, home in CLOCKS:
        local = moment.astimezone(ZoneInfo(zone))
        hour = local.hour
        weekday = local.strftime("%a")
        open_now = 8 <= hour < 18 and weekday not in ("Sat", "Sun")
        classes = "clk" + (" home" if home else "") + (" on" if open_now else "")
        parts.append(
            f'<div class="{classes}" data-tz="{e_raw(zone)}"><b>{e(label)}</b>'
            f'<span class="ct">{clock_time(local)}</span>'
            f'<span class="cd">{e(day_label(local.date()))}</span>'
            f'<small class="sub">{e(subtitle)}</small></div>'
        )
    return "".join(parts)


def fx_html(data):
    view = fx_view(data or {})
    if not view:
        return (
            '<p class="fxline" id="fxchip" data-rate="" data-asof="" data-source="">'
            'USD to CAD <b id="fxr">unavailable</b>'
            ' · <span id="fxd"></span> · <span id="fxs">Bank of Canada Valet loads when this page is online.</span></p>'
        )
    rate = f"{view['rate']:.4f}"
    return (
        f'<p class="fxline" id="fxchip" data-rate="{e_raw(rate)}" data-asof="{e_raw(view["as_of"])}" '
        f'data-source="{e(view["source"])}">USD to CAD <b id="fxr">{e(rate)}</b>'
        f' · <span id="fxd">{e(view["as_of"])}</span>'
        f' · <span id="fxs">{e(view["source"])}</span></p>'
    )


def asof_html(data):
    generated = utc((data or {}).get("generated_at")) if isinstance(data, dict) else None
    if not generated:
        return "Data time is not in this file. Times on the clocks are Pacific, Central, Eastern, and local."
    local = generated.astimezone(PT)
    stamp = day_label(local.date()) + ", " + clock_time(local)
    return f"Data as of {stamp} Pacific. Times below are local to each city."


def fill(template, data, state, area="All", today=None):
    main, counts = render_main(data, state, area, today=today)
    return (
        template.replace("<!--PRE_MAIN-->", main)
        .replace("<!--PRE_CLOCKS-->", clocks_html())
        .replace("<!--PRE_COUNTS-->", e(counts))
        .replace("<!--PRE_FX-->", fx_html(data))
        .replace("<!--PRE_ASOF-->", e(asof_html(data)))
    )
