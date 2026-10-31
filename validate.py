"""Shape checks for a cockpit data.json push.

Unknown keys are kept. A bad type is rejected so the previous file stays in
place. restaurants is required.
"""
import re

ITEM_KINDS = {"task", "email", "draft", "meeting", "money"}
SECTIONS = {"now", "open", "waiting", "watch", "owed", "money"}
AREAS_HINT = {"Echandia", "FiFi4", "Personal", "Personal / Own cos", "Household"}
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}\Z")
DUE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
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
    return obj


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
                if not isinstance(link, dict):
                    raise ValueError(f"items[{index}].links[{link_index}] must be an object")
                if "href" in link and not isinstance(link["href"], str):
                    raise ValueError(f"items[{index}].links[{link_index}].href must be a string")
                if "label" in link and not isinstance(link["label"], str):
                    raise ValueError(f"items[{index}].links[{link_index}].label must be a string")
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
