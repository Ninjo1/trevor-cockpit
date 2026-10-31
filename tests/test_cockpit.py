"""Auth, ingest, tick survival, and atomic writes for the cockpit server."""
import base64
import datetime as dt
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
import sys

sys.path.insert(0, ROOT)

import render
import store
from actions import apply
from auth import basic_ok, bearer_ok, const_eq
from server import Server, make_handler
from store import Config, atomic_write, fresh_state, save_state
from validate import validate_data

PT = ZoneInfo("America/Vancouver")
SAMPLE = os.path.join(ROOT, "sample", "data.json")
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF]")


def visible(page):
    text = re.sub(r"<script[\s\S]*?</script>", " ", page, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return "Basic " + token


class HttpCase(unittest.TestCase):
    password = "test-pass"
    token = "test-token"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            data_dir=self.tmp.name,
            password=self.password,
            user="trevor",
            ingest_token=self.token,
            sample_path=SAMPLE,
        )
        self.httpd = Server(("127.0.0.1", 0), make_handler(self.cfg))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def request(self, path, method="GET", data=None, headers=None, auth=True, follow=True):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if auth is True:
            req.add_header("Authorization", basic("trevor", self.password))
        elif isinstance(auth, str):
            req.add_header("Authorization", auth)
        for key, value in (headers or {}).items():
            req.add_header(key, value)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(urllib.request.HTTPHandler if follow else NoRedirect)
        try:
            with opener.open(req, timeout=5) as res:
                return res.status, {key.lower(): value for key, value in res.headers.items()}, res.read()
        except urllib.error.HTTPError as ex:
            return ex.code, {key.lower(): value for key, value in ex.headers.items()}, ex.read()

    def sample(self):
        return json.load(open(SAMPLE, encoding="utf-8"))


class AuthTests(HttpCase):
    def test_constant_time_compare_ignores_length(self):
        self.assertTrue(const_eq(b"abc", b"abc"))
        self.assertFalse(const_eq(b"short", b"a much longer password value"))
        self.assertFalse(basic_ok("Basic " + base64.b64encode(b"trevor:nope").decode(), "trevor", "test-pass"))
        self.assertFalse(basic_ok(None, "trevor", ""))
        self.assertFalse(bearer_ok("Bearer something", ""))

    def test_fail_closed_when_password_unset(self):
        raw = tempfile.TemporaryDirectory()
        cfg = Config(data_dir=raw.name, password="", user="trevor", ingest_token="tok", sample_path=SAMPLE)
        httpd = Server(("127.0.0.1", 0), make_handler(cfg))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            for path in ("/", "/healthz", "/api/state", "/api/ingest"):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET" if path != "/api/ingest" else "POST")
                if path == "/api/ingest":
                    req.data = b"{}"
                    req.add_header("Authorization", "Bearer tok")
                try:
                    with urllib.request.urlopen(req, timeout=5) as res:
                        status, body = res.status, res.read()
                except urllib.error.HTTPError as ex:
                    status, body = ex.code, ex.read()
                self.assertEqual(status, 503, path)
                self.assertIn(b"BASIC_AUTH_PASS is unset", body)
                self.assertNotIn(b"North Pier", body)
        finally:
            httpd.shutdown()
            httpd.server_close()
            raw.cleanup()

    def test_healthz_is_open_only_when_configured(self):
        status, _headers, body = self.request("/healthz", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_pages_require_basic_auth(self):
        status, headers, body = self.request("/", auth=False)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("www-authenticate", ""))
        self.assertNotIn(b"North Pier", body)
        self.assertNotIn(b"Sample:", body)
        status, _headers, body = self.request("/", auth=basic("trevor", "wrong"))
        self.assertEqual(status, 401)
        self.assertNotIn(b"North Pier", body)
        status, _headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"North Pier Cafe", body)

    def test_logout_challenges_again(self):
        status, headers, body = self.request("/logout")
        self.assertEqual(status, 401)
        self.assertIn(b"Signed out", body)
        self.assertIn("Basic", headers.get("www-authenticate", ""))


