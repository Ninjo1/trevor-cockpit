#!/usr/bin/env python3
"""Cockpit web server. Standard library only. Binds to $PORT.

Auth: HTTP Basic on every path except /healthz. Fails closed when neither
BASIC_AUTH_PASS nor BASIC_AUTH_PASS_SHA256 is set. When the hash is set,
it is the password check (SHA-256 hex of the UTF-8 password) and
BASIC_AUTH_PASS is optional. POST /api/ingest uses Authorization: Bearer
$INGEST_TOKEN. GET /api/state accepts that bearer or the same Basic check.

Ticks live in $DATA_DIR/state.json and are never written by ingest.
"""
import datetime as dt
import json
import os
import re
from urllib.parse import parse_qs, quote, urlsplit

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fxrates
import render
import voice
import weather as weather_mod
from actions import AREAS, apply, brief_log
from auth import basic_ok, bearer_ok, origin_allowed
from icons import MANIFEST, icon_png
from store import Config, StateError, data_dir_writable, load_data, load_state, save_data, save_state
from validate import validate_data

ROOT = os.path.dirname(os.path.abspath(__file__))
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; connect-src 'self' https://www.bankofcanada.ca; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
)
JSON_LIMIT = 100_000
INGEST_LIMIT = 5_000_000
FORM_LIMIT = 100_000


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_handler(config):
    import threading

    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        cfg = config
        state_lock = lock
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _path(self):
            return urlsplit(self.path).path or "/"

        def _area(self):
            query = parse_qs(urlsplit(self.path).query)
            area = (query.get("area") or [""])[0]
            return area if area in AREAS else "All"

        def _send(self, code, body, content_type="text/html; charset=utf-8", extra=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Content-Security-Policy", CSP)
            for key, value in extra or ():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)

        def _json(self, code, obj, extra=None):
            self._send(code, json.dumps(obj), "application/json; charset=utf-8", extra)

        def _read(self, limit):
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                return b"", None
            try:
                length = int(raw_length)
            except ValueError:
                return None, "bad length"
            if length < 0 or length > limit:
                return None, "too large"
            return self.rfile.read(length), None

        def _unconfigured(self):
            self._json(503, {"ok": False, "error": "BASIC_AUTH_PASS is unset"})

        def _unauthorized(self, scheme="Basic"):
            if scheme == "Bearer":
                extra = [("WWW-Authenticate", 'Bearer realm="Cockpit"')]
                body = "Authentication required"
            else:
                extra = [("WWW-Authenticate", 'Basic realm="Cockpit", charset="UTF-8"')]
                body = "Authentication required"
            self._send(401, body, "text/plain; charset=utf-8", extra)

        def _basic_ok(self):
            return basic_ok(
                self.headers.get("Authorization"),
                self.cfg.user,
                self.cfg.password,
                self.cfg.password_sha256,
            )

        def _basic(self):
            if self._basic_ok():
                return True
            self._unauthorized("Basic")
            return False

        def _same_origin(self):
            if origin_allowed(
                self.headers.get("Origin"),
                self.headers.get("X-Forwarded-Host") or self.headers.get("Host"),
                self.headers.get("X-Forwarded-Proto"),
                self.headers.get("Sec-Fetch-Site"),
            ):
                return True
            self._json(403, {"ok": False, "error": "origin is not allowed"})
            return False

        def _health(self):
            if not self.cfg.configured():
                self._unconfigured()
                return
            if not data_dir_writable(self.cfg):
                self._json(503, {"ok": False, "error": "DATA_DIR is not writable"})
                return
            self._json(200, {"ok": True})

        def _logout(self):
            page = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Signed out</title>
