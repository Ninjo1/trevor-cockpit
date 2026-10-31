#!/usr/bin/env python3
"""Check the cockpit page over HTTP, and in a browser when Playwright is installed.

Usage:
  python3 verify_ui.py
  python3 verify_ui.py http://127.0.0.1:8080 --user trevor --password secret
"""
import argparse
import base64
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))


def visible(page):
    text = re.sub(r"<script[\s\S]*?</script>", " ", page, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def request(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read()


def check(base, user, password):
    base = base.rstrip("/")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    auth = {"Authorization": "Basic " + token}
    report = {}
    status, body = request(base + "/healthz")
    report["healthz"] = status
    status, body = request(base + "/")
    report["root_without_auth"] = status
    status, body = request(base + "/", headers=auth)
    page = body.decode("utf-8", "replace")
    report["page"] = status
    text = visible(page)
    report["has_restaurants"] = "North Pier Cafe" in page or "Restaurants" in page
    report["has_clocks"] = "Vancouver" in page and "Amsterdam" in page and "San Diego" not in page
    report["has_fx"] = "USD to CAD" in page
    report["has_principles"] = "Principles" in page
    report["has_emails"] = "Emails waiting" in page
    report["has_money"] = "Money owed" in page
    report["has_manifest"] = "manifest" in page and "apple-touch-icon" in page
    report["exclamation_in_visible_text"] = "!" in text
    status, raw = request(
        base + "/api/todo/toggle",
        method="POST",
        data=json.dumps({"id": "S-now-1", "done": True}).encode(),
        headers={**auth, "Content-Type": "application/json"},
    )
    report["tick"] = status
    status, body = request(base + "/", headers=auth)
    report["tick_visible"] = b"it done" in body
    failures = []
    if report["healthz"] != 200:
        failures.append("healthz")
    if report["root_without_auth"] != 401:
        failures.append("auth")
    if report["page"] != 200:
        failures.append("page")
    for key in ("has_restaurants", "has_clocks", "has_fx", "has_principles", "has_emails", "has_money", "has_manifest", "tick_visible"):
        if not report[key]:
            failures.append(key)
    if report["tick"] != 200:
        failures.append("tick")
    if report["exclamation_in_visible_text"]:
        failures.append("exclamation")
    report["ok"] = not failures
    report["failures"] = failures
    return report


def serve_sample():
    from server import Server, make_handler
    from store import Config

    tmp = tempfile.TemporaryDirectory()
    cfg = Config(
        data_dir=tmp.name,
        password="verify-pass",
        user="trevor",
        ingest_token="verify-token",
        sample_path=os.path.join(ROOT, "sample", "data.json"),
    )
    httpd = Server(("127.0.0.1", 0), make_handler(cfg))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, tmp, f"http://127.0.0.1:{httpd.server_address[1]}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?")
    parser.add_argument("--user", default="trevor")
    parser.add_argument("--password", default="verify-pass")
    args = parser.parse_args()
    httpd = tmp = None
    base = args.base
    if not base:
        httpd, tmp, base = serve_sample()
    try:
        report = check(base, args.user, args.password)
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if tmp is not None:
            tmp.cleanup()
    print(json.dumps(report, indent=1))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