class IngestTests(HttpCase):
    def _post(self, payload, auth):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return self.request(
            "/api/ingest",
            method="POST",
            data=data,
            auth=auth,
            headers={"Content-Type": "application/json"},
        )

    def test_ingest_auth_and_validation(self):
        good = self.sample()
        status, _headers, _body = self._post(good, auth=False)
        self.assertEqual(status, 401)
        status, _headers, _body = self._post(good, auth=basic("trevor", self.password))
        self.assertEqual(status, 401)
        status, _headers, _body = self._post(good, auth="Bearer wrong-token")
        self.assertEqual(status, 401)
        status, _headers, body = self._post(good, auth="Bearer " + self.token)
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["ok"])
        bad = self.sample()
        bad["restaurants"] = {"city": "X"}
        status, _headers, body = self._post(bad, auth="Bearer " + self.token)
        self.assertEqual(status, 400, body)
        stored = json.load(open(os.path.join(self.tmp.name, "data.json"), encoding="utf-8"))
        self.assertEqual(stored["restaurants"]["city"], "Sample Harbor")
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "data.json.prev")))
        good["notes"] = ["Second push, still sample."]
        status, _headers, body = self._post(good, auth="Bearer " + self.token)
        self.assertEqual(status, 200, body)
        previous = json.load(open(os.path.join(self.tmp.name, "data.json.prev"), encoding="utf-8"))
        self.assertEqual(previous["restaurants"]["city"], "Sample Harbor")
        self.assertNotIn("Second push", json.dumps(previous))

    def test_ingest_token_unset_fails_closed(self):
        self.cfg.ingest_token = ""
        status, _headers, body = self._post(self.sample(), auth="Bearer test-token")
        self.assertEqual(status, 503)
        self.assertIn(b"INGEST_TOKEN is unset", body)

    def test_sample_file_validates(self):
        validate_data(self.sample())


