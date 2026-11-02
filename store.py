"""Atomic JSON files, state backups, and the data file for the cockpit."""
import datetime as dt
import json
import os
import re
import tempfile
from zoneinfo import ZoneInfo

import events as eventlog

PT = ZoneInfo("America/Vancouver")
BACKUP_DAYS = 14
_DAY = re.compile(r"state-(\d{4}-\d{2}-\d{2})\.json")


class StateError(Exception):
    """state.json is present but cannot be trusted. Callers must not overwrite it."""


class Config:
    def __init__(self, data_dir, password, user="trevor", ingest_token="", sample_path=None, password_sha256="", voice_pickup_token=""):
        self.data_dir = data_dir
        self.password = password if (password or "").strip() else ""
        self.password_sha256 = (password_sha256 or "").strip().lower()
        self.user = (user or "trevor").strip() or "trevor"
        self.ingest_token = ingest_token if (ingest_token or "").strip() else ""
        self.sample_path = sample_path
        self.voice_pickup_token = voice_pickup_token if (voice_pickup_token or "").strip() else ""

    def configured(self):
        """True when Basic auth has a plaintext password or a password hash."""
        return bool(self.password or self.password_sha256)

    @classmethod
    def from_env(cls, root=None):
        root = root or os.path.dirname(os.path.abspath(__file__))
        return cls(
            data_dir=os.environ.get("DATA_DIR") or os.path.join(root, "var", "data"),
            password=os.environ.get("BASIC_AUTH_PASS") or "",
            user=os.environ.get("BASIC_AUTH_USER") or "trevor",
            ingest_token=os.environ.get("INGEST_TOKEN") or "",
            sample_path=os.environ.get("SAMPLE_DATA") or os.path.join(root, "sample", "data.json"),
            password_sha256=os.environ.get("BASIC_AUTH_PASS_SHA256") or "",
            voice_pickup_token=os.environ.get("VOICE_PICKUP_TOKEN") or "",
        )


def fsync_dir(directory):
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, write_fn):
    """Write path by flushing a temp file in the same directory, then replacing.

    If write_fn raises, the original file is left in place and the temp file
    is removed. Readers see either the old file or the new file.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            write_fn(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
        fsync_dir(directory)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def atomic_write_json(path, obj):
    def write_fn(handle):
        json.dump(obj, handle, indent=1, ensure_ascii=False)
        handle.write("\n")

    atomic_write(path, write_fn)


def atomic_write_bytes(path, blob):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
        fsync_dir(directory)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def fresh_state():
    return {
        "todos": {},
        "custom": [],
        "emails": {},
        "questions": {},
        "voice_drafts": [],
        "decisions": [],
        "events": [],
        "watches": {},
        "log": [],
    }


def state_path(cfg):
    return os.path.join(cfg.data_dir, "state.json")


def data_path(cfg):
    return os.path.join(cfg.data_dir, "data.json")


def load_state(cfg):
    path = state_path(cfg)
    if not os.path.exists(path):
        return fresh_state()
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except json.JSONDecodeError as ex:
        raise StateError("state.json is not valid JSON. Refusing to overwrite it.") from ex
    except OSError as ex:
        raise StateError("state.json could not be read. Refusing to overwrite it.") from ex
    if not isinstance(loaded, dict):
        raise StateError("state.json is not an object. Refusing to overwrite it.")
    if not isinstance(loaded.get("todos"), dict):
        loaded["todos"] = {}
    if not isinstance(loaded.get("custom"), list):
        loaded["custom"] = []
    if not isinstance(loaded.get("emails"), dict):
        loaded["emails"] = {}
    if not isinstance(loaded.get("log"), list):
        loaded["log"] = []
    if not isinstance(loaded.get("questions"), dict):
        loaded["questions"] = {}
    if not isinstance(loaded.get("voice_drafts"), list):
        loaded["voice_drafts"] = []
    if not isinstance(loaded.get("decisions"), list):
        loaded["decisions"] = []
    if not isinstance(loaded.get("events"), list):
        loaded["events"] = []
    if not isinstance(loaded.get("watches"), dict):
        loaded["watches"] = {}
    return loaded


def prune_backups(directory, today):
    """Keep dated backups for BACKUP_DAYS days, including today."""
    cutoff = today - dt.timedelta(days=BACKUP_DAYS - 1)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return
    for name in names:
        match = _DAY.fullmatch(name)
        if not match:
            continue
        try:
            day = dt.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass


def save_state(cfg, state, now=None):
    now = now or dt.datetime.now(PT)
    if now.tzinfo is None:
        now = now.replace(tzinfo=PT)
    state = dict(state)
    state["updated_at"] = now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["log"] = list(state.get("log") or [])[-500:]
    state.setdefault("todos", {})
    state.setdefault("custom", [])
    state.setdefault("emails", {})
    state.setdefault("questions", {})
    state.setdefault("voice_drafts", [])
    state.setdefault("decisions", [])
    state.setdefault("events", [])
    if not isinstance(state.get("watches"), dict):
        state["watches"] = {}
    eventlog.prune(state, now.astimezone(dt.timezone.utc))
    os.makedirs(cfg.data_dir, exist_ok=True)
    atomic_write_json(state_path(cfg), state)
    backup_dir = os.path.join(cfg.data_dir, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    atomic_write_json(os.path.join(backup_dir, f"state-{now.date().isoformat()}.json"), state)
    prune_backups(backup_dir, now.date())
    return state


def load_data(cfg):
    path = data_path(cfg)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    if cfg.sample_path and os.path.exists(cfg.sample_path):
        with open(cfg.sample_path, encoding="utf-8") as handle:
            return json.load(handle)
    return {
        "items": [],
        "calendar": [],
        "restaurants": {"city": "", "as_of": "", "eat": [], "delivery": []},
    }


def save_data(cfg, obj):
    """Replace data.json atomically and keep the previous file as data.json.prev.

    state.json is not touched.
    """
    os.makedirs(cfg.data_dir, exist_ok=True)
    path = data_path(cfg)
    if os.path.exists(path):
        with open(path, "rb") as handle:
            previous = handle.read()
        atomic_write_bytes(path + ".prev", previous)
    atomic_write_json(path, obj)


def data_dir_writable(cfg):
    try:
        os.makedirs(cfg.data_dir, exist_ok=True)
    except OSError:
        return False
    return os.path.isdir(cfg.data_dir) and os.access(cfg.data_dir, os.W_OK)
