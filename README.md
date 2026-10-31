# Cockpit

Private board for Trevor Small. Open one URL on a phone, tablet, or laptop. Ticks, custom tasks, edits, snoozes, and handled mail are stored on the server and stay put across devices and reloads.

The page is a small Python 3 program from the standard library. Render serves it over HTTPS. A morning job on the Mac builds `data.json` and pushes it. That push never clears a tick.

`sample/data.json` is fake data for local development and tests. Do not commit a real `data.json`, `state.json`, ledger, or secret.

## Deploy on Render

1. Merge this repo to the branch Render should track.
2. In the Render dashboard choose **New > Blueprint**.
3. Select this repository. Render reads `render.yaml`.
4. When asked, set **INGEST_TOKEN** and **BASIC_AUTH_PASS_SHA256**. Leave them out of git. `BASIC_AUTH_USER` is already `trevor`. `DATA_DIR` is already `/var/data`. `BASIC_AUTH_PASS` can stay empty.
5. Apply the Blueprint. The service is named `trevor-cockpit`, on the starter plan, with a 1 GB disk mounted at `/var/data`. The health check is `/healthz`.
6. Open the service URL and sign in as `trevor` with the password whose hash you set.

The process listens on `$PORT`. If neither `BASIC_AUTH_PASS` nor `BASIC_AUTH_PASS_SHA256` is set, every route including `/healthz` returns 503 and the deploy stays unhealthy. If `INGEST_TOKEN` is missing, `POST /api/ingest` returns 503.

`BASIC_AUTH_PASS_SHA256` is the lowercase hex SHA-256 of the UTF-8 password. When it is set, that hash is the Basic auth check and `BASIC_AUTH_PASS` is optional. If both are set, the hash wins and the plaintext value is ignored. Compute it without a trailing newline:

```sh
printf '%s' 'the password' | sha256sum | cut -d' ' -f1
```

Sign out with `/logout`. The browser asks for the password again.

To put the board on the iPhone home screen, open it in Safari, then Share, then Add to Home Screen. The page includes a web app manifest and an apple touch icon. iOS sometimes asks for the password again before it can fetch the icon, because the icon is behind the same login as the rest of the board.

## What you must set

| Variable | Where it is set | Purpose |
| --- | --- | --- |
| `BASIC_AUTH_USER` | Blueprint, value `trevor` | Sign-in name |
| `BASIC_AUTH_PASS_SHA256` | Render dashboard, secret | Lowercase hex SHA-256 of the sign-in password. Preferred. When set, Basic auth checks this hash |
| `BASIC_AUTH_PASS` | Render dashboard, secret | Plaintext sign-in password. Optional when the hash is set. Ignored if the hash is also set |
| `INGEST_TOKEN` | Render dashboard, secret | Bearer token for `POST /api/ingest` and an alternate login for `GET /api/state` |
| `DATA_DIR` | Blueprint, value `/var/data` | Disk directory for `state.json`, `data.json`, and backups |
| `HOME_TZ` | Render dashboard | Fallback IANA zone for the Here clock when the device zone is unknown and the payload has no location for today. Example: `Asia/Bangkok` |
| `HOME_LABEL` | Render dashboard, optional | City name for that fallback. If empty, the city is taken from the zone (`Asia/Bangkok` becomes Bangkok) |
| `DISPLAY_TZ` | Render dashboard | Zone for calendar times, which day an event falls on, due badges, and the refreshed line. If empty, `HOME_TZ` is used, then `America/Vancouver` |

One of `BASIC_AUTH_PASS_SHA256` or `BASIC_AUTH_PASS` must be set or the app refuses to serve.

The board shows three slim clocks in one row: where you are, Vancouver, and Amsterdam. Where you are uses the device time zone and is marked Here. If the device zone is unknown, the page uses today's payload location when that place includes a time zone, then `HOME_TZ`. Vancouver is marked Home. Amsterdam is the third clock. San Diego is not shown. If where you are is already Vancouver or Amsterdam, that clock is marked Here and is not repeated. These three variables are already set on the running service. This repo does not change them.

Local run:

```sh
export BASIC_AUTH_PASS_SHA256="$(printf '%s' 'choose-a-password' | sha256sum | cut -d' ' -f1)"
export INGEST_TOKEN='choose-a-long-token'
export DATA_DIR="$PWD/var/data"
python3 server.py
```

Then open `http://127.0.0.1:8080`.

## Push data

From the Mac, after the morning build has written a `data.json`:

```sh
curl --fail-with-body -sS -X POST "https://YOUR-SERVICE.onrender.com/api/ingest" \
  -H "Authorization: Bearer $INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @data.json
```

