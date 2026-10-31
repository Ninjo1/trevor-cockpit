# Cockpit

Private board for Trevor Small. Open one URL on a phone, tablet, or laptop. Ticks, custom tasks, edits, snoozes, and handled mail are stored on the server and stay put across devices and reloads.

The page is a small Python 3 program from the standard library. Render serves it over HTTPS. A morning job on the Mac builds `data.json` and pushes it. That push never clears a tick.

`sample/data.json` is fake data for local development and tests. Do not commit a real `data.json`, `state.json`, ledger, or secret.

## Deploy on Render

1. Merge this repo to the branch Render should track.
2. In the Render dashboard choose **New > Blueprint**.
3. Select this repository. Render reads `render.yaml`.
4. When asked, set **BASIC_AUTH_PASS** and **INGEST_TOKEN**. Leave them out of git. `BASIC_AUTH_USER` is already `trevor`. `DATA_DIR` is already `/var/data`.
5. Apply the Blueprint. The service is named `trevor-cockpit`, on the starter plan, with a 1 GB disk mounted at `/var/data`. The health check is `/healthz`.
6. Open the service URL and sign in as `trevor` with the password you set.

The process listens on `$PORT`. If `BASIC_AUTH_PASS` is missing, every route including `/healthz` returns 503 and the deploy stays unhealthy. If `INGEST_TOKEN` is missing, `POST /api/ingest` returns 503.

Sign out with `/logout`. The browser asks for the password again.

To put the board on the iPhone home screen, open it in Safari, then Share, then Add to Home Screen. The page includes a web app manifest and an apple touch icon. iOS sometimes asks for the password again before it can fetch the icon, because the icon is behind the same login as the rest of the board.

## What you must set

| Variable | Where it is set | Purpose |
| --- | --- | --- |
| `BASIC_AUTH_USER` | Blueprint, value `trevor` | Sign-in name |
| `BASIC_AUTH_PASS` | Render dashboard, secret | Sign-in password. Required or the app refuses to serve |
| `INGEST_TOKEN` | Render dashboard, secret | Bearer token for `POST /api/ingest` and an alternate login for `GET /api/state` |
| `DATA_DIR` | Blueprint, value `/var/data` | Disk directory for `state.json`, `data.json`, and backups |

Local run:

```sh
export BASIC_AUTH_PASS='choose-a-password'
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
| `/var/data/backups/state-YYYY-MM-DD.json` | One backup per Pacific date, kept 14 days including today |
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

Calendar events need `id`, `subject`, `start`, and `end`. `start` and `end` are UTC `YYYY-MM-DDTHH:MM:SSZ`, or `YYYY-MM-DD` for all-day events (end is the morning after the last day). Optional: `allDay`, `location`, `join` (Teams), `webLink` (Outlook), `note`, `stale`. The page shows today and the next 7 days in Pacific time. Timed events that overlap, when one looks like a flight, are marked Flight clash.

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

The page does not show emoji or exclamation points. Those characters are stripped from visible text.

A minimal restaurants object, if the morning file does not have one yet:

```json
"restaurants": {"city": "", "as_of": "", "eat": [], "delivery": []}
```

## Other routes

| Method | Path | Auth | Effect |
| --- | --- | --- | --- |
| GET | `/healthz` | none | `{"ok": true}` when the password is set and the data directory is writable |
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