class TickTests(HttpCase):
    def test_tick_survives_ingest(self):
        status, _headers, body = self.request(
            "/api/todo/toggle",
            method="POST",
            data=json.dumps({"id": "S-now-1", "done": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        status, _headers, page = self.request("/")
        self.assertIn(b"it done", page)
        pushed = self.sample()
        pushed["items"][0]["done"] = False
        pushed["items"][0]["title"] = "Sample: renamed by the data push"
        status, _headers, body = self.request(
            "/api/ingest",
            method="POST",
            data=json.dumps(pushed).encode(),
            auth="Bearer " + self.token,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        state = json.load(open(os.path.join(self.tmp.name, "state.json"), encoding="utf-8"))
        self.assertTrue(state["todos"]["S-now-1"]["done"])
        status, _headers, page = self.request("/")
        text = page.decode()
        self.assertIn("it done", text)
        self.assertIn("Sample: renamed by the data push", text)
        status, _headers, body = self.request("/api/state", auth="Bearer " + self.token)
        self.assertEqual(status, 200, body)
        remote = json.loads(body)
        self.assertTrue(remote["todos"]["S-now-1"]["done"])
        self.assertNotIn("log", remote)

    def test_edit_and_snooze_round_trip(self):
        status, _headers, body = self.request(
            "/api/todo/add",
            method="POST",
            data=json.dumps({"title": "Ship it!", "area": "Personal", "due": "2026-11-01", "notes": "Hello!"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        item_id = json.loads(body)["result"]["id"]
        status, _headers, body = self.request(
            "/api/todo/edit",
            method="POST",
            data=json.dumps({"id": item_id, "title": "Ship the sample", "notes": "Updated note"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        status, _headers, body = self.request(
            "/api/snooze",
            method="POST",
            data=json.dumps({"id": "S-open-1", "kind": "todo", "until": "2099-01-01"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        status, _headers, page = self.request("/")
        text = visible(page.decode())
        self.assertIn("Ship the sample", text)
        self.assertNotIn("!", text)
        self.assertIsNone(EMOJI.search(text))
        self.assertIn("Snoozed", page.decode())

    def test_form_post_marks_done_without_javascript(self):
        status, headers, body = self.request(
            "/form/todo/toggle",
            method="POST",
            data=b"id=S-now-1&done=1&area=",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow=False,
        )
        self.assertEqual(status, 303, body)
        self.assertTrue(headers.get("location", "").startswith("/"))
        status, _headers, page = self.request("/")
        self.assertIn(b"it done", page)

    def test_cross_origin_post_is_rejected(self):
        status, _headers, body = self.request(
            "/api/todo/toggle",
            method="POST",
            data=json.dumps({"id": "S-now-1", "done": True}).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403, body)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "state.json")))


class AtomicTests(unittest.TestCase):
    def test_failed_write_keeps_the_original_and_no_temp_file(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "state.json")
            store.atomic_write_json(path, {"ok": True})

            def boom(handle):
                handle.write("{")
                raise RuntimeError("disk")

            with self.assertRaises(RuntimeError):
                atomic_write(path, boom)
            self.assertEqual(json.load(open(path, encoding="utf-8")), {"ok": True})
            self.assertFalse(any(name.startswith(".tmp-") for name in os.listdir(tmp.name)))
        finally:
            tmp.cleanup()

    def test_daily_backups_cover_14_days(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            cfg = Config(data_dir=tmp.name, password="x", sample_path=None)
            backup = os.path.join(tmp.name, "backups")
            os.makedirs(backup)
            start = dt.date(2026, 10, 1)
            for offset in range(20):
                day = start + dt.timedelta(days=offset)
                with open(os.path.join(backup, f"state-{day.isoformat()}.json"), "w", encoding="utf-8") as handle:
                    handle.write("{}\n")
            moment = dt.datetime(2026, 10, 30, 9, 0, tzinfo=PT)
            save_state(cfg, fresh_state(), now=moment)
            names = set(os.listdir(backup))
            self.assertIn("state-2026-10-30.json", names)
            self.assertNotIn("state-2026-10-16.json", names)
            self.assertIn("state-2026-10-17.json", names)
            kept = []
            for name in names:
                kept.append(dt.date.fromisoformat(name[len("state-") : -len(".json")]))
            cutoff = dt.date(2026, 10, 30) - dt.timedelta(days=13)
            self.assertTrue(all(day >= cutoff for day in kept))
            saved = json.load(open(os.path.join(backup, "state-2026-10-30.json"), encoding="utf-8"))
            self.assertIn("todos", saved)
            self.assertFalse(any(name.startswith(".tmp-") for name in os.listdir(tmp.name)))
        finally:
            tmp.cleanup()

    def test_corrupt_state_is_not_overwritten(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            cfg = Config(data_dir=tmp.name, password="x", sample_path=SAMPLE)
            path = os.path.join(tmp.name, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{")
            state = {"todos": {}, "custom": [], "emails": {}, "log": []}
            with self.assertRaises(store.StateError):
                apply_in_place = store.load_state(cfg)
                apply(apply_in_place, "todo/toggle", {"id": "S-now-1", "done": True}, "2026-10-30T00:00:00Z")
            self.assertEqual(open(path, encoding="utf-8").read(), "{")
        finally:
            tmp.cleanup()


class PageTests(unittest.TestCase):
    def test_render_has_sections_and_flight_clash(self):
        data = json.load(open(SAMPLE, encoding="utf-8"))
        html, _counts = render.render_main(data, fresh_state(), today=dt.date(2026, 10, 30))
        for needle in (
            "North Pier Cafe",
            "Harbor Bowl",
            "Emails waiting",
            "Money owed",
            "Today and the next 7 days",
            "Flight clash",
            "Principles",
            "Join Teams",
            "ClickUp sample",
            "Sample Labs",
            "You owe",
        ):
            self.assertIn(needle, html, needle)
        text = visible(html)
        self.assertNotIn("!", text)
        self.assertIsNone(EMOJI.search(text))
        flight, overlap = render.classify_clashes(data["calendar"])
        self.assertIn("cal-flight", flight)
        self.assertIn("cal-harbor", flight)
        self.assertNotIn("cal-tomorrow", flight)
        self.assertNotIn("cal-tomorrow", overlap)

    def test_template_has_home_screen_hooks(self):
        template = open(os.path.join(ROOT, "app.html"), encoding="utf-8").read()
        self.assertIn('rel="manifest"', template)
        self.assertIn("apple-touch-icon", template)
        self.assertIn("apple-mobile-web-app-capable", template)
        self.assertIn("bankofcanada.ca", template)
        clocks = render.clocks_html(dt.datetime(2026, 10, 30, 18, 0, tzinfo=dt.timezone.utc))
        for city in ("Vancouver", "Chicago", "New York", "Netherlands", "Sweden", "Seoul", "Bangkok", "Tokyo"):
            self.assertIn(city, clocks)

    def test_repo_has_no_personal_mailboxes(self):
        banned = ("@trevorsmall.com", "@echandia.com", "@fifi4solutions.com", "NGROK_AUTHTOKEN", "store-f2dd5dab")
        for dirpath, dirs, files in os.walk(ROOT):
            dirs[:] = [name for name in dirs if name not in {".git", "__pycache__", "var"}]
            for name in files:
                if not name.endswith((".py", ".html", ".json", ".md", ".yml", ".yaml", ".sh", ".txt")):
                    continue
                path = os.path.join(dirpath, name)
                if os.path.abspath(path) == os.path.abspath(__file__):
                    continue
                text = open(path, encoding="utf-8", errors="replace").read()
                for needle in banned:
                    self.assertNotIn(needle, text, path)


if __name__ == "__main__":
    unittest.main()