<style>body{margin:0;background:#07090d;color:#e8eef6;font:18px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:36rem;margin:10vh auto;padding:24px}a{color:#5fe0ff}</style></head>
<body><main><h1>Signed out</h1>
<p>The cockpit is locked. Open it again and enter the password.</p>
<p><a href="/">Open the cockpit</a></p></main></body></html>"""
            self._send(
                401,
                page,
                extra=[("WWW-Authenticate", 'Basic realm="Cockpit", charset="UTF-8"')],
            )

        def _rates(self, data):
            fallback = None
            fx = data.get("fx") if isinstance(data, dict) else None
            if isinstance(fx, dict):
                fallback = fx.get("usdcad", fx.get("rate"))
            elif isinstance(fx, (int, float)) and not isinstance(fx, bool):
                fallback = fx
            elif isinstance(data, dict) and isinstance(data.get("fx_usdcad"), (int, float)):
                fallback = data.get("fx_usdcad")
            return fxrates.get_rates(fallback)

        def _forecasts(self, data):
            places = data.get("weather_places") if isinstance(data, dict) else None
            if not isinstance(places, list) or not places:
                return None
            return weather_mod.get_forecasts(places, data_dir=self.cfg.data_dir)

        def _page(self):
            with open(os.path.join(ROOT, "app.html"), encoding="utf-8") as handle:
                template = handle.read()
            try:
                with self.state_lock:
                    state = load_state(self.cfg)
                    data = load_data(self.cfg)
                forecasts = self._forecasts(data)
                rates = self._rates(data)
                page = render.fill(template, data, state, area=self._area(), weather=forecasts, rates=rates)
            except StateError as ex:
                self._send(500, f"Could not read ticks: {ex}", "text/plain; charset=utf-8")
                return
            except Exception:
                self._send(500, "Could not build the page.", "text/plain; charset=utf-8")
                return
            self._send(200, page)

        def _state(self):
            header = self.headers.get("Authorization")
            allowed = bearer_ok(header, self.cfg.ingest_token) or self._basic_ok()
            if not allowed:
                self._unauthorized("Bearer" if (header or "").lower().startswith("bearer ") else "Basic")
                return
            try:
                with self.state_lock:
                    state = load_state(self.cfg)
            except StateError as ex:
                self._json(500, {"ok": False, "error": str(ex)})
                return
            self._json(
                200,
                {
                    "ok": True,
                    "todos": state.get("todos") or {},
                    "custom": state.get("custom") or [],
                    "emails": state.get("emails") or {},
                    "updated_at": state.get("updated_at"),
                    "server_time": now_iso(),
                },
            )

        def _data(self):
            try:
                with self.state_lock:
                    payload = {"data": load_data(self.cfg), "state": load_state(self.cfg), "server_time": now_iso()}
            except StateError as ex:
                self._json(500, {"ok": False, "error": str(ex)})
                return
            self._json(200, payload)

        def _fragment(self):
            try:
                with self.state_lock:
                    data = load_data(self.cfg)
                    state = load_state(self.cfg)
                forecasts = self._forecasts(data)
                rates = self._rates(data)
                main, counts, jarvis = render.render_main(data, state, self._area(), weather=forecasts, rates=rates)
            except StateError as ex:
                self._json(500, {"ok": False, "error": str(ex)})
                return
            self._json(200, {"main": main, "counts": counts, "jarvis": jarvis})

        def _ingest(self):
            if not self.cfg.ingest_token:
                self._json(503, {"ok": False, "error": "INGEST_TOKEN is unset"})
                return
            if not bearer_ok(self.headers.get("Authorization"), self.cfg.ingest_token):
                self._unauthorized("Bearer")
                return
            body, error = self._read(INGEST_LIMIT)
            if error == "too large":
                self._json(413, {"ok": False, "error": "body is too large"})
                return
            if error:
                self._json(400, {"ok": False, "error": "body length is not valid"})
                return
            try:
                obj = json.loads(body.decode("utf-8") or "null")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"ok": False, "error": "body is not JSON"})
                return
            try:
                validate_data(obj)
            except ValueError as ex:
                self._json(400, {"ok": False, "error": str(ex)})
                return
            try:
                with self.state_lock:
                    save_data(self.cfg, obj)
            except OSError:
                self._json(500, {"ok": False, "error": "could not store data"})
                return
            restaurants = obj.get("restaurants") or {}
            self._json(
                200,
                {
                    "ok": True,
                    "items": len(obj.get("items") or []),
                    "calendar": len(obj.get("calendar") or []),
                    "restaurants": len(restaurants.get("eat") or []) + len(restaurants.get("delivery") or []),
                },
            )

        def _mutate(self, op, body):
            try:
                with self.state_lock:
                    state = load_state(self.cfg)
                    result = apply(state, op, body, now_iso())
                    state["log"] = list(state.get("log") or [])
                    state["log"].append(brief_log(op, body, now_iso()))
                    save_state(self.cfg, state)
            except KeyError:
                return 404, "unknown op"
            except ValueError as ex:
                return 400, str(ex)
            except StateError as ex:
                return 500, str(ex)
            except OSError:
                return 500, "could not store ticks"
            return 200, result

        def _api_post(self, op):
            if not self._same_origin() or not self._basic():
                return
            body, error = self._read(JSON_LIMIT)
            if error == "too large":
                self._json(413, {"ok": False, "error": "body is too large"})
                return
            if error:
                self._json(400, {"ok": False, "error": "body length is not valid"})
                return
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"ok": False, "error": "body is not JSON"})
                return
            if op == "question/toggle":
                payload = self._question_payload(payload)
            if op == "voice/draft_request":
                self._voice_queue(payload)
                return
            if op == "voice/ask":
                self._voice_ask(payload)
                return
            status, result = self._mutate(op, payload)
            if status != 200:
                self._json(status, {"ok": False, "error": result})
                return
            self._json(200, {"ok": True, "result": result})

        def _voice_guard(self):
            token = self.cfg.voice_pickup_token
            if not token:
                self._json(503, {"ok": False, "error": "VOICE_PICKUP_TOKEN is unset"})
                return False
            if not bearer_ok(self.headers.get("Authorization"), token):
                self._unauthorized("Bearer")
                return False
            return True

        def _voice_list(self):
            if not self._voice_guard():
                return
            status = (parse_qs(urlsplit(self.path).query).get("status") or ["pending"])[0]
            if status not in ("pending", "done"):
                self._json(400, {"ok": False, "error": "status must be pending or done"})
                return
            try:
                with self.state_lock:
                    state = load_state(self.cfg)
            except StateError as ex:
                self._json(500, {"ok": False, "error": str(ex)})
                return
            self._json(200, {"requests": voice.list_drafts(state, status)})

        def _voice_done(self, request_id):
            if not self._voice_guard():
                return
            body, error = self._read(JSON_LIMIT)
            if error:
                self._json(413 if error == "too large" else 400, {"ok": False, "error": "body is not valid"})
                return
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"ok": False, "error": "body is not JSON"})
                return
            if not isinstance(payload, dict):
                payload = {}
            try:
                with self.state_lock:
                    state = load_state(self.cfg)
                    record = voice.finish_draft(state, request_id, payload.get("draft_weblink"), now_iso())
                    save_state(self.cfg, state)
            except KeyError:
                self._json(404, {"ok": False, "error": "request was not found"})
                return
            except ValueError as ex:
                self._json(400, {"ok": False, "error": str(ex)})
                return
            except StateError as ex:
                self._json(500, {"ok": False, "error": str(ex)})
                return
            self._json(200, {"ok": True, "result": record})

        def _voice_queue(self, payload):
            if not isinstance(payload, dict):
                self._json(400, {"ok": False, "error": "body must be an object"})
                return
            try:
                with self.state_lock:
                    state = load_state(self.cfg)
                    data = load_data(self.cfg)
                    record = voice.queue_draft(state, data, payload.get("item_id"), payload.get("text") or "", now_iso())
                    save_state(self.cfg, state)
            except KeyError:
                self._json(404, {"ok": False, "error": "item was not found"})
                return
            except ValueError as ex:
                self._json(400, {"ok": False, "error": str(ex)})
                return
            except StateError as ex:
                self._json(500, {"ok": False, "error": str(ex)})
                return
            self._json(200, {"ok": True, "result": record})

        def _voice_ask(self, payload):
            if not isinstance(payload, dict):
                self._json(400, {"ok": False, "error": "body must be an object"})
                return
            try:
                with self.state_lock:
                    state = load_state(self.cfg)
                    data = load_data(self.cfg)
            except StateError as ex:
                self._json(500, {"ok": False, "error": str(ex)})
                return
            zone, _label = render.resolve_display()
            moment = dt.datetime.now(dt.timezone.utc)
            facts = voice.board_facts(data, state, moment.astimezone(zone).date(), moment, zone)
            self._json(200, {"ok": True, "answer": voice.answer(payload.get("text") or "", facts)})

        def _question_payload(self, payload):
            """Stamp the Robbins day from the current-location zone, not the browser."""
            body = dict(payload) if isinstance(payload, dict) else {}
            try:
                data = load_data(self.cfg)
            except Exception:
                data = {}
            day, _zone = render.ritual_day(None, data, dt.datetime.now(dt.timezone.utc))
            body["day"] = day
            return body

        def _form(self, op):
            if not self._same_origin() or not self._basic():
                return
            body, error = self._read(FORM_LIMIT)
            if error:
                self._send(413 if error == "too large" else 400, "Could not read the form.", "text/plain; charset=utf-8")
                return
            fields = {key: values[0] for key, values in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}
            try:
                if op == "todo/toggle":
                    payload = {"id": fields["id"], "done": fields.get("done") == "1"}
                elif op == "email/handled":
                    payload = {"id": fields["id"], "handled": fields.get("handled") == "1"}
                elif op == "todo/add":
                    payload = {
                        "title": fields.get("title", ""),
                        "area": fields.get("area"),
                        "due": fields.get("due", ""),
                        "notes": fields.get("notes", ""),
                    }
                elif op == "todo/edit":
                    payload = {
                        "id": fields["id"],
                        "title": fields.get("title", ""),
                        "area": fields.get("area"),
                        "due": fields.get("due", ""),
                        "notes": fields.get("notes", ""),
                    }
                elif op == "todo/quadrant":
                    payload = {"id": fields["id"], "quadrant": fields.get("quadrant")}
                elif op == "snooze":
                    payload = {
                        "id": fields["id"],
                        "kind": fields.get("kind") or "todo",
                        "until": fields.get("until", ""),
                        "clear": fields.get("clear") == "1",
                    }
                elif op == "question/toggle":
                    payload = self._question_payload({"id": fields["id"], "done": fields.get("done") == "1"})
                else:
                    self._send(404, "Not found", "text/plain; charset=utf-8")
                    return
            except KeyError:
                self._send(400, "The form is missing a field.", "text/plain; charset=utf-8")
                return
            status, result = self._mutate(op, payload)
            if status != 200:
                self._send(status, f"Could not save. {result}", "text/plain; charset=utf-8")
                return
            area_q = fields.get("area_q") or ""
            location = "/" + (f"?area={quote(area_q)}" if area_q in AREAS else "")
            if op == "question/toggle":
                location += "#principles"
            elif fields.get("id"):
                location += "#" + render.dom_id("i", fields["id"])
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_GET(self):
            self._route(write=False)

        def do_HEAD(self):
            self._route(write=False)

        def do_POST(self):
            self._route(write=True)

        def do_PUT(self):
            self._send(405, "Method not allowed", "text/plain; charset=utf-8")

        do_DELETE = do_PUT
        do_PATCH = do_PUT

        def _route(self, write):
            if not self.cfg.configured():
                self._unconfigured()
                return
            path = self._path()
            if not write and path == "/healthz":
                self._health()
                return
            if not write and path == "/logout":
                self._logout()
                return
            if write and path == "/api/ingest":
                self._ingest()
                return
            if not write and path == "/api/state":
                self._state()
                return
            if not write and path == "/api/voice/draft_requests":
                self._voice_list()
                return
            done_match = re.fullmatch(r"/api/voice/draft_requests/([^/]+)/done", path)
            if write and done_match:
                self._voice_done(done_match.group(1))
                return
            if path.startswith("/api/") or path.startswith("/form/") or path in (
                "/",
                "/index.html",
                "/manifest.webmanifest",
                "/apple-touch-icon.png",
                "/icon-192.png",
                "/icon-512.png",
                "/favicon.ico",
            ):
                if not self._basic():
                    return
            else:
                if not self._basic():
                    return
                self._send(404, "Not found", "text/plain; charset=utf-8")
                return
            if write:
                if path.startswith("/api/"):
                    self._api_post(path[len("/api/") :])
                    return
                if path.startswith("/form/"):
                    self._form(path[len("/form/") :])
                    return
                self._send(404, "Not found", "text/plain; charset=utf-8")
                return
            if path in ("/", "/index.html"):
                self._page()
            elif path == "/api/data":
                self._data()
            elif path == "/api/fragment":
                self._fragment()
            elif path == "/manifest.webmanifest":
                self._send(200, MANIFEST, "application/manifest+json; charset=utf-8")
            elif path == "/apple-touch-icon.png":
                self._send(200, icon_png(180), "image/png")
            elif path == "/icon-192.png":
                self._send(200, icon_png(192), "image/png")
            elif path == "/icon-512.png":
                self._send(200, icon_png(512), "image/png")
            elif path == "/favicon.ico":
                self._send(200, icon_png(32), "image/png")
            else:
                self._send(404, "Not found", "text/plain; charset=utf-8")

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    config = Config.from_env(ROOT)
    host = os.environ.get("BIND", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    try:
        os.makedirs(config.data_dir, exist_ok=True)
    except OSError as ex:
        print(f"data dir not ready: {ex}", flush=True)
    httpd = Server((host, port), make_handler(config))
    print(f"listening on {host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
