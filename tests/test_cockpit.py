"""Auth, ingest, tick survival, and atomic writes for the cockpit server."""
import base64
import datetime as dt
import hashlib
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
import voice
from actions import apply
from auth import basic_ok, bearer_ok, const_eq, origin_allowed
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

    def test_sha256_password_check_is_constant_time_and_hash_wins(self):
        digest = hashlib.sha256("hash-secret".encode("utf-8")).hexdigest()
        good = "Basic " + base64.b64encode(b"trevor:hash-secret").decode()
        plain = "Basic " + base64.b64encode(b"trevor:plain-secret").decode()
        wrong_user = "Basic " + base64.b64encode(b"other:hash-secret").decode()
        self.assertTrue(basic_ok(good, "trevor", "", digest))
        self.assertTrue(basic_ok(good, "trevor", "plain-secret", digest.upper()))
        self.assertFalse(basic_ok(plain, "trevor", "plain-secret", digest))
        self.assertFalse(basic_ok(wrong_user, "trevor", "", digest))
        self.assertFalse(basic_ok(None, "trevor", "plain-secret", digest))
        self.assertFalse(basic_ok(good, "trevor", "", "not-a-hash"))
        self.assertTrue(origin_allowed(None, "host", "https"))
        self.assertTrue(origin_allowed("null", "127.0.0.1:8765", None, "same-origin"))
        self.assertFalse(origin_allowed("null", "127.0.0.1:8765", None, "cross-site"))
        self.assertFalse(origin_allowed("https://evil.example", "127.0.0.1:8765", "https"))


def _serve(cfg):
    httpd = Server(("127.0.0.1", 0), make_handler(cfg))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


class HashAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.digest = hashlib.sha256(b"hash-secret").hexdigest()
        self.cfg = Config(
            data_dir=self.tmp.name,
            password="plain-secret",
            user="trevor",
            ingest_token="test-token",
            sample_path=SAMPLE,
            password_sha256=self.digest,
        )
        self.httpd = _serve(self.cfg)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _open(self, path, auth=None, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as ex:
            return ex.code, ex.read()

    def test_hash_wins_and_state_uses_the_same_check(self):
        status, body = self._open("/healthz")
        self.assertEqual(status, 200, body)
        hashed = "Basic " + base64.b64encode(b"trevor:hash-secret").decode()
        plaintext = "Basic " + base64.b64encode(b"trevor:plain-secret").decode()
        status, body = self._open("/", auth=plaintext)
        self.assertEqual(status, 401, body)
        status, body = self._open("/", auth=hashed)
        self.assertEqual(status, 200, body)
        self.assertIn(b"North Pier Cafe", body)
        status, body = self._open("/api/state", auth=hashed)
        self.assertEqual(status, 200, body)
        self.assertIn("todos", json.loads(body))
        status, body = self._open("/api/state", auth=plaintext)
        self.assertEqual(status, 401, body)
        status, body = self._open("/api/state", auth="Bearer test-token")
        self.assertEqual(status, 200, body)

    def test_hash_alone_is_enough(self):
        self.cfg.password = ""
        status, body = self._open("/healthz")
        self.assertEqual(status, 200, body)
        hashed = "Basic " + base64.b64encode(b"trevor:hash-secret").decode()
        status, body = self._open("/api/state", auth=hashed)
        self.assertEqual(status, 200, body)
        status, _body = self._open("/api/ingest", method="POST")
        self.assertEqual(status, 401)

    def test_from_env_reads_the_hash(self):
        previous = os.environ.get("BASIC_AUTH_PASS_SHA256")
        os.environ["BASIC_AUTH_PASS_SHA256"] = self.digest.upper() + "\n"
        try:
            cfg = Config.from_env()
        finally:
            if previous is None:
                os.environ.pop("BASIC_AUTH_PASS_SHA256", None)
            else:
                os.environ["BASIC_AUTH_PASS_SHA256"] = previous
        self.assertEqual(cfg.password_sha256, self.digest)
        self.assertTrue(cfg.configured())


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
        html, _counts, _jarvis = render.render_main(data, fresh_state(), today=dt.date(2026, 10, 30))
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
        moment = dt.datetime(2026, 10, 30, 18, 0, tzinfo=dt.timezone.utc)
        clocks = render.clocks_html(moment, env={"HOME_TZ": "Asia/Bangkok", "HOME_LABEL": "Bangkok"})
        for city in ("Chicago", "New York", "Netherlands", "Sweden", "Seoul", "Tokyo", "San Diego", "UTC"):
            self.assertNotIn(city, clocks)
        self.assertLess(clocks.find(">Bangkok<"), clocks.find(">Vancouver<"))
        self.assertLess(clocks.find(">Vancouver<"), clocks.find(">Amsterdam<"))
        self.assertIn("clk current", clocks)
        self.assertIn(">Here<", clocks)
        self.assertIn(">Home<", clocks)
        self.assertEqual(clocks.count("data-tz="), 3)
        self.assertIn('data-tz="Asia/Bangkok"', clocks)
        self.assertNotIn("America/Los_Angeles", clocks)
        self.assertNotIn("Clocks show the time this page was built", template)
        self.assertIn("<!--PRE_JARVIS-->", template)
        self.assertLess(template.find("<!--PRE_JARVIS-->"), template.find("<!--PRE_FX-->"))
        self.assertLess(template.find("<!--PRE_FX-->"), template.find('id="clocks"'))
        self.assertIn("credentials: \"same-origin\"", template)
        self.assertIn("location.assign", template)
        self.assertIn("form.submit", template)

    def test_zones_and_display_day(self):
        zone, label = render.resolve_display({})
        self.assertEqual(zone.key, "America/Vancouver")
        self.assertEqual(label, "Vancouver")
        zone, label = render.resolve_display({"HOME_TZ": "Asia/Bangkok"})
        self.assertEqual(zone.key, "Asia/Bangkok")
        self.assertEqual(label, "Bangkok")
        zone, label = render.resolve_display({"HOME_TZ": "Asia/Bangkok", "DISPLAY_TZ": "Europe/Amsterdam"})
        self.assertEqual(zone.key, "Europe/Amsterdam")
        self.assertEqual(label, "Amsterdam")
        zone, label = render.resolve_display({"DISPLAY_TZ": "Not/AZone", "HOME_TZ": "Asia/Bangkok"})
        self.assertEqual(zone.key, "Asia/Bangkok")
        zone, label = render.resolve_display({"DISPLAY_TZ": "Nope", "HOME_TZ": "Also/Nope"})
        self.assertEqual(zone.key, "America/Vancouver")
        home_label, home_key = render.home_clock({"HOME_TZ": "Asia/Bangkok"})
        self.assertEqual((home_label, home_key), ("Bangkok", "Asia/Bangkok"))
        home_label, home_key = render.home_clock({"HOME_TZ": "Asia/Bangkok", "HOME_LABEL": "Where I am"})
        self.assertEqual(home_label, "Where I am")
        home_label, home_key = render.home_clock({"HOME_TZ": "America/Argentina/Buenos_Aires"})
        self.assertEqual(home_label, "Buenos Aires")
        home_rows = render.clock_rows({})
        self.assertEqual([row[1] for row in home_rows], ["America/Vancouver", "Europe/Amsterdam"])
        self.assertEqual(home_rows[0][2], "Here")
        self.assertTrue(home_rows[0][3])
        vancouver_rows = render.clock_rows({"HOME_TZ": "America/Vancouver"})
        self.assertEqual([row[1] for row in vancouver_rows], ["America/Vancouver", "Europe/Amsterdam"])
        self.assertEqual(vancouver_rows[0][2], "Here")
        amsterdam_rows = render.clock_rows({"HOME_TZ": "Europe/Amsterdam", "HOME_LABEL": "Amsterdam"})
        self.assertEqual([row[1] for row in amsterdam_rows], ["Europe/Amsterdam", "America/Vancouver"])
        self.assertEqual(amsterdam_rows[0][2], "Here")
        self.assertEqual(amsterdam_rows[1][2], "Home")
        osaka = {"restaurant_locations": [
            {"name": "Osaka", "from": "2026-11-07", "until": "2026-11-10", "tz": "Asia/Tokyo", "currency": "JPY", "eat": [], "delivery": []},
        ]}
        osaka_rows = render.clock_rows({"HOME_TZ": "Asia/Bangkok"}, osaka, dt.date(2026, 11, 8))
        self.assertEqual(osaka_rows[0][0], "Osaka")
        self.assertEqual(osaka_rows[0][1], "Asia/Tokyo")
        self.assertEqual([row[1] for row in osaka_rows], ["Asia/Tokyo", "America/Vancouver", "Europe/Amsterdam"])
        data = {
            "generated_at": "2026-10-30T17:00:00Z",
            "items": [],
            "calendar": [{
                "id": "z1",
                "subject": "Zone check meeting",
                "allDay": False,
                "start": "2026-10-30T17:00:00Z",
                "end": "2026-10-30T18:00:00Z",
                "stale": False,
            }],
            "restaurants": {"city": "", "as_of": "", "eat": [], "delivery": []},
        }
        bangkok, _counts, _jarvis = render.render_main(
            data, fresh_state(), today=dt.date(2026, 10, 30),
            zone=ZoneInfo("Asia/Bangkok"), zone_label="Bangkok",
        )
        self.assertIn("Tomorrow, Sat Oct 31", bangkok)
        self.assertIn("12:00 AM", bangkok)
        self.assertNotIn("Pacific", bangkok)
        self.assertNotIn("Zone check meeting", bangkok.split("Tomorrow, Sat Oct 31")[0])
        vancouver, _counts, _jarvis = render.render_main(
            data, fresh_state(), today=dt.date(2026, 10, 30),
            zone=ZoneInfo("America/Vancouver"), zone_label="Vancouver",
        )
        self.assertIn("10:00 AM", vancouver)
        self.assertIn("Zone check meeting", vancouver.split("Tomorrow, Sat Oct 31")[0])

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


class GlanceTests(unittest.TestCase):
    def _base(self):
        return {"items": [{"id": "S-1", "title": "Keep this task", "area": "Personal", "kind": "task", "section": "now", "due": "", "done": False, "links": []}], "calendar": [], "restaurants": {"city": "", "as_of": "", "eat": [], "delivery": []}}

    def test_missing_keys_hide_the_cards(self):
        html, _counts, _jarvis = render.render_main(self._base(), fresh_state(), now=dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc))
        self.assertNotIn('id="weather"', html)
        self.assertNotIn('id="next-up"', html)
        self.assertNotIn('id="today-meet"', html)
        self.assertNotIn('id="upgrades"', html)
        self.assertIn("Keep this task", html)

    def test_schema_accepts_glance_keys_and_rejects_bad_types(self):
        payload = self._base()
        payload["weather_places"] = [{"name": "Bangkok", "lat": 13.75, "lon": 100.5}]
        payload["travel"] = [{
            "id": "T-1", "kind": "flight", "title": "Sample hop",
            "start": "2026-11-03T12:00:00+07:00", "end": "2026-11-03T16:00:00+07:00",
            "from": "BKK", "to": "CNX", "confirmation": "ABC123", "details": "Seat 12A",
            "status": "confirmed", "url": "https://example.com/trip",
        }]
        payload["upgrades"] = [{
            "flight": "DL 100", "date": "2026-11-03", "route": "BKK-SEA",
            "requested_cabin": "Delta One", "status": "Waitlisted", "position": 4,
            "checked_at": "2026-11-01T12:00:00Z", "url": "https://example.com/delta",
        }]
        payload["calendar"] = [{
            "id": "m1", "subject": "Sample call", "allDay": False,
            "start": "2026-11-01T18:00:00Z", "end": "2026-11-01T19:00:00Z",
            "join_url": "https://teams.microsoft.com/l/meetup-join/sample", "stale": False,
        }]
        validate_data(payload)
        bad = self._base()
        bad["weather_places"] = "Bangkok"
        with self.assertRaises(ValueError):
            validate_data(bad)
        bad = dict(payload)
        bad["travel"] = [dict(payload["travel"][0], kind="boat")]
        with self.assertRaises(ValueError):
            validate_data(bad)
        bad = dict(payload)
        bad["weather_places"] = [{"name": "Bangkok", "lat": "north", "lon": 1}]
        with self.assertRaises(ValueError):
            validate_data(bad)

    def test_travel_meetings_and_upgrades_render(self):
        now = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        data = self._base()
        data["travel"] = [
            {
                "id": "T-old", "kind": "flight", "title": "Already landed",
                "start": "2026-10-01T01:00:00+07:00", "end": "2026-10-01T04:00:00+07:00",
            },
            {
                "id": "T-soon", "kind": "flight", "title": "Sample hop",
                "start": "2026-11-03T12:00:00+07:00", "end": "2026-11-03T16:00:00+07:00",
                "from": "BKK", "to": "CNX", "url": "https://example.com/trip",
            },
            {
                "id": "T-check", "kind": "flight", "title": "Morning hop",
                "start": "2026-11-01T20:00:00+07:00", "end": "2026-11-01T22:00:00+07:00",
                "url": "https://example.com/check",
            },
        ]
        data["calendar"] = [{
            "id": "m1", "subject": "Sample call", "allDay": False,
            "start": "2026-11-01T18:00:00Z", "end": "2026-11-01T19:00:00Z",
            "join_url": "https://teams.microsoft.com/l/meetup-join/sample",
            "webLink": "https://outlook.office.com/calendar/item/sample",
            "stale": False,
        }]
        data["upgrades"] = [{
            "flight": "DL 100", "date": "2026-11-03", "route": "BKK-SEA",
            "requested_cabin": "Delta One", "status": "Waitlisted", "position": 4,
            "checked_at": "2026-11-01T12:00:00Z", "url": "https://example.com/delta",
        }]
        html, _counts, _jarvis = render.render_main(
            data, fresh_state(), zone=ZoneInfo("America/Vancouver"), zone_label="Vancouver", now=now,
        )
        self.assertIn("Sample hop", html)
        self.assertNotIn("Already landed", html)
        self.assertIn("in 2 days", html)
        self.assertIn("Check-in open", html)
        self.assertIn('href="https://example.com/trip"', html)
        self.assertIn("12:00 PM", html)
        self.assertIn("Sample call", html)
        self.assertIn('class="lk joinbig"', html)
        self.assertIn("Vancouver", html)
        self.assertNotIn('id="upgrades"', html)
        card = html.split('id="next-up"', 1)[1]
        self.assertIn("Waitlisted", card)
        self.assertIn("Position 4", card)
        self.assertIn("Delta One", card)
        self.assertIn("BKK-SEA", card)
        self.assertIn('href="https://example.com/delta"', card)
        self.assertIn("Checked", card)
        self.assertNotIn("!", visible(html))

    def test_upgrade_status_renders_inside_the_matching_trip(self):
        now = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        data = self._base()
        data["travel"] = [
            {
                "id": "T-1", "kind": "flight", "title": "DL 1001 to Osaka",
                "start": "2026-11-03T09:30:00+07:00", "end": "2026-11-03T16:00:00+07:00",
                "from": "BKK", "to": "KIX",
            },
            {
                "id": "T-2", "kind": "hotel", "title": "Namba stay",
                "start": "2026-11-03T15:00:00+09:00", "end": "2026-11-06T11:00:00+09:00",
            },
        ]
        data["upgrades"] = [
            {
                "flight": "DL 1001", "date": "2026-11-03", "route": "BKK-KIX",
                "requested_cabin": "Delta One", "status": "Waitlisted", "position": 4,
                "checked_at": "2026-11-01T12:00:00Z", "url": "https://example.com/delta",
            },
            {
                "flight": "DL 1001", "date": "2026-11-09", "route": "BKK-KIX",
                "requested_cabin": "First", "status": "Cleared", "position": 1,
                "checked_at": "2026-11-01T12:00:00Z", "url": "https://example.com/cleared",
            },
            {
                "flight": "DL 999", "date": "2026-11-08", "route": "SEA-BKK",
                "requested_cabin": "Comfort+", "status": "Requested", "position": 2,
                "checked_at": "2026-11-01T12:00:00Z", "url": "https://example.com/other",
            },
        ]
        html, _counts, _jarvis = render.render_main(
            data, fresh_state(), zone=ZoneInfo("Asia/Bangkok"), zone_label="Bangkok", now=now,
        )
        self.assertNotIn('id="upgrades"', html)
        card = html.split('id="next-up"', 1)[1]
        trip, rest = card.split("Namba stay", 1)
        self.assertIn("Waitlisted", trip)
        self.assertIn("Position 4", trip)
        self.assertIn("Delta One", trip)
        self.assertIn("BKK-KIX", trip)
        self.assertIn("Checked", trip)
        self.assertIn('href="https://example.com/delta"', trip)
        self.assertNotIn("Requested", trip)
        self.assertNotIn("Cleared", trip)
        self.assertIn("Requested", rest)
        self.assertIn("Cleared", rest)
        self.assertIn('href="https://example.com/other"', rest)
        self.assertTrue(render._upgrade_match(
            {"flight": "DL 200", "date": "2026-11-04", "route": "BKK-KIX"},
            {"title": "City hop", "from": "BKK", "to": "KIX"},
            dt.datetime(2026, 11, 4, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=7))),
        ))
        bare = self._base()
        bare["travel"] = data["travel"]
        bare["upgrades"] = []
        empty, _counts, _jarvis = render.render_main(bare, fresh_state(), now=now)
        self.assertIn("DL 1001 to Osaka", empty)
        self.assertNotIn('class="up"', empty)
        self.assertNotIn('id="upgrades"', empty)
        only = self._base()
        only["upgrades"] = data["upgrades"][:1]
        lone, _counts, _jarvis = render.render_main(
            only, fresh_state(), zone=ZoneInfo("Asia/Bangkok"), zone_label="Bangkok", now=now,
        )
        self.assertIn('id="next-up"', lone)
        self.assertIn("Waitlisted", lone.split('id="next-up"', 1)[1])
        self.assertNotIn('id="upgrades"', lone)
        self.assertNotIn("!", visible(html))

    def test_hotel_dates_weather_days_and_show_all(self):
        now = dt.datetime(2026, 10, 28, 12, tzinfo=dt.timezone.utc)
        zone = dt.timezone.utc
        data = self._base()
        trips = [
            {
                "id": "H1", "kind": "hotel", "title": "Pier stay",
                "start": "2026-10-29T00:00:00+07:00", "end": "2026-11-04T00:00:00+07:00",
                "details": "Quiet room · Late checkout · High floor",
            },
            {
                "id": "F0", "kind": "flight", "title": "Midnight hop",
                "start": "2026-10-30T00:00:00Z", "end": "2026-10-30T05:00:00Z",
            },
            {
                "id": "Fsame", "kind": "other", "title": "One moment",
                "start": "2026-11-02T18:00:00Z", "end": "2026-11-02T18:00:00Z",
            },
        ]
        for index in range(1, 5):
            trips.append({
                "id": f"Fx{index}", "kind": "flight", "title": f"Later hop {index}",
                "start": f"2026-11-0{index + 2}T15:00:00Z", "end": f"2026-11-0{index + 2}T18:00:00Z",
            })
        data["travel"] = trips
        html, _counts, _jarvis = render.render_main(
            data, fresh_state(), zone=zone, zone_label="UTC", now=now,
        )
        self.assertIn("Check-in Thu Oct 29", html)
        self.assertIn("Check-out Wed Nov 4", html)
        self.assertIn("Fri Oct 30", html)
        self.assertNotIn("12:00 AM", html)
        before, after = html.split("Show all", 1)
        self.assertIn("Pier stay", before)
        self.assertIn("Later hop 2", before)
        self.assertIn("Later hop 3", after)
        self.assertNotIn("Later hop 3", before)
        self.assertIn("<summary>Details</summary>", html)
        self.assertIn("Quiet room", html)
        same = html.split("One moment", 1)[1].split("</div>", 1)[0]
        self.assertNotIn(" to ", same)
        card = render.weather_card([{
            "name": "Bangkok", "temp": 31, "sky": "Clear", "wind": 5,
            "days": [
                {"date": "2026-11-01", "high": 33, "low": 25, "sky": "Clear"},
                {"date": "2026-11-02", "high": 32, "low": 24, "sky": "Rain"},
                {"date": "2026-11-03", "high": 31, "low": 24, "sky": "Clouds"},
            ],
        }], today=dt.date(2026, 11, 1))
        self.assertIn(">Today<", card)
        self.assertIn(">Tomorrow<", card)
        self.assertIn(">Tue Nov 3<", card)
        self.assertNotIn("2026-11-01", card)
        self.assertNotIn("!", visible(html + card))

    def test_weather_fallback_and_cache(self):
        import weather as weather_mod
        places = [{"name": "Bangkok", "lat": 13.75, "lon": 100.5}]
        calls = []

        def boom(url):
            calls.append(url)
            raise OSError("down")

        clock = {"t": 1_700_000_000.0}
        cache = {}
        rows = weather_mod.get_forecasts(places, fetcher=boom, clock=lambda: clock["t"], cache=cache)
        self.assertTrue(rows[0]["unavailable"])
        data = self._base()
        data["weather_places"] = places
        html, _counts, _jarvis = render.render_main(
            data, fresh_state(), weather=rows, now=dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc),
        )
        self.assertIn("weather unavailable", html)
        self.assertIn("Keep this task", html)
        self.assertIn("open-meteo.com", calls[0])

        calls.clear()
        payload = {
            "current": {"temperature_2m": 31.2, "weather_code": 1, "wind_speed_10m": 12.4},
            "daily": {
                "time": ["2026-11-01", "2026-11-02", "2026-11-03"],
                "temperature_2m_max": [33, 32, 31],
                "temperature_2m_min": [25, 24, 24],
                "weather_code": [1, 61, 3],
            },
        }

        def ok(url):
            calls.append(url)
            return payload

        cache = {}
        first = weather_mod.get_forecasts(places, fetcher=ok, clock=lambda: clock["t"], cache=cache)
        second = weather_mod.get_forecasts(places, fetcher=ok, clock=lambda: clock["t"] + 10, cache=cache)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first[0]["temp"], 31)
        self.assertEqual(second[0]["sky"], "Mainly clear")
        self.assertEqual(len(first[0]["days"]), 3)
        clock["t"] += weather_mod.CACHE_SECONDS + 5
        weather_mod.get_forecasts(places, fetcher=ok, clock=lambda: clock["t"], cache=cache)
        self.assertEqual(len(calls), 2)
        shown, _counts, _jarvis = render.render_main(
            data, fresh_state(), weather=first, now=dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc),
        )
        self.assertIn("31 C", shown)
        self.assertIn("km/h", shown)
        self.assertNotIn("!", visible(shown))


