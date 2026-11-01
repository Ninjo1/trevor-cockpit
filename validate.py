"""Shape checks for a cockpit data.json push.

Unknown keys are kept. A bad type is rejected so the previous file stays in
place. restaurants is required.
"""
import datetime as dt
import re

ITEM_KINDS = {"task", "email", "draft", "meeting", "money"}
SECTIONS = {"now", "open", "waiting", "watch", "owed", "money"}
AREAS_HINT = {"Echandia", "FiFi4", "Personal", "Personal / Own cos", "Household"}
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}\Z")
DUE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
TRAVEL_KINDS = {"flight", "hotel", "car", "train", "other"}
MAX_ITEMS = 2000


def _loose_id(value, limit, field):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{field} must be a non-empty string")
    if re.search(r"[\x00-\x1f]", value):
        raise ValueError(f"{field} has a control character")


def _string(obj, key, field, required=True):
    if key not in obj:
        if required:
            raise ValueError(f"{field} is required")
        return
    if not isinstance(obj[key], str):
        raise ValueError(f"{field} must be a string")


def _string_list(obj, key, field):
    if key not in obj or obj[key] is None:
        return
    if not isinstance(obj[key], list) or not all(isinstance(x, str) for x in obj[key]):
        raise ValueError(f"{field} must be a list of strings")


def validate_data(obj):
    if not isinstance(obj, dict):
        raise ValueError("data must be an object")
    items = obj.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    if len(items) > MAX_ITEMS:
        raise ValueError("items is too long")
    calendar = obj.get("calendar")
    if not isinstance(calendar, list):
        raise ValueError("calendar must be a list")
    if len(calendar) > MAX_ITEMS:
        raise ValueError("calendar is too long")
    _restaurants(obj.get("restaurants"))
    _items(items)
    _calendar(calendar)
    if "money" in obj and obj["money"] is not None:
        _money(obj["money"])
    if "notes" in obj and obj["notes"] is not None:
        if not isinstance(obj["notes"], list) or not all(isinstance(x, str) for x in obj["notes"]):
            raise ValueError("notes must be a list of strings")
    if "principles" in obj and obj["principles"] is not None and not isinstance(obj["principles"], dict):
        raise ValueError("principles must be an object")
    if "numbers" in obj and obj["numbers"] is not None and not isinstance(obj["numbers"], list):
        raise ValueError("numbers must be a list")
    if "fx" in obj and obj["fx"] is not None:
        _fx(obj["fx"])
    if "fx_usdcad" in obj and obj["fx_usdcad"] is not None and not _number(obj["fx_usdcad"]):
        raise ValueError("fx_usdcad must be a number")
    if "generated_at" in obj and obj["generated_at"] is not None and not isinstance(obj["generated_at"], str):
        raise ValueError("generated_at must be a string")
    if "schema" in obj and obj["schema"] is not None and not isinstance(obj["schema"], int):
        raise ValueError("schema must be an integer")
    if "weather_places" in obj and obj["weather_places"] is not None:
        _weather_places(obj["weather_places"])
    if "travel" in obj and obj["travel"] is not None:
        _travel(obj["travel"])
    if "upgrades" in obj and obj["upgrades"] is not None:
        _upgrades(obj["upgrades"])
    return obj


def _http_url(value, field):
    if not isinstance(value, str) or not (value.startswith("https://") or value.startswith("http://")):
        raise ValueError(f"{field} must be an http or https url")
    if any(char in value for char in " \n\r\t\"<>"):
        raise ValueError(f"{field} must be an http or https url")


def _attachments(rows, index):
    if not isinstance(rows, list):
        raise ValueError(f"items[{index}].attachments must be a list")
    if len(rows) > 20:
        raise ValueError(f"items[{index}].attachments is too long")
    for att_index, row in enumerate(rows):
        field = f"items[{index}].attachments[{att_index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{field} must be an object")
        name = row.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError(f"{field}.name must be a string of at most 80 characters")
        _http_url(row.get("url"), f"{field}.url")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fx(fx):
    if _number(fx):
        if fx <= 0:
            raise ValueError("fx must be a positive number")
        return
    if not isinstance(fx, dict):
        raise ValueError("fx must be a number or an object")
    raw = fx.get("usdcad", fx.get("rate"))
    if not _number(raw) or raw <= 0:
        raise ValueError("fx.usdcad must be a positive number")