Or `./scripts/push_data.sh data.json` with `BASE_URL` and `INGEST_TOKEN` set.

A good push responds with `{"ok": true, "items": N, "calendar": N, "restaurants": N}`. The previous file is kept at `$DATA_DIR/data.json.prev`. `state.json` is not modified.

A bad body responds with 400 and an `error` string. The current `data.json` stays as it was. Basic auth is not accepted on this route. There is only one `Authorization` header, and this route requires the bearer token.

Read ticks back for the ledger or Notion sync:

```sh
curl -sS -H "Authorization: Bearer $INGEST_TOKEN" "https://YOUR-SERVICE.onrender.com/api/state"
```

The same call accepts `-u trevor:$BASIC_AUTH_PASS`. The body is `todos`, `custom`, `emails`, `updated_at`, and `server_time`. `todos` holds done flags and per-item edits. `emails` holds handled flags. `custom` holds tasks added in the browser.

## What is stored

| File | Contents |
| --- | --- |
| `/var/data/state.json` | Ticks, custom tasks, edits, snoozes, handled mail |
| `/var/data/backups/state-YYYY-MM-DD.json` | One backup per America/Vancouver date, kept 14 days including today. The name stays on that zone when `DISPLAY_TZ` changes |
| `/var/data/data.json` | Latest pushed board |
| `/var/data/data.json.prev` | The board from the push before that |

Writes go to a temp file in the same directory, then `os.replace`, so a crash does not leave a half-written file. If `state.json` is not valid JSON, the server refuses to overwrite it.

Until the first push, the server shows `sample/data.json` and does not copy it onto the disk.

## data.json

Top-level object. Unknown keys are kept. These are required:

| Field | Shape |
| --- | --- |
| `items` | List of tasks, emails, and drafts. May be empty |
| `calendar` | List of events. May be empty |
| `restaurants` | `{city, as_of, eat: [...], delivery: [...]}` |

`restaurants.city` and `restaurants.as_of` are strings. `eat` entries need `name`, `area`, `cuisine`, `price` (string or number), `why`, and `map_url`. `delivery` entries need `name`, `app`, `why`, and `url`. Use empty strings when a value is unknown. The Restaurants section is open on first load.

Each item needs `id` and `title`. `id` matches `[A-Za-z0-9][A-Za-z0-9._:-]{0,80}`. Useful fields, matching the Mac board:

| Field | Values |
| --- | --- |
| `area` | `Echandia`, `FiFi4`, `Personal`. Older labels `Personal / Own cos` and `Household` display as Personal |
| `kind` | `task`, `email`, `draft`, `meeting`, `money` |
| `section` | `now`, `open`, `waiting`, `watch`, `owed`, `money` |
| `due` | `YYYY-MM-DD` or `""` |
| `priority` | `low` or null |
| `done` | bool. A tick in `state.json` wins over this |
| `note`, `done_note` | strings |
| `ledger_ids`, `refs` | lists of strings |
| `links` | `[{label, href, kind}]`. `kind` may be `clickup`, `outlook`, `teams`, `calendar`, `mail`, or `web` |

Email and draft rows render under Emails waiting with Handled and Undo. Other rows are tasks: Done, Undo, Edit, and Snooze. The area chips filter tasks, mail, and money.

Calendar events need `id`, `subject`, `start`, and `end`. `start` and `end` are UTC `YYYY-MM-DDTHH:MM:SSZ`, or `YYYY-MM-DD` for all-day events (end is the morning after the last day). Optional: `allDay`, `location`, `join` (Teams), `webLink` (Outlook), `note`, `stale`. The page shows today and the next 7 days in `DISPLAY_TZ`. Timed events that overlap, when one looks like a flight, are marked Flight clash. Area chips load that filter from the server. If the in-page request is refused, the chip follows its normal link, which matters on iPad Safari when Basic auth is not sent with `fetch`.

Optional:

| Field | Shape |
| --- | --- |
| `generated_at` | UTC timestamp string |
| `schema` | integer, currently 3 |
| `fx` | number, or `{usdcad or rate, as_of, source}`. Also `fx_usdcad` as a number. Shown until the browser loads the Bank of Canada Valet FXUSDCAD series |
| `money` | `[{id, who, direction, amount, currency, area, due, note, href}]`. `direction` is `owed_to_me` or `i_owe` |
| `principles` | `{musk: {title, bottleneck, steps: [[name, text], ...]}, robbins: {title, rpm_result, rpm_purpose, rpm_plan, standards, pain, pleasure, morning, evening, values, kaizen}}`. The panel is collapsed |
| `numbers` | `[{label, value, sub, source}]` |
| `notes` | list of strings, shown in the footer |
| `sources` | `{connected: [...], needed: [...]}` |
| `weather_places` | `[{name, lat, lon}]`. The server asks Open-Meteo for current conditions, today's high and low, and a 3-day outlook, in C and km/h. Cached about 30 minutes. A failed fetch shows "weather unavailable" and the rest of the page still loads. Omit the key to hide the card |
| `travel` | `[{id, kind, title, start, end, from, to, confirmation, details, status, url}]`. `kind` is `flight`, `hotel`, `car`, `train`, or `other`. `start` and `end` are ISO 8601 with an offset. The Next up card lists items whose end has not passed, soonest first, in that item's own time plus `DISPLAY_TZ` when those differ. A flight departing within 24 hours is marked check-in open. `url` is the link |
| `upgrades` | `[{flight, date, route, requested_cabin, status, position, checked_at, url}]`. A small card with status and when it was last checked. `url` is the link |

Calendar events may also include `join_url` (Teams, Zoom, or Meet). Today's card lists meetings for today and the next 24 hours in `DISPLAY_TZ`, with a Join link when `join_url` or `join` is set, and the Vancouver time in small text. Omit any of these keys and that card stays hidden. A wrong type is rejected and the previous file is kept.

The page does not show emoji or exclamation points. Those characters are stripped from visible text.

## Full ingest example

A scheduled job can post this shape. Omit a key to hide that card. `quadrant` may be `do_now`, `schedule`, `delegate`, or `delete`. If it is missing, an important item that is overdue or due within 48 hours lands in Do now, important later work in Schedule, someone else's urgent work in Delegate, and the rest in Delete or drop. Undated tasks stay collapsed inside their quadrant. Moving a task on the board stores `quadrant` in `state.json` and survives the next ingest.

`waiting_on` is mail you sent or decisions other people owe you: `who`, `what`, `since` (`YYYY-MM-DD`), `channel` (`email`, `meeting`, or `slack`), `url`, and `id`. Rows older than 5 days are highlighted.

`restaurant_locations` is a list of `{name, from, until, currency, eat, delivery}`. Optional `tz` is an IANA zone. The page shows the range that contains today. `from` and `until` are inclusive. Prices in THB or JPY also show an approximate CAD figure from the live rate. If this list is absent, the flat `restaurants` object is used. When the browser cannot read a device zone, a location for today that includes `tz` (or a top-level `location` object with `name` and `tz`) is the Here clock. Otherwise `HOME_TZ` is used.

`jarvis.on_fire` and `jarvis.decision` are the two lines at the top. `jarvis.numbers` or `numbers` supplies up to five figures. Each number may set `status` to `red`, `amber`, or `green`.

`generated_at` is the "Data as of" time. It turns amber after 12 hours and red after 24.

There is no `build_cockpit_payload` script in this repo. The job that builds `data.json` should follow this example:

```json
{
  "generated_at": "2026-10-31T18:00:00Z",
  "schema": 3,
  "fx": {"usdcad": 1.396, "as_of": "2026-10-30", "source": "Bank of Canada Valet"},
  "jarvis": {
    "on_fire": "One checklist is still open before the demo.",
    "decision": "Approve the sample budget figure today.",
    "numbers": [
      {"label": "Open now", "value": "3", "status": "red", "sub": "Due inside 48 hours"},
      {"label": "Waiting", "value": "2", "status": "amber"},
      {"label": "Scheduled", "value": "4", "status": "green"}
    ]
  },
  "items": [
    {
      "id": "S-1",
      "area": "Echandia",
      "kind": "task",
      "section": "now",
      "quadrant": "do_now",
      "important": true,
      "owner": "",
      "due": "2026-10-31",
      "priority": null,
      "title": "Confirm the demo checklist",
      "note": "",
      "done": false,
      "links": [{"label": "ClickUp", "href": "https://app.clickup.com/t/sample", "kind": "clickup"}]
    }
  ],
  "waiting_on": [
    {"id": "W-1", "who": "Demo Person", "what": "Reply on the date", "since": "2026-10-20", "channel": "email", "url": "https://outlook.office.com/mail/sample", "area": "Echandia"}
  ],
  "calendar": [
    {"id": "cal-1", "subject": "Harbor check-in", "allDay": false, "start": "2026-10-31T17:00:00Z", "end": "2026-10-31T18:00:00Z", "join_url": "https://teams.microsoft.com/l/meetup-join/sample", "webLink": "https://outlook.office.com/calendar/item/sample", "note": "", "stale": false}
  ],
  "travel": [
    {"id": "T-1", "kind": "flight", "title": "Sample flight", "start": "2026-11-03T09:30:00+07:00", "end": "2026-11-03T12:00:00+07:00", "from": "BKK", "to": "KIX", "confirmation": "ABC123", "details": "", "status": "confirmed", "url": "https://example.com/flight"}
  ],
  "upgrades": [
    {"flight": "DL 100", "date": "2026-11-03", "route": "BKK-KIX", "requested_cabin": "First", "status": "Waitlisted", "position": "4", "checked_at": "2026-10-31T12:00:00Z", "url": "https://example.com/delta"}
  ],
  "weather_places": [
    {"name": "Bangkok", "lat": 13.75, "lon": 100.5}
  ],
  "restaurant_locations": [
    {"name": "Bangkok", "from": "2026-10-01", "until": "2026-11-06", "currency": "THB", "eat": [{"name": "Sample noodle", "area": "Sukhumvit", "cuisine": "Noodles", "price": "120", "why": "Near the hotel.", "map_url": "https://example.com/noodle"}], "delivery": []},
    {"name": "Osaka", "from": "2026-11-07", "until": "2026-11-10", "currency": "JPY", "eat": [{"name": "Sample counter", "area": "Namba", "cuisine": "Grill", "price": "2000", "why": "One seat at the counter.", "map_url": "https://example.com/osaka"}], "delivery": []}
  ],
  "restaurants": {"city": "Sample Harbor", "as_of": "2026-10-30", "eat": [], "delivery": []},
  "money": [
    {"id": "M-1", "who": "Sample Labs", "direction": "owed_to_me", "amount": "100.00", "currency": "USD", "area": "Echandia", "due": "2026-11-15", "note": "Sample invoice.", "href": "https://example.com/invoice"}
  ],
  "numbers": [
    {"label": "Sample figure", "value": "1 of 1", "status": "green", "sub": "Not a real metric.", "source": "sample"}
  ],
  "principles": {
    "musk": {"title": "Musk 5-step algorithm", "bottleneck": "One checklist.", "steps": [["Question the requirement", "Does this need a meeting?"], ["Delete", "Drop the duplicate."], ["Simplify", "One list."], ["Accelerate", "Send it today."], ["Automate", "Let the morning push refresh the board."]]},
    "robbins": {"title": "Robbins RPM", "rpm_result": "Checklist closed.", "rpm_purpose": "A clean day.", "rpm_plan": ["Read it.", "Tick it."], "standards": ["One owner per item."], "pain": "An open loop.", "pleasure": "A short list.", "morning": ["What is the result?"], "evening": ["What did I finish?"], "values": ["Clarity"], "kaizen": "One less open loop."}
  },
  "notes": ["Sample data."],
  "sources": {"connected": ["Sample file"], "needed": []}
}
```

If `principles` is omitted, the page uses a built-in Musk 5-step list and a short Robbins block. The Delete candidates link jumps to the Delete or drop quadrant.

A minimal restaurants object, if the morning file does not have one yet:

```json
"restaurants": {"city": "", "as_of": "", "eat": [], "delivery": []}
```

## Other routes

| Method | Path | Auth | Effect |
| --- | --- | --- | --- |
| GET | `/healthz` | none | `{"ok": true}` when a password or password hash is set and the data directory is writable |
| GET | `/` | Basic | The board. Works with JavaScript off |
| GET | `/api/fragment?area=` | Basic | HTML fragment used by the page script |
| GET | `/api/data` | Basic | Current data plus state |
| GET | `/api/state` | Bearer or Basic | Ticks for the morning sync |
| POST | `/api/ingest` | Bearer | Replace `data.json` |
| POST | `/api/todo/toggle` | Basic | `{id, done}` |
| POST | `/api/todo/add` | Basic | `{title, area, due, notes}` |
| POST | `/api/todo/edit` | Basic | `{id, title, area, due, notes}` |
| POST | `/api/email/handled` | Basic | `{id, handled}` |
| POST | `/api/snooze` | Basic | `{id, kind: "todo" or "email", until: "YYYY-MM-DD"}`. `clear: true` removes a snooze |
| POST | `/form/...` | Basic | Same actions as a normal form, then a redirect. Used when JavaScript is off |
| GET | `/logout` | none | 401 so the browser drops the password prompt |

Browser posts must come from the same host. `curl` sends no `Origin` header and is allowed.

## Tests

```sh
python3 -m unittest discover -s tests -v
python3 verify_ui.py
```

`verify_ui.py` starts a temporary server on the sample file, checks auth, the main sections, and that a tick is still on the page after a reload.