class RedesignTests(unittest.TestCase):
    def test_quadrant_defaults_and_persisted_move(self):
        today = dt.date(2026, 10, 31)
        self.assertEqual(render.derive_quadrant({
            "section": "open", "due": "2026-10-30", "important": True, "kind": "task",
        }, today), "do_now")
        self.assertEqual(render.derive_quadrant({
            "section": "open", "due": "2026-12-01", "important": True, "kind": "task",
        }, today), "schedule")
        self.assertEqual(render.derive_quadrant({
            "section": "open", "due": "2026-10-30", "owner": "Ada", "kind": "task",
        }, today), "delegate")
        self.assertEqual(render.derive_quadrant({
            "section": "watch", "due": "", "priority": "low", "kind": "task",
        }, today), "delete")
        from actions import apply
        state = fresh_state()
        apply(state, "todo/quadrant", {"id": "S-open-1", "quadrant": "delete"}, "2026-10-31T00:00:00Z")
        data = json.load(open(SAMPLE, encoding="utf-8"))
        html, _counts, _jarvis = render.render_main(data, state, today=today, now=dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc))
        chunk = html.split('id="quadrant-delete"')[1].split('id="quadrant-')[0]
        self.assertIn("weekly note", chunk)

    def test_clone_filter_and_note_and_freshness(self):
        events = [
            {"id": "a", "subject": "Real call", "start": "2026-11-01T17:00:00Z", "end": "2026-11-01T18:00:00Z"},
            {"id": "b", "subject": "Real call (Clone)", "start": "2026-11-01T17:00:00Z", "end": "2026-11-01T18:00:00Z", "note": "Starts 10:00 AM Bangkok"},
        ]
        kept = render.drop_clones(events)
        self.assertEqual([row["id"] for row in kept], ["a"])
        self.assertEqual(render.tidy_note("Starts 10:00 AM Bangkok", "Bangkok"), "Starts")
        generated = dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc)
        now = dt.datetime(2026, 10, 31, 12, tzinfo=dt.timezone.utc)
        self.assertEqual(render.freshness_class(generated, now), "stale-red")
        self.assertEqual(render.freshness_class(now - dt.timedelta(hours=13), now), "stale-amber")
        self.assertEqual(render.freshness_class(now - dt.timedelta(hours=2), now), "")

    def test_restaurant_location_and_waiting_and_defaults(self):
        today = dt.date(2026, 11, 7)
        data = {
            "items": [],
            "calendar": [],
            "restaurants": {"city": "Sample Harbor", "as_of": "", "eat": [{"name": "North Pier Cafe", "area": "Pier", "cuisine": "Soup", "price": "20 CAD", "why": "Fallback.", "map_url": "https://example.com/a"}], "delivery": []},
            "restaurant_locations": [
                {"name": "Bangkok", "from": "2026-10-01", "until": "2026-11-07", "currency": "THB", "eat": [{"name": "Old stop", "area": "Pier", "cuisine": "Noodles", "price": "100", "why": "Last day.", "map_url": "https://example.com/b"}], "delivery": []},
                {"name": "Osaka", "from": "2026-11-07", "until": "2026-11-10", "currency": "JPY", "eat": [{"name": "Counter seat", "area": "Namba", "cuisine": "Grill", "price": "2000", "why": "New city.", "map_url": "https://example.com/c"}], "delivery": []},
            ],
            "waiting_on": [
                {"id": "W-1", "who": "Demo Person", "what": "Sign the sample note", "since": "2026-10-01", "channel": "email", "url": "https://example.com/mail"},
            ],
        }
        rates = {"cad_per": {"JPY": 0.01, "CAD": 1}}
        html, _counts, _jarvis = render.render_main(data, fresh_state(), today=today, rates=rates, now=dt.datetime(2026, 11, 7, tzinfo=dt.timezone.utc))
        self.assertIn("Counter seat", html)
        self.assertNotIn("Old stop", html)
        self.assertIn("about 20 CAD", html)
        self.assertIn("wait hot", html)
        self.assertIn("Sign the sample note", html)
        bare = {"items": [], "calendar": [], "restaurants": {"city": "", "as_of": "", "eat": [], "delivery": []}}
        plain, _counts, _jarvis = render.render_main(bare, fresh_state())
        self.assertIn("Question the requirement", plain)
        self.assertIn("Delete candidates", plain)

    def test_layout_order_fx_line_and_empty_quadrants(self):
        today = dt.date(2026, 10, 31)
        data = json.load(open(SAMPLE, encoding="utf-8"))
        html, _counts, jarvis = render.render_main(
            data, fresh_state(), today=today, now=dt.datetime(2026, 10, 31, 12, tzinfo=dt.timezone.utc),
        )
        self.assertLess(html.find('id="quadrant-do_now"'), html.find('id="waiting-on"'))
        self.assertLess(html.find('id="waiting-on"'), html.find('id="quadrant-schedule"'))
        self.assertLess(html.find('id="quadrant-schedule"'), html.find('id="quadrant-delegate"'))
        self.assertLess(html.find('id="quadrant-delegate"'), html.find('id="quadrant-delete"'))
        self.assertLess(html.find('id="quadrant-delete"'), html.find('id="calendar"'))
        self.assertLess(html.find('id="calendar"'), html.find('id="restaurants"'))
        self.assertLess(html.find('id="restaurants"'), html.find('id="emails"'))
        self.assertIn("Today's calendar", html)
        self.assertNotIn('id="jarvis"', html)
        self.assertIn('id="jarvis"', jarvis)
        self.assertIn('class="home-top"', html)
        bare = {
            "items": [],
            "calendar": [],
            "restaurants": {"city": "", "as_of": "", "eat": [], "delivery": []},
        }
        plain, _counts, _jarvis = render.render_main(bare, fresh_state(), today=today)
        self.assertIn('class="quad-line" id="quadrant-schedule"', plain)
        self.assertIn("Schedule: nothing.", plain)
        self.assertIn("Waiting on: nothing.", plain)
        self.assertNotIn('class="quad q-schedule"', plain)
        rates = {
            "cad_per": {"CAD": 1, "USD": 1.396, "THB": 0.04135, "JPY": 0.00887},
            "usd_per": {"CAD": 1},
            "source": "Bank of Canada Valet",
            "date": "2026-10-30",
        }
        fx = render.fx_html(data, rates)
        self.assertIn("USD to CAD", fx)
        self.assertIn("THB to CAD", fx)
        self.assertIn("JPY to CAD", fx)
        self.assertIn("0.04135", fx)
        self.assertIn("0.00887", fx)
        self.assertNotIn("!", visible(plain + fx + jarvis))

    def test_fx_pairs_from_valet_shape(self):
        import fxrates
        payload = {"observations": [{"d": "2026-10-30", "FXUSDCAD": {"v": "1.3960"}, "FXTHBCAD": {"v": "0.04135"}, "FXJPYCAD": {"v": "0.008870"}}]}
        rates = fxrates._from_valet(payload)
        rates["usd_per"] = fxrates.usd_per(rates["cad_per"])
        self.assertAlmostEqual(rates["cad_per"]["THB"], 0.04135)
        self.assertAlmostEqual(rates["usd_per"]["THB"], 0.04135 / 1.3960, places=6)
        self.assertAlmostEqual(rates["usd_per"]["JPY"], 0.008870 / 1.3960, places=6)
        self.assertEqual(rates["source"], "Bank of Canada Valet")

    def test_quadrant_order_and_reason_labels(self):
        today = dt.date(2026, 11, 1)
        self.assertEqual(
            render.quadrant_reason({"important": True, "due": "2026-11-03", "section": "open"}, today),
            "Important, due in 2 days",
        )
        self.assertEqual(
            render.quadrant_reason({"due": "2026-10-31", "section": "open"}, today),
            "Urgent, overdue 1 day",
        )
        self.assertEqual(
            render.quadrant_reason({"section": "watch", "due": ""}, today),
            "Not urgent, no due date",
        )
        self.assertEqual(
            render.quadrant_reason({"quadrant_manual": True, "important": True, "due": "2026-11-03"}, today),
            "Moved manually",
        )
        for text in (
            "Important, due in 2 days",
            "Urgent, overdue 1 day",
            "Not urgent, no due date",
            "Moved manually",
        ):
            self.assertNotIn("\u2014", text)
            self.assertNotIn("\u2013", text)
        data = {
            "items": [
                {"id": "N1", "title": "Do the urgent thing", "area": "Personal", "kind": "task", "section": "now", "important": True, "due": "2026-11-01", "done": False, "links": []},
                {"id": "N2", "title": "Schedule the later thing", "area": "Personal", "kind": "task", "section": "open", "important": True, "due": "2026-12-01", "done": False, "links": []},
                {"id": "N3", "title": "Delegate the overdue thing", "area": "Personal", "kind": "task", "section": "open", "due": "2026-10-31", "done": False, "links": []},
                {"id": "N4", "title": "Drop the someday thing", "area": "Personal", "kind": "task", "section": "watch", "due": "", "done": False, "links": []},
            ],
            "calendar": [],
            "restaurants": {"city": "", "as_of": "", "eat": [], "delivery": []},
        }
        html, _counts, _jarvis = render.render_main(data, fresh_state(), today=today)
        positions = [html.find(f'id="quadrant-{key}"') for key in ("do_now", "schedule", "delegate", "delete")]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn(-1, positions)
        self.assertLess(html.find("Do the urgent thing"), html.find("Schedule the later thing"))
        self.assertLess(html.find("Schedule the later thing"), html.find("Delegate the overdue thing"))
        self.assertLess(html.find("Delegate the overdue thing"), html.find("Drop the someday thing"))
        self.assertIn("Important, due today", html)
        self.assertIn("Urgent, overdue 1 day", html)
        self.assertIn("Not urgent, no due date", html)
        state = fresh_state()
        apply(state, "todo/quadrant", {"id": "N2", "quadrant": "delete"}, "2026-11-01T00:00:00Z")
        moved, _counts, _jarvis = render.render_main(data, state, today=today)
        delete_chunk = moved.split('id="quadrant-delete"', 1)[1].split('id="calendar"', 1)[0]
        self.assertIn("Schedule the later thing", delete_chunk)
        self.assertIn("Moved manually", delete_chunk)

    def test_row_links_attachments_and_draft_ready(self):
        base = {
            "calendar": [],
            "restaurants": {"city": "", "as_of": "", "eat": [], "delivery": []},
        }
        files = [{"name": f"File{i}.pdf", "url": f"https://example.com/f{i}.pdf"} for i in range(1, 6)]
        item = {
            "id": "S-1", "title": "Reply with the draft", "area": "Echandia", "kind": "email",
            "section": "now", "due": "2026-11-01", "done": False,
            "note": "Asking for a sample date that stays on one line until tapped.",
            "webLink": "https://outlook.office.com/mail/inbox/sample",
            "links": [
                {"kind": "draft", "url": "https://outlook.office.com/mail/drafts/sample"},
                {"kind": "clickup", "href": "https://app.clickup.com/t/sampletask", "label": "ClickUp sample"},
            ],
            "attachments": files,
        }
        html, _counts, _jarvis = render.render_main(
            {**base, "items": [item]}, fresh_state(), today=dt.date(2026, 11, 1),
        )
        self.assertLess(html.find("Draft ready"), html.find("Open email"))
        self.assertLess(html.find("Open email"), html.find("ClickUp sample"))
        self.assertIn('class="draftready"', html)
        self.assertIn('details class="note"', html)
        self.assertIn("Asking for a sample date", html)
        head, tail = html.split("+2 more", 1)
        self.assertIn("File1.pdf", head)
        self.assertIn("File3.pdf", head)
        self.assertNotIn("File5.pdf", head)
        self.assertIn("File5.pdf", tail)
        task = dict(item, id="S-2", kind="task", links=[], webLink="https://outlook.office.com/mail/inbox/task")
        task_html, _counts, _jarvis = render.render_main({**base, "items": [task]}, fresh_state(), today=dt.date(2026, 11, 1))
        self.assertIn("Open email", task_html)
        self.assertNotIn("Draft ready", task_html)
        one = dict(item, id="S-3", links=[], webLink="", attachments=files[:1])
        one_html, _counts, _jarvis = render.render_main({**base, "items": [one]}, fresh_state(), today=dt.date(2026, 11, 1))
        self.assertIn("File1.pdf", one_html)
        self.assertNotIn("+", one_html.split("File1.pdf", 1)[1][:80])
        none = dict(item, id="S-4", links=[], webLink="", attachments=[])
        none_html, _counts, _jarvis = render.render_main({**base, "items": [none]}, fresh_state(), today=dt.date(2026, 11, 1))
        self.assertNotIn("File1.pdf", none_html)
        self.assertNotIn("Draft ready", none_html)
        self.assertNotIn("Open email", none_html)
        payload = {**base, "items": [item]}
        validate_data(payload)
        bare = {**base, "items": [{"id": "S-9", "title": "Old shape", "area": "Personal"}]}
        validate_data(bare)
        bad = {**base, "items": [dict(item, attachments=[{"name": "X", "url": "ftp://example.com/x"}])]}
        with self.assertRaises(ValueError):
            validate_data(bad)
        long_name = {**base, "items": [dict(item, attachments=[{"name": "n" * 81, "url": "https://example.com/n"}])]}
        with self.assertRaises(ValueError):
            validate_data(long_name)
        bad_draft = {**base, "items": [dict(item, links=[{"kind": "draft", "url": "mailto:a@example.com"}])]}
        with self.assertRaises(ValueError):
            validate_data(bad_draft)
        self.assertNotIn("!", visible(html))