def _restaurants(restaurants):
    if not isinstance(restaurants, dict):
        raise ValueError("restaurants must be an object")
    _string(restaurants, "city", "restaurants.city")
    _string(restaurants, "as_of", "restaurants.as_of")
    eat = restaurants.get("eat")
    delivery = restaurants.get("delivery")
    if not isinstance(eat, list):
        raise ValueError("restaurants.eat must be a list")
    if not isinstance(delivery, list):
        raise ValueError("restaurants.delivery must be a list")
    if len(eat) > 200 or len(delivery) > 200:
        raise ValueError("restaurants list is too long")
    for index, row in enumerate(eat):
        if not isinstance(row, dict):
            raise ValueError(f"restaurants.eat[{index}] must be an object")
        for key in ("name", "area", "cuisine", "why", "map_url"):
            _string(row, key, f"restaurants.eat[{index}].{key}")
        price = row.get("price")
        if not isinstance(price, str) and not _number(price):
            raise ValueError(f"restaurants.eat[{index}].price must be a string or number")
    for index, row in enumerate(delivery):
        if not isinstance(row, dict):
            raise ValueError(f"restaurants.delivery[{index}] must be an object")
        for key in ("name", "app", "why", "url"):
            _string(row, key, f"restaurants.delivery[{index}].{key}")


def _items(items):
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}] must be an object")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
            raise ValueError(f"items[{index}].id is not a valid id")
        if item_id in seen:
            raise ValueError(f"duplicate item id {item_id}")
        seen.add(item_id)
        if not isinstance(item.get("title"), str):
            raise ValueError(f"items[{index}].title must be a string")
        if "area" in item and item["area"] is not None and not isinstance(item["area"], str):
            raise ValueError(f"items[{index}].area must be a string")
        if "kind" in item and item["kind"] is not None and item["kind"] not in ITEM_KINDS:
            raise ValueError(f"items[{index}].kind is not a known kind")
        if "section" in item and item["section"] is not None and item["section"] not in SECTIONS:
            raise ValueError(f"items[{index}].section is not a known section")
        if "due" in item and item["due"] not in (None, "") and (
            not isinstance(item["due"], str) or not DUE.fullmatch(item["due"])
        ):
            raise ValueError(f"items[{index}].due must be YYYY-MM-DD")
        if "done" in item and not isinstance(item["done"], bool):
            raise ValueError(f"items[{index}].done must be true or false")
        if "priority" in item and item["priority"] is not None and not isinstance(item["priority"], str):
            raise ValueError(f"items[{index}].priority must be a string or null")
        _string_list(item, "ledger_ids", f"items[{index}].ledger_ids")
        _string_list(item, "refs", f"items[{index}].refs")
        if "links" in item and item["links"] is not None:
            if not isinstance(item["links"], list):
                raise ValueError(f"items[{index}].links must be a list")
            for link_index, link in enumerate(item["links"]):
                field = f"items[{index}].links[{link_index}]"
                if not isinstance(link, dict):
                    raise ValueError(f"{field} must be an object")
                if "href" in link and link["href"] is not None and not isinstance(link["href"], str):
                    raise ValueError(f"{field}.href must be a string")
                if "url" in link and link["url"] is not None and not isinstance(link["url"], str):
                    raise ValueError(f"{field}.url must be a string")
                if "label" in link and link["label"] is not None and not isinstance(link["label"], str):
                    raise ValueError(f"{field}.label must be a string")
                if link.get("kind") == "draft":
                    _http_url(link.get("url") or link.get("href"), f"{field}.url")
        if "attachments" in item and item["attachments"] is not None:
            _attachments(item["attachments"], index)
        if "webLink" in item and item["webLink"] is not None and not isinstance(item["webLink"], str):
            raise ValueError(f"items[{index}].webLink must be a string")
        if item.get("area") and item["area"] not in AREAS_HINT and len(item["area"]) > 40:
            raise ValueError(f"items[{index}].area is too long")


def _calendar(events):
    seen = set()
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"calendar[{index}] must be an object")
        _loose_id(event.get("id"), 400, f"calendar[{index}].id")
        if event["id"] in seen:
            raise ValueError("duplicate calendar id")
        seen.add(event["id"])
        for key in ("subject", "start", "end"):
            _string(event, key, f"calendar[{index}].{key}")
        if "allDay" in event and not isinstance(event["allDay"], bool):
            raise ValueError(f"calendar[{index}].allDay must be true or false")
        if "stale" in event and not isinstance(event["stale"], bool):
            raise ValueError(f"calendar[{index}].stale must be true or false")
        if "join_url" in event and event["join_url"] is not None and not isinstance(event["join_url"], str):
            raise ValueError(f"calendar[{index}].join_url must be a string")


def _money(rows):
    if not isinstance(rows, list):
        raise ValueError("money must be a list")
    if len(rows) > 500:
        raise ValueError("money is too long")
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"money[{index}] must be an object")
        _loose_id(row.get("id"), 80, f"money[{index}].id")
        if row["id"] in seen:
            raise ValueError("duplicate money id")
        seen.add(row["id"])
        _string(row, "who", f"money[{index}].who")
        if row.get("direction") not in ("owed_to_me", "i_owe"):
            raise ValueError(f"money[{index}].direction must be owed_to_me or i_owe")
        if not isinstance(row.get("amount"), str) and not _number(row.get("amount")):
            raise ValueError(f"money[{index}].amount must be a string or number")
        _string(row, "currency", f"money[{index}].currency")
        if "area" in row and row["area"] is not None and not isinstance(row["area"], str):
            raise ValueError(f"money[{index}].area must be a string")
        if "due" in row and row["due"] not in (None, "") and (
            not isinstance(row["due"], str) or not DUE.fullmatch(row["due"])
        ):
            raise ValueError(f"money[{index}].due must be YYYY-MM-DD")


def _moment(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO 8601 time")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as ex:
        raise ValueError(f"{field} must be an ISO 8601 time") from ex
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a UTC offset")


def _weather_places(places):
    if not isinstance(places, list):
        raise ValueError("weather_places must be a list")
    if len(places) > 8:
        raise ValueError("weather_places is too long")
    for index, place in enumerate(places):
        if not isinstance(place, dict):
            raise ValueError(f"weather_places[{index}] must be an object")
        _string(place, "name", f"weather_places[{index}].name")
        for key, limit in (("lat", 90), ("lon", 180)):
            raw = place.get(key)
            if not _number(raw) or abs(float(raw)) > limit:
                raise ValueError(f"weather_places[{index}].{key} must be a number")


def _travel(rows):
    if not isinstance(rows, list):
        raise ValueError("travel must be a list")
    if len(rows) > 200:
        raise ValueError("travel is too long")
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"travel[{index}] must be an object")
        _loose_id(row.get("id"), 80, f"travel[{index}].id")
        if row["id"] in seen:
            raise ValueError("duplicate travel id")
        seen.add(row["id"])
        _string(row, "title", f"travel[{index}].title")
        if row.get("kind") not in TRAVEL_KINDS:
            raise ValueError(f"travel[{index}].kind must be flight, hotel, car, train, or other")
        _moment(row.get("start"), f"travel[{index}].start")
        _moment(row.get("end"), f"travel[{index}].end")
        for key in ("from", "to", "confirmation", "details", "status", "url"):
            if key in row and row[key] is not None and not isinstance(row[key], str):
                raise ValueError(f"travel[{index}].{key} must be a string")


def _upgrades(rows):
    if not isinstance(rows, list):
        raise ValueError("upgrades must be a list")
    if len(rows) > 50:
        raise ValueError("upgrades is too long")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"upgrades[{index}] must be an object")
        _string(row, "flight", f"upgrades[{index}].flight")
        _string(row, "status", f"upgrades[{index}].status")
        for key in ("date", "route", "requested_cabin", "checked_at", "url"):
            if key in row and row[key] is not None and not isinstance(row[key], str):
                raise ValueError(f"upgrades[{index}].{key} must be a string")
        if "position" in row and row["position"] is not None and not (
            isinstance(row["position"], str) or _number(row["position"])
        ):
            raise ValueError(f"upgrades[{index}].position must be a string or number")