class RobbinsTests(HttpCase):
    MORNING = [
        "What am I happy about today?",
        "What am I excited about?",
        "What am I proud of?",
        "What am I grateful for?",
        "What am I committed to today?",
    ]
    EVENING = [
        "What did I give today?",
        "What did I learn today?",
        "How did today add to my life / the future?",
    ]

    def _board(self):
        return {
            "items": [],
            "calendar": [],
            "restaurants": {"city": "", "as_of": "", "eat": [], "delivery": []},
            "principles": {"robbins": {"morning": self.MORNING, "evening": self.EVENING}},
        }

    def test_question_ids_ignore_list_index(self):
        first = render.question_id("morning", self.MORNING[0])
        self.assertEqual(first, render.question_id("morning", self.MORNING[0]))
        self.assertNotEqual(first, render.question_id("evening", self.MORNING[0]))
        html = render.principles_html(self._board(), 0, {})
        flipped = render.principles_html(
            {"principles": {"robbins": {"morning": list(reversed(self.MORNING)), "evening": self.EVENING}}},
            0,
            {},
        )
        self.assertIn(f'value="{first}"', html)
        self.assertIn(f'value="{first}"', flipped)
        self.assertLess(html.find(self.MORNING[0]), html.find(self.MORNING[1]))
        self.assertLess(flipped.find(self.MORNING[-1]), flipped.find(self.MORNING[0]))

    def test_toggle_persists_and_day_follows_current_location_zone(self):
        moment = dt.datetime(2026, 11, 1, 6, 0, tzinfo=dt.timezone.utc)
        bangkok_day, bangkok_zone = render.ritual_day({"HOME_TZ": "Asia/Bangkok"}, {}, moment)
        vancouver_day, vancouver_zone = render.ritual_day({"HOME_TZ": "America/Vancouver"}, {}, moment)
        missing_day, missing_zone = render.ritual_day({}, {}, moment)
        self.assertEqual((bangkok_day, bangkok_zone), ("2026-11-01", "Asia/Bangkok"))
        self.assertEqual((vancouver_day, vancouver_zone), ("2026-10-31", "America/Vancouver"))
        self.assertEqual(missing_zone, "Asia/Bangkok")
        self.assertNotEqual(missing_zone, "America/Vancouver")
        osaka_day, osaka_zone = render.ritual_day(
            {"HOME_TZ": "Asia/Bangkok"},
            {"location": {"name": "Osaka", "tz": "Asia/Tokyo"}},
            moment,
        )
        self.assertEqual(osaka_zone, "Asia/Tokyo")
        self.assertEqual(osaka_day, "2026-11-01")
        qid = render.question_id("morning", self.MORNING[0])
        state = fresh_state()
        apply(state, "question/toggle", {"id": qid, "done": True, "day": bangkok_day}, "2026-11-01T06:00:00Z")
        shown, _counts, _jarvis = render.render_main(
            self._board(), state, env={"HOME_TZ": "Asia/Bangkok"}, now=moment,
        )
        self.assertIn('aria-pressed="true"', shown.split(self.MORNING[0], 1)[0][-180:])
        away, _counts, _jarvis = render.render_main(
            self._board(), state, env={"HOME_TZ": "America/Vancouver"}, now=moment,
        )
        happy = away.split(self.MORNING[0], 1)[0][-180:]
        self.assertIn('aria-pressed="false"', happy)
        later = dt.datetime(2026, 11, 1, 8, 30, tzinfo=dt.timezone.utc)
        vancouver_next, _zone = render.ritual_day({"HOME_TZ": "America/Vancouver"}, {}, later)
        self.assertEqual(vancouver_next, "2026-11-01")
        rolled, _counts, _jarvis = render.render_main(
            self._board(), state, env={"HOME_TZ": "America/Vancouver"}, now=later,
        )
        self.assertIn('aria-pressed="true"', rolled.split(self.MORNING[0], 1)[0][-180:])
        apply(state, "question/toggle", {"id": qid, "done": True, "day": "2026-10-31"}, "2026-11-01T06:00:00Z")
        before_midnight = dt.datetime(2026, 11, 1, 7, 30, tzinfo=dt.timezone.utc)
        on_old_day, _counts, _jarvis = render.render_main(
            self._board(), state, env={"HOME_TZ": "America/Vancouver"}, now=before_midnight,
        )
        self.assertIn('aria-pressed="true"', on_old_day.split(self.MORNING[0], 1)[0][-180:])
        after = dt.datetime(2026, 11, 2, 8, 30, tzinfo=dt.timezone.utc)
        reset, _counts, _jarvis = render.render_main(
            self._board(), state, env={"HOME_TZ": "America/Vancouver"}, now=after,
        )
        self.assertIn('aria-pressed="false"', reset.split(self.MORNING[0], 1)[0][-180:])

    def test_question_toggle_requires_auth_and_persists(self):
        question = "What did I learn today?"
        qid = render.question_id("evening", question)
        status, _headers, _body = self.request(
            "/api/question/toggle",
            method="POST",
            data=json.dumps({"id": qid, "done": True}).encode(),
            headers={"Content-Type": "application/json"},
            auth=False,
        )
        self.assertEqual(status, 401)
        status, _headers, body = self.request(
            "/api/question/toggle",
            method="POST",
            data=json.dumps({"id": qid, "done": True, "day": "1999-01-01"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertTrue(saved["result"]["done"])
        self.assertNotEqual(saved["result"]["day"], "1999-01-01")
        status, _headers, page = self.request("/")
        self.assertEqual(status, 200)
        text = page.decode()
        self.assertIn(question, text)
        self.assertIn('aria-pressed="true"', text.split(question, 1)[0][-220:])
        state = json.load(open(os.path.join(self.tmp.name, "state.json"), encoding="utf-8"))
        self.assertTrue(state["questions"][saved["result"]["day"]][qid]["done"])


class VoiceTests(HttpCase):
    def _write_mail(self):
        data = self.sample()
        for item in data["items"]:
            if item.get("id") == "S-mail-1":
                item["message_id"] = "AAMk-sample"
                item["conversation_id"] = "conv-sample"
                item["subject"] = "Harbor note"
        with open(os.path.join(self.tmp.name, "data.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_local_answers(self):
        facts = {
            "due_today": ["File the form"],
            "do_now": ["Call the harbor"],
            "next_trip": "DL 1001 to Osaka",
            "next_trip_kind": "flight",
            "meetings": ["Budget decision"],
            "waiting": ["Ada Cole: Sign the drawing"],
            "upgrades": ["DL 1001, Waitlisted, position 4"],
        }
        self.assertEqual(voice.answer("what's due today", facts), "File the form")
        self.assertEqual(voice.answer("what is in Do now", facts), "Call the harbor")
        self.assertEqual(voice.answer("next flight", facts), "Next flight is DL 1001 to Osaka.")
        self.assertEqual(voice.answer("today's meetings", facts), "Budget decision")
        self.assertEqual(voice.answer("who am I waiting on", facts), "Ada Cole: Sign the drawing")
        self.assertEqual(voice.answer("upgrade status", facts), "DL 1001, Waitlisted, position 4")
        self.assertEqual(voice.answer("tell me a joke", facts), "I can't answer that from the board yet.")
        self.assertEqual(voice.answer("due today", {"due_today": []}), "Nothing is due today.")
        self.assertEqual(voice.answer("anything", facts, backend=lambda question, found: "Later model"), "Later model")

    def test_draft_queue_auth_bearer_and_done(self):
        self._write_mail()
        body = json.dumps({"item_id": "S-mail-1", "text": "Please send the file."}).encode()
        status, _headers, _raw = self.request(
            "/api/voice/draft_request", method="POST", data=body,
            headers={"Content-Type": "application/json"}, auth=False,
        )
        self.assertEqual(status, 401)
        status, _headers, raw = self.request(
            "/api/voice/draft_request", method="POST", data=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, raw)
        saved = json.loads(raw)["result"]
        self.assertEqual(saved["status"], "pending")
        self.assertEqual(saved["item_id"], "S-mail-1")
        self.assertEqual(saved["message_id"], "AAMk-sample")
        self.assertEqual(saved["conversation_id"], "conv-sample")
        self.assertEqual(saved["subject"], "Harbor note")
        self.assertEqual(saved["text"], "Please send the file.")
        self.assertTrue(saved["id"].startswith("V-"))
        status, _headers, page = self.request("/")
        self.assertIn(b'class="draftwait">Draft requested', page)
        status, _headers, raw = self.request("/api/voice/draft_requests?status=pending", auth=False)
        self.assertEqual(status, 503)
        self.assertIn(b"VOICE_PICKUP_TOKEN is unset", raw)
        self.cfg.voice_pickup_token = "pickup-secret"
        status, _headers, raw = self.request(
            "/api/voice/draft_requests?status=pending",
            auth="Bearer wrong-token",
        )
        self.assertEqual(status, 401)
        status, _headers, raw = self.request(
            "/api/voice/draft_requests?status=pending",
            auth="Bearer pickup-secret",
        )
        self.assertEqual(status, 200)
        listed = json.loads(raw)["requests"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], saved["id"])
        status, _headers, raw = self.request(
            "/api/voice/draft_requests/" + saved["id"] + "/done",
            method="POST",
            data=json.dumps({"draft_weblink": "https://outlook.office.com/mail/drafts/ready"}).encode(),
            headers={"Content-Type": "application/json"},
            auth="Bearer pickup-secret",
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["result"]["status"], "done")
        status, _headers, raw = self.request(
            "/api/voice/draft_requests?status=pending",
            auth="Bearer pickup-secret",
        )
        self.assertEqual(json.loads(raw)["requests"], [])
        status, _headers, page = self.request("/")
        row = page.decode().split('data-id="S-mail-1"', 1)[1].split("</li>", 1)[0]
        self.assertNotIn("Draft requested", row)
        self.assertIn("https://outlook.office.com/mail/drafts/ready", row)
        self.assertIn("Draft ready", row)


if __name__ == "__main__":
    unittest.main()
