# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The part of PhoneBridge that runs on the phone.

Nothing is installed there: the PC starts `python3` over SSH and feeds it
this file (see connection.BOOTSTRAP), so the phone always runs the agent
that belongs to the app on the PC. It needs nothing but Python and
PyGObject, which every FuriOS/Phosh phone has.

Protocol, one JSON object per line:
  PC -> phone   {"id": 7, "cmd": "sms.send", "args": {...}}
  phone -> PC   {"id": 7, "ok": true, "result": ...}
                {"id": 7, "ok": false, "error": "..."}
                {"event": "status", "data": {...}}     (whenever it changes)
                {"event": "sms", "new": [...]}         (chatty's store changed)

Sources: UPower (battery), ofono (network, mobile data, incoming SMS),
ModemManager (sending SMS - the same way chatty does), chatty's SQLite
store (the SMS history - read only), NetworkManager via nmcli (Wi-Fi),
wpctl (volume), power-profiles/batman, feedbackd (ring profile, ringing)
and GSettings (GNOME settings)."""

import base64
import binascii
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import queue
import threading
import time
import traceback

UID = os.getuid()
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % UID)
# An SSH login has no session bus of its own - use the one of the phone's
# graphical session, otherwise GSettings writes would go nowhere.
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS",
                      "unix:path=%s/bus" % os.environ["XDG_RUNTIME_DIR"])
os.environ["LC_ALL"] = "C.UTF-8"

import gi  # noqa: E402

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402
from urllib.parse import unquote, urlparse  # noqa: E402

VERSION = 1
APP_ID = "io.github.miscde.PhoneBridge"
HOME = os.path.expanduser("~")
CHATTY_DB = os.environ.get(
    "PHONEBRIDGE_CHATTY_DB",
    os.path.join(HOME, ".purple/chatty/db/chatty-history.db"))
DATA_DIR = os.environ.get(
    "PHONEBRIDGE_DATA", os.path.join(HOME, ".local/share/phonebridge"))
SENT_LOG = os.path.join(DATA_DIR, "sent.jsonl")
# Local address books keep contacts.db; synced ones (CardDAV, Google,
# Nextcloud ...) keep a cache.db with the vCards in ECacheObjects.
ADDRESSBOOKS = os.environ.get(
    "PHONEBRIDGE_ADDRESSBOOKS",
    os.pathsep.join((os.path.join(HOME, ".local/share/evolution/addressbook"),
                     os.path.join(HOME, ".cache/evolution/addressbook"))))
# where chatty keeps the files its store names by relative path
CHATTY_FILES = [os.path.join(HOME, d) for d in
                (".cache/chatty", ".local/share/chatty", ".purple/chatty")]
AVATAR_MAX = 4 * 1024 * 1024
# A message the phone sent shows up in chatty's store as well when chatty
# sent it; our own log then holds a duplicate within this many seconds.
DUPLICATE_WINDOW = 600

COMMANDS = {}
_out_lock = threading.Lock()


def command(name, deferred=False, threaded=False):
    """Registers a handler. A deferred one gets a reply(result, error)
    callback and answers later; the others return their result. A threaded
    one runs in the worker thread - for what may take long (EDS, the SMS
    store, big files), so the main loop keeps delivering calls and SMS."""
    def wrap(fn):
        COMMANDS[name] = (fn, deferred, threaded)
        return fn
    return wrap


def guarded(fn):
    """For GLib callbacks: an exception would remove a periodic source for
    good - it is printed instead, and the source keeps its return value."""
    def wrapper(*args, keep=None):
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return keep
    return wrapper


def send(obj):
    data = json.dumps(obj, ensure_ascii=False, default=str) + "\n"
    with _out_lock:
        sys.stdout.buffer.write(data.encode("utf-8"))
        sys.stdout.buffer.flush()


def run(*argv, timeout=5):
    """Output of a read-only helper program, or None."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def run_checked(*argv, timeout=10):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("%s is not installed" % argv[0])
    except subprocess.TimeoutExpired:
        raise RuntimeError("%s did not answer" % argv[0])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "%s failed" % argv[0])
    return r.stdout


# --- phone numbers ---------------------------------------------------------

def normalize(number, country="49"):
    """'0171 123-45' -> '+4917112345'; names and short codes stay as they are."""
    n = re.sub(r"[\s\-/().]", "", number or "")
    if not re.fullmatch(r"\+?\d+", n):
        return (number or "").strip()
    if n.startswith("00"):
        return "+" + n[2:]
    if n.startswith("0") and country:
        return "+" + country + n[1:]
    return n


# --- SMS store (chatty, read only) + our log of what we sent ---------------

def open_store(path=None):
    path = path or CHATTY_DB
    if not os.path.exists(path):
        return None
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    return db


def store_last_id(path=None):
    db = open_store(path)
    if db is None:
        return 0
    with db:
        return db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]


THREADS_SQL = """
SELECT t.id, t.name, t.type,
       COALESCE(NULLIF(t.alias, ''),
                (SELECT NULLIF(u.alias, '') FROM thread_members tm
                   JOIN users u ON u.id = tm.user_id
                  WHERE tm.thread_id = t.id LIMIT 1),
                t.name) AS title,
       NULLIF(t.alias, '') IS NULL AND NOT EXISTS (
           SELECT 1 FROM thread_members tm JOIN users u ON u.id = tm.user_id
            WHERE tm.thread_id = t.id AND NULLIF(u.alias, '') IS NOT NULL) AS unnamed,
       (SELECT f.path FROM files f WHERE f.id = COALESCE(t.avatar_id,
            (SELECT u.avatar_id FROM thread_members tm JOIN users u ON u.id = tm.user_id
              WHERE tm.thread_id = t.id AND u.avatar_id IS NOT NULL LIMIT 1)))
           AS avatar_path,
       m.id AS mid, m.body, m.direction, m.time
  FROM threads t
  JOIN messages m ON m.id = (SELECT id FROM messages WHERE thread_id = t.id
                              ORDER BY time DESC, id DESC LIMIT 1)
"""


# --- contacts (evolution-data-server, read only) and pictures --------------

def _unfold(text):
    """vCard lines may be folded: a continuation starts with a space or tab."""
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _vcard_line(line):
    """'TEL;TYPE=CELL:+49 171' -> ('TEL', {'TYPE': 'CELL'}, '+49 171')."""
    head, _sep, value = line.partition(":")
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        key, eq, val = p.partition("=")
        params[key.upper()] = val if eq else key    # vCard 2.1: ;JPEG
    return parts[0].split(".")[-1].upper(), params, value


def _unescape(value):
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1),
                  value)


def _decoded(data):
    try:
        raw = base64.b64decode(re.sub(r"\s", "", data), validate=False)
    except (binascii.Error, ValueError):
        return None
    return ("data", raw) if raw else None


def parse_vcard(text):
    """(name, numbers, picture) - picture is ("data", bytes), ("file", path)
    or None."""
    name, fallback, numbers, photo = "", "", [], None
    for line in _unfold(text):
        if ":" not in line:
            continue
        key, params, value = _vcard_line(line)
        if key == "FN":
            name = _unescape(value).strip()
        elif key == "N" and not fallback:
            parts = [_unescape(p).strip() for p in value.split(";")]
            fallback = " ".join(p for p in (parts[1:2] + parts[:1]) if p)
        elif key == "ORG" and not fallback:
            fallback = _unescape(value.split(";")[0]).strip()
        elif key == "TEL":
            number = value[4:] if value.lower().startswith("tel:") else value
            if number.strip():
                numbers.append(number.strip())
        elif key == "PHOTO" and photo is None:
            if value.startswith("data:"):           # vCard 4
                photo = _decoded(value.partition(",")[2])
            elif (params.get("ENCODING", "").upper() in ("B", "BASE64")
                  or "BASE64" in params):
                photo = _decoded(value)
            elif value.startswith("file://"):
                photo = ("file", unquote(urlparse(value).path))
    return name or fallback, numbers, photo


class Book:
    """Contacts with a phone number, by normalized number; read again when
    an address book changes."""

    def __init__(self, roots=None):
        self.roots = (roots or ADDRESSBOOKS).split(os.pathsep)
        self._stamp = None
        self._checked = 0.0
        self._by_number = {}
        self._lock = threading.RLock()

    def _databases(self):
        found = []
        for root in self.roots:
            for name in ("contacts.db", "cache.db"):
                found += glob.glob(os.path.join(root, "*", name))
        out = []
        for p in sorted(found):
            try:
                if os.path.getsize(p) > 0:
                    out.append(p)
            except OSError:
                pass                    # gone meanwhile (EDS rewriting it)
        return out

    def _current_stamp(self):
        out = []
        for db in self._databases():
            for p in (db, db + "-wal"):
                try:
                    st = os.stat(p)
                    out.append((p, st.st_mtime_ns, st.st_size))
                except OSError:
                    pass
        return tuple(out)

    def lookup(self, number, country="49"):
        with self._lock:
            # whether a book changed: looked at most every 2 s, not per lookup
            if time.monotonic() - self._checked > 2:
                self._checked = time.monotonic()
                stamp = self._current_stamp()
                if stamp != self._stamp:
                    self._stamp = stamp
                    self._by_number = {}
                    _KEYS.clear()
                    for db in self._databases():
                        self._read(db)
            return self._by_number.get(normalize(number, country))

    def _vcards(self, db):
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "ECacheObjects" in tables:
            # ECacheState 3: deleted here, not yet on the server
            yield from db.execute(
                "SELECT ECacheOBJ FROM ECacheObjects"
                " WHERE ECacheState IS NOT 3 AND is_list IS NOT 1")
        if "folders" in tables:
            for (table,) in db.execute("SELECT folder_id FROM folders"):
                cols = {r[1] for r in db.execute('PRAGMA table_info("%s")' % table)}
                if "vcard" in cols:
                    where = " WHERE is_list IS NOT 1" if "is_list" in cols else ""
                    yield from db.execute('SELECT vcard FROM "%s"%s' % (table, where))

    def _read(self, path):
        try:
            db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
        except sqlite3.Error:
            return
        try:
            for (vcard,) in self._vcards(db):
                if isinstance(vcard, bytes):
                    vcard = vcard.decode("utf-8", "replace")
                name, numbers, photo = parse_vcard(vcard or "")
                if photo and photo[0] == "file" and not os.path.isfile(photo[1]):
                    photo = None
                for n in numbers:
                    known = self._by_number.get(normalize(n))
                    # the same person in two books: keep the one with a picture
                    if known is None or (known[1] is None and photo is not None):
                        self._by_number[normalize(n)] = (name or (known or ("",))[0],
                                                         photo)
        except sqlite3.Error:
            pass
        finally:
            db.close()


BOOK = Book()
AVATARS = {}    # key -> ("data", bytes) or ("file", path), see cmd_avatar


def chatty_file(path):
    if not path:
        return None
    if os.path.isabs(path):
        return path if os.path.isfile(path) else None
    for d in CHATTY_FILES:
        if os.path.isfile(os.path.join(d, path)):
            return os.path.join(d, path)
    return None


_KEYS = {}      # id(picture) -> key, for the pictures the book holds


def avatar_key(picture):
    """A key for a picture that changes when the picture does - the PC
    caches by it - or None."""
    if picture is None:
        return None
    known = _KEYS.get(id(picture))
    if known is not None and known[0] is picture:
        AVATARS.setdefault(known[1], picture)
        return known[1]
    if picture[0] == "file":
        try:
            st = os.stat(picture[1])
        except OSError:
            return None
        seed = ("%s|%d|%d" % (picture[1], st.st_mtime_ns, st.st_size)).encode()
    else:
        seed = picture[1]
    key = hashlib.sha1(seed).hexdigest()[:20]
    AVATARS[key] = picture
    if picture[0] == "data":
        _KEYS[id(picture)] = (picture, key)
    return key


def picture_bytes(picture):
    if picture[0] == "data":
        return picture[1]
    with open(picture[1], "rb") as f:
        return f.read(AVATAR_MAX + 1)


def read_sent(path=None):
    path = path or SENT_LOG
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return out


SENT_KEEP_DAYS = 90


def _private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def append_sent(entry, path=None):
    """Our log of sent messages - private, and only the last 90 days."""
    path = path or SENT_LOG
    _private_dir(os.path.dirname(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        os.chmod(path, 0o600)
        if os.path.getsize(path) > 256 * 1024:
            cutoff = time.time() - SENT_KEEP_DAYS * 86400
            keep = [e for e in read_sent(path) if e.get("time", 0) >= cutoff]
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for e in keep:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
    except OSError:
        pass


def _is_duplicate(entry, outgoing):
    return any(m["body"] == entry["body"]
               and abs(m["time"] - entry["time"]) <= DUPLICATE_WINDOW
               for m in outgoing)


def _person(name, title, unnamed, avatar_path, book, country):
    """(title, avatar key): chatty's name, else the contact's; chatty's
    picture, else the contact's."""
    contact = book.lookup(name, country) if book is not None else None
    # chatty sometimes keeps the number itself as the alias - no name either
    if contact and contact[0] and (unnamed or normalize(title, country)
                                   == normalize(name, country)):
        title = contact[0]
    picture = None
    path = chatty_file(avatar_path)
    if path:
        picture = ("file", path)
    elif contact:
        picture = contact[1]
    return title, avatar_key(picture)


def list_threads(baseline=0, seen=None, country="49", store=None, sent=None,
                 book=BOOK):
    """Conversations, newest first. A message counts as unread when it came
    in after both `baseline` (the newest message when the PC first met this
    phone) and seen[thread] (the newest one the PC has shown)."""
    seen = seen or {}
    threads = {}
    db = open_store(store)
    unread = {}
    outgoing = {}
    if db is not None:
        with db:
            for r in db.execute(THREADS_SQL):
                group = r["type"] == 1
                title, avatar = (r["title"], None) if group else _person(
                    r["name"], r["title"], r["unnamed"], r["avatar_path"], book, country)
                threads[r["name"]] = {
                    "thread": r["name"], "title": title, "avatar": avatar,
                    "group": group,
                    "last": {"id": r["mid"], "body": r["body"],
                             "out": r["direction"] != 1, "time": r["time"]},
                    "unread": 0,
                }
            for r in db.execute(
                    "SELECT t.name, m.id FROM messages m JOIN threads t"
                    " ON t.id = m.thread_id WHERE m.direction = 1 AND m.id > ?",
                    (int(baseline or 0),)):
                if r["id"] > int(seen.get(r["name"], 0)):
                    unread[r["name"]] = unread.get(r["name"], 0) + 1
            for r in db.execute(
                    "SELECT t.name, m.body, m.time FROM messages m JOIN threads t"
                    " ON t.id = m.thread_id WHERE m.direction != 1 AND m.time > ?",
                    (int(time.time()) - 86400 * 30,)):
                outgoing.setdefault(normalize(r["name"], country), []).append(
                    {"body": r["body"], "time": r["time"]})
    by_number = {normalize(name, country): name for name in threads}
    for e in read_sent(sent):
        if _is_duplicate(e, outgoing.get(e["to"], [])):
            continue
        name = by_number.get(e["to"])
        if name is None:
            name = by_number[e["to"]] = e["to"]
            title, avatar = _person(name, name, True, None, book, country)
            threads[name] = {"thread": name, "title": title, "avatar": avatar,
                             "group": False, "last": None, "unread": 0}
        last = threads[name]["last"]
        if last is None or e["time"] >= last["time"]:
            threads[name]["last"] = {"id": e["id"], "body": e["body"],
                                     "out": True, "time": e["time"]}
    for name, n in unread.items():
        if name in threads:
            threads[name]["unread"] = n
    def newest(t):  # equal seconds: the later message of chatty's first
        last = t["last"]
        return last["time"], last["id"] if isinstance(last["id"], int) else 0
    return sorted(threads.values(), key=newest, reverse=True)


def list_messages(thread, limit=300, country="49", store=None, sent=None):
    """One conversation, oldest first."""
    msgs = []
    db = open_store(store)
    if db is not None:
        with db:
            for r in db.execute(
                    "SELECT m.id, m.body, m.direction, m.time, m.status"
                    "  FROM messages m JOIN threads t ON t.id = m.thread_id"
                    " WHERE t.name = ? ORDER BY m.time DESC, m.id DESC LIMIT ?",
                    (thread, int(limit))):
                msgs.append({"id": r["id"], "body": r["body"],
                             "out": r["direction"] != 1, "time": r["time"],
                             "status": r["status"]})
    number = normalize(thread, country)
    outgoing = [m for m in msgs if m["out"]]
    for e in read_sent(sent):
        if e["to"] == number and not _is_duplicate(e, outgoing):
            msgs.append({"id": e["id"], "body": e["body"], "out": True,
                         "time": e["time"], "status": e.get("status")})
    msgs.sort(key=lambda m: m["time"])
    return msgs[-int(limit):]


def new_incoming(after_id, store=None, book=BOOK):
    db = open_store(store)
    if db is None:
        return []
    with db:
        rows = db.execute(
            "SELECT m.id, t.name, m.body, m.time,"
            "       COALESCE(NULLIF(t.alias, ''), NULLIF(u.alias, ''), t.name) AS title"
            "  FROM messages m JOIN threads t ON t.id = m.thread_id"
            "  LEFT JOIN users u ON u.id = m.sender_id"
            " WHERE m.direction = 1 AND m.id > ? ORDER BY m.id", (after_id,))
        out = []
        for r in rows:
            title = r["title"]
            contact = book.lookup(r["name"]) if book is not None else None
            if contact and contact[0] and normalize(title) == normalize(r["name"]):
                title = contact[0]
            out.append({"id": r["id"], "thread": r["name"], "title": title,
                        "body": r["body"], "time": r["time"],
                        "avatar": avatar_key(contact[1]) if contact else None})
        return out


# --- D-Bus helpers ---------------------------------------------------------

def bus(kind):
    try:
        return Gio.bus_get_sync(kind, None)
    except GLib.Error:
        return None


class DBusFailure(RuntimeError):
    """A failed D-Bus call; `name` is the D-Bus error name, if any."""

    def __init__(self, message, name=None):
        super().__init__(message)
        self.name = name


def call(conn, name, path, iface, method, args=None, reply=None, timeout=5000):
    if conn is None:
        raise DBusFailure("%s is not reachable" % name, "org.freedesktop.DBus.Error.ServiceUnknown")
    try:
        v = conn.call_sync(name, path, iface, method, args,
                           GLib.VariantType(reply) if reply else None,
                           Gio.DBusCallFlags.NONE, timeout, None)
    except GLib.Error as e:
        remote = Gio.DBusError.get_remote_error(e)
        Gio.DBusError.strip_remote_error(e)
        raise DBusFailure(e.message, remote)
    return v.unpack() if v is not None else None


def get_prop(conn, name, path, iface, prop):
    try:
        return call(conn, name, path, "org.freedesktop.DBus.Properties", "Get",
                    GLib.Variant("(ss)", (iface, prop)), "(v)")[0]
    except RuntimeError:
        return None


def set_prop(conn, name, path, iface, prop, value):
    call(conn, name, path, "org.freedesktop.DBus.Properties", "Set",
         GLib.Variant("(ssv)", (iface, prop, value)))


# --- the agent -------------------------------------------------------------

BATTERY_STATES = {1: "charging", 2: "discharging", 3: "empty", 4: "full",
                  5: "pending-charge", 6: "pending-discharge"}


class Agent:
    def __init__(self, loop):
        self.loop = loop
        self.system = bus(Gio.BusType.SYSTEM)
        self.session = bus(Gio.BusType.SESSION)
        self.status = None
        self._refresh_pending = 0
        self._last_refresh = 0.0
        self._sms_pending = 0
        try:
            self.last_sms_id = store_last_id()
        except sqlite3.Error:
            self.last_sms_id = 0        # locked or being migrated: found later
        self._modem = None
        self._modem_tried = 0.0
        self.pim = Pim(self.session)
        self._calls_pending = 0
        self._slow = {}                 # what only the 30 s tick refreshes
        self._call_audio = None
        self._call_audio_tried = 0.0
        self.work = queue.Queue()
        self.files_work = queue.Queue()     # the file browser's: never behind the others
        self.last_rx = time.monotonic()
        self._watch()

    @property
    def modem(self):
        """ofono's modem - looked for again (at most every 10 s) while there
        is none: after the phone booted, ofono may come after the agent."""
        if self._modem is None and time.monotonic() - self._modem_tried > 10:
            self._modem_tried = time.monotonic()
            self._modem = self._ofono_modem()
        return self._modem

    @modem.setter
    def modem(self, value):
        self._modem = value

    # -- sources ----------------------------------------------------------
    def _ofono_modem(self):
        try:
            modems = call(self.system, "org.ofono", "/", "org.ofono.Manager",
                          "GetModems", None, "(a(oa{sv}))")[0]
        except RuntimeError:
            return None
        return modems[0][0] if modems else None

    def _ofono(self, iface):
        if not self.modem:
            return {}
        try:
            return call(self.system, "org.ofono", self.modem, "org.ofono." + iface,
                        "GetProperties", None, "(a{sv})", timeout=1500)[0]
        except RuntimeError:
            return {}

    def _mm_modem(self):
        objs = call(self.system, "org.freedesktop.ModemManager1",
                    "/org/freedesktop/ModemManager1",
                    "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                    None, "(a{oa{sa{sv}}})")[0]
        for path, ifaces in objs.items():
            if "org.freedesktop.ModemManager1.Modem.Messaging" in ifaces:
                return path
        raise RuntimeError("no modem with SMS support")

    def battery(self):
        path = "/org/freedesktop/UPower/devices/DisplayDevice"
        try:
            props = call(self.system, "org.freedesktop.UPower", path,
                         "org.freedesktop.DBus.Properties", "GetAll",
                         GLib.Variant("(s)", ("org.freedesktop.UPower.Device",)),
                         "(a{sv})")[0]
        except RuntimeError:
            return None
        if not props.get("IsPresent", True):
            return None
        return {"percent": round(props.get("Percentage", 0)),
                "state": BATTERY_STATES.get(props.get("State"), "unknown"),
                "time_to_empty": props.get("TimeToEmpty", 0),
                "time_to_full": props.get("TimeToFull", 0)}

    def network(self):
        reg = self._ofono("NetworkRegistration")
        if not reg:
            return None
        return {"status": reg.get("Status"), "operator": reg.get("Name"),
                "technology": reg.get("Technology"),
                "strength": reg.get("Strength")}

    def mobile_data(self):
        cm = self._ofono("ConnectionManager")
        return cm.get("Powered") if cm else None

    def wifi(self):
        state = run("nmcli", "-t", "-f", "WIFI", "general")
        if state is None:
            return None
        info = {"enabled": state.strip() == "enabled", "ssid": None,
                "signal": None}
        lines = run("nmcli", "-t", "-e", "no", "-f", "ACTIVE,SIGNAL,SSID",
                    "device", "wifi", "list", "--rescan", "no") or ""
        for line in lines.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "yes":
                info["signal"] = int(parts[1]) if parts[1].isdigit() else None
                info["ssid"] = parts[2]
        return info

    def volume(self):
        out = run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
        m = re.search(r"Volume:\s*([\d.]+)", out or "")
        if not m:
            return None
        return {"level": float(m.group(1)), "muted": "[MUTED]" in out}

    def power_profile(self):
        return get_prop(self.system, "org.freedesktop.UPower.PowerProfiles",
                        "/org/freedesktop/UPower/PowerProfiles",
                        "org.freedesktop.UPower.PowerProfiles", "ActiveProfile")

    def feedback_profile(self):
        return get_prop(self.session, "org.sigxcpu.Feedback",
                        "/org/sigxcpu/Feedback", "org.sigxcpu.Feedback", "Profile")

    def call_audio(self):
        """(PipeWire runs, call audio nodes there) - asked again on every
        slow refresh (30 s): PipeWire can stop or crash at any time."""
        self._call_audio = pipewire_state()
        return self._call_audio

    def collect(self, slow=False):
        """The status; Wi-Fi, volume and profiles (helper programs, the
        session bus) only on the 30 s tick or when asked - a burst of ofono
        signals must not run them over and over."""
        if slow or not self._slow:
            self._slow = {"wifi": self.wifi(), "volume": self.volume(),
                          "power_profile": self.power_profile(),
                          "feedback_profile": self.feedback_profile()}
            self._slow["pipewire"], self._slow["call_audio"] = self.call_audio()
        return dict({"hostname": socket.gethostname(), "battery": self.battery(),
                     "network": self.network(), "mobile_data": self.mobile_data()},
                    **self._slow)

    # -- watching ---------------------------------------------------------
    def _watch(self):
        # only the signals that mean something here - ofono can be chatty
        if self.system is not None:
            for sender, iface, member in (
                    ("org.freedesktop.UPower", "org.freedesktop.DBus.Properties",
                     "PropertiesChanged"),
                    ("org.freedesktop.UPower.PowerProfiles",
                     "org.freedesktop.DBus.Properties", "PropertiesChanged"),
                    ("org.ofono", "org.ofono.NetworkRegistration", "PropertyChanged"),
                    ("org.ofono", "org.ofono.ConnectionManager", "PropertyChanged"),
                    ("org.ofono", "org.ofono.VoiceCallManager", "CallAdded"),
                    ("org.ofono", "org.ofono.VoiceCallManager", "CallRemoved"),
                    ("org.ofono", "org.ofono.VoiceCall", "PropertyChanged"),
                    ("org.ofono", "org.ofono.MessageManager", "IncomingMessage"),
                    ("org.ofono", "org.ofono.Manager", "ModemAdded"),
                    ("org.ofono", "org.ofono.Manager", "ModemRemoved")):
                self.system.signal_subscribe(
                    sender, iface, member, None, None, Gio.DBusSignalFlags.NONE,
                    self._on_signal)
        if self.session is not None:
            self.session.signal_subscribe(
                "org.sigxcpu.Feedback", "org.freedesktop.DBus.Properties",
                "PropertiesChanged", None, None, Gio.DBusSignalFlags.NONE,
                self._on_signal)
        store = Gio.File.new_for_path(CHATTY_DB)
        try:
            self._monitor = store.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", lambda *a: self.schedule_sms_check())
        except GLib.Error:
            self._monitor = None
        GLib.timeout_add_seconds(30, self._tick)
        GLib.timeout_add_seconds(20, self._sms_tick)
        GLib.timeout_add_seconds(10, self._watchdog)
        self._vb_signature = voicebox_signature()
        self._vb_pending = 0
        try:
            self._vb_monitor = Gio.File.new_for_path(voicebox_dir()).monitor_directory(
                Gio.FileMonitorFlags.NONE, None)
            self._vb_monitor.connect("changed", lambda *a: self.schedule_voicebox_check())
        except GLib.Error:
            self._vb_monitor = None
        GLib.timeout_add_seconds(30, lambda: guarded(self.check_voicebox)(keep=True) or True)

    def active_calls(self):
        if not self.modem:
            return []
        try:
            calls = call(self.system, "org.ofono", self.modem, "org.ofono.VoiceCallManager",
                         "GetCalls", None, "(a(oa{sv}))")[0]
        except RuntimeError:
            return []
        out = []
        for path, props in calls:
            number = props.get("LineIdentification", "")
            contact = BOOK.lookup(number) if number and number != "withheld" else None
            out.append({"path": path, "state": props.get("State", ""), "number": number,
                        "name": (contact[0] if contact else "") or props.get("Name", ""),
                        "avatar": avatar_key(contact[1]) if contact else None})
        return out

    def _watchdog(self):
        """The PC pings every 20 s. Nothing for 75 s: it is gone (suspended,
        out of Wi-Fi) without the connection noticing - give the phone's
        microphone back and end, instead of keeping it muted for minutes."""
        if time.monotonic() - self.last_rx > 75:
            pc_audio_release(self)
            self.loop.quit()
            return False
        return True

    def _send_calls(self):
        self._calls_pending = 0
        try:
            calls = self.active_calls()
        except Exception:  # noqa: BLE001 - the call event must go out
            traceback.print_exc()
            calls = []
        if not calls:
            pc_audio_release(self)      # the call is over: the microphone as it was
        send({"event": "calls", "calls": calls})
        return False

    def _on_signal(self, conn, sender, path, iface, signal, params):
        if iface == "org.ofono.Manager":
            self.modem = None           # modems came or went: look again
            self._modem_tried = 0.0
        if iface in ("org.ofono.VoiceCallManager", "org.ofono.VoiceCall"):
            if not self._calls_pending:
                self._calls_pending = GLib.timeout_add(150, self._send_calls)
        if iface == "org.ofono.MessageManager" and signal == "IncomingMessage":
            # chatty stores it a moment later; the file monitor catches that
            # too, this is the safety net when it does not fire.
            GLib.timeout_add_seconds(3, lambda: guarded(self.check_sms)() and False)
        if iface not in ("org.ofono.VoiceCall", "org.ofono.MessageManager"):
            self.schedule_refresh()

    def _tick(self):
        self._slow_due = True
        self.schedule_refresh()
        return True

    def _sms_tick(self):
        guarded(self.check_sms)()
        return True

    def schedule_refresh(self, delay=800, slow=False):
        """At most one status refresh every 3 s, whatever signals come."""
        if slow:
            self._slow_due = True
        if not self._refresh_pending:
            wait = max(delay, int((self._last_refresh + 3 - time.monotonic()) * 1000))
            self._refresh_pending = GLib.timeout_add(max(0, wait), self._refresh)

    def _refresh(self):
        self._refresh_pending = 0
        self._last_refresh = time.monotonic()
        slow, self._slow_due = getattr(self, "_slow_due", False), False
        try:
            status = self.collect(slow=slow)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return False
        if status != self.status:
            self.status = status
            send({"event": "status", "data": status})
        return False

    def schedule_voicebox_check(self):
        if not self._vb_pending:
            self._vb_pending = GLib.timeout_add(600, self._voicebox_once)

    def _voicebox_once(self):
        self._vb_pending = 0
        guarded(self.check_voicebox)()
        return False

    def check_voicebox(self):
        sig = voicebox_signature()
        if sig != self._vb_signature:
            self._vb_signature = sig
            send({"event": "voicebox"})
        return False

    def schedule_sms_check(self):
        if not self._sms_pending:
            self._sms_pending = GLib.timeout_add(700, self._sms_check_once)

    def _sms_check_once(self):
        self._sms_pending = 0
        guarded(self.check_sms)()
        return False

    def check_sms(self):
        try:
            last = store_last_id()
        except sqlite3.Error:
            return False
        if last != self.last_sms_id:
            new = new_incoming(self.last_sms_id) if last > self.last_sms_id else []
            self.last_sms_id = last
            send({"event": "sms", "new": new})
        return False

    # -- requests ---------------------------------------------------------
    def dispatch(self, line):
        try:
            req = json.loads(line)
        except ValueError:
            return False
        rid = req.get("id")
        entry = COMMANDS.get(req.get("cmd"))

        def reply(result=None, error=None):
            if error is not None:
                send({"id": rid, "ok": False, "error": str(error)})
            else:
                send({"id": rid, "ok": True, "result": result})

        if entry is None:
            reply(error="unknown command: %s" % req.get("cmd"))
            return False
        fn, deferred, threaded = entry
        if threaded:
            q = self.files_work if threaded == "files" else self.work
            q.put((fn, req.get("args") or {}, reply))
            return False
        try:
            if deferred:
                fn(self, req.get("args") or {}, reply)
            else:
                reply(fn(self, req.get("args") or {}))
        except Exception as e:  # noqa: BLE001 - every failure goes back to the PC
            reply(error=e)
        return False


# --- commands --------------------------------------------------------------

@command("ping")
def cmd_ping(agent, args):
    return "pong"


@command("hello")
def cmd_hello(agent, args):
    return {"version": VERSION, "hostname": socket.gethostname(),
            "sms_last_id": agent.last_sms_id,
            "has": {"sms": os.path.exists(CHATTY_DB), "voicebox": voicebox_installed(),
                    "call_audio": call_audio_nodes(),
                    "pipewire": pipewire_state()[0],
                    "ofono": agent.modem is not None,
                    "calls": run("sh", "-c", "command -v gnome-calls") is not None}}


@command("status")
def cmd_status(agent, args):
    agent.status = agent.collect()
    return agent.status


@command("sms.threads", threaded=True)
def cmd_threads(agent, args):
    return list_threads(args.get("baseline", 0), args.get("seen"),
                        args.get("country", "49"))


@command("sms.messages", threaded=True)
def cmd_messages(agent, args):
    return list_messages(args["thread"], args.get("limit", 300),
                         args.get("country", "49"))


@command("avatar", threaded=True)
def cmd_avatar(agent, args):
    """The picture behind a key from sms.threads, base64."""
    picture = AVATARS.get(args["key"])
    if picture is None:
        raise RuntimeError("unknown picture")
    try:
        raw = picture_bytes(picture)
    except OSError as e:
        raise RuntimeError(str(e))
    if len(raw) > AVATAR_MAX:
        raise RuntimeError("picture too large")
    return {"data": base64.b64encode(raw).decode("ascii")}


@command("sms.send", deferred=True)
def cmd_send(agent, args, reply):
    to = normalize(args["to"], args.get("country", "49"))
    body = args["body"]
    if not to or not body.strip():
        raise RuntimeError("number and text are needed")
    modem = agent._mm_modem()
    props = GLib.Variant("(a{sv})", ({"number": GLib.Variant("s", to),
                                      "text": GLib.Variant("s", body)},))
    sms = call(agent.system, "org.freedesktop.ModemManager1", modem,
               "org.freedesktop.ModemManager1.Modem.Messaging", "Create",
               props, "(o)")[0]

    def done(conn, res):
        entry = {"id": "s%d" % int(time.time() * 1000), "to": to, "body": body,
                 "time": int(time.time())}
        try:
            conn.call_finish(res)
        except GLib.Error as e:
            Gio.DBusError.strip_remote_error(e)
            reply(error=e.message)
            return
        finally:
            try:
                call(conn, "org.freedesktop.ModemManager1", modem,
                     "org.freedesktop.ModemManager1.Modem.Messaging", "Delete",
                     GLib.Variant("(o)", (sms,)))
            except RuntimeError:
                pass
        entry["status"] = "sent"
        append_sent(entry)
        reply(entry)
        send({"event": "sms", "new": []})

    agent.system.call("org.freedesktop.ModemManager1", sms,
                      "org.freedesktop.ModemManager1.Sms", "Send", None, None,
                      Gio.DBusCallFlags.NONE, 90000, None, done)


def _schema_key(schema, key):
    source = Gio.SettingsSchemaSource.get_default()
    s = source.lookup(schema, True) if source else None
    if s is None or not s.has_key(key):
        return None, None
    return s, s.get_key(key)


def _settings(schema, path=None):
    """Gio.Settings of a schema - at `path` for a relocatable one (the
    notification settings of one app, say)."""
    return Gio.Settings.new_with_path(schema, path) if path else Gio.Settings.new(schema)


def _describe(schema, key, settings=None, path=None):
    s, k = _schema_key(schema, key)
    if k is None:
        return None
    if s.get_path() is None and not path:
        return None                     # relocatable, but no path given
    settings = settings or _settings(schema, path)
    value = settings.get_value(key)
    rng = k.get_range().unpack()
    return {"schema": schema, "key": key, "value": value.unpack(),
            "text": value.print_(False), "type": k.get_value_type().dup_string(),
            "range": list(rng), "summary": k.get_summary() or "",
            "description": k.get_description() or "",
            "default": not settings.get_user_value(key)}


def key_id(entry):
    """"schema key" or "schema key path" - how gsettings.get names a key."""
    return " ".join(entry)


@command("gsettings.get")
def cmd_gs_get(agent, args):
    out = {}
    for entry in args["keys"]:
        schema, key = entry[0], entry[1]
        path = entry[2] if len(entry) > 2 else None
        out[key_id(entry)] = _describe(schema, key, path=path)
    return out


@command("gsettings.set")
def cmd_gs_set(agent, args):
    schema, key, path = args["schema"], args["key"], args.get("path")
    s, k = _schema_key(schema, key)
    if k is None:
        raise RuntimeError("no such setting: %s %s" % (schema, key))
    vtype = k.get_value_type().dup_string()
    if "text" in args:
        try:
            value = GLib.Variant.parse(GLib.VariantType(vtype), args["text"])
        except GLib.Error as e:
            raise RuntimeError(e.message)
    else:
        v = args["value"]
        if vtype == "d":
            v = float(v)
        elif vtype in ("i", "u", "x", "t", "n", "q", "y"):
            v = int(v)
        value = GLib.Variant(vtype, v)
    if not k.range_check(value):
        raise RuntimeError("value out of range")
    settings = _settings(schema, path)
    if not settings.set_value(key, value):
        raise RuntimeError("the setting is read only")
    Gio.Settings.sync()
    return _describe(schema, key, settings, path)


@command("gsettings.reset")
def cmd_gs_reset(agent, args):
    settings = _settings(args["schema"], args.get("path"))
    settings.reset(args["key"])
    Gio.Settings.sync()
    return _describe(args["schema"], args["key"], settings, args.get("path"))


NOTIFY_APP_SCHEMA = "org.gnome.desktop.notifications.application"
NOTIFY_APP_KEYS = ("enable", "show-banners", "enable-sound-alerts", "show-in-lock-screen",
                   "details-in-lock-screen", "force-expanded")


def _desktop_app_info(app_id):
    """GioUnix.DesktopAppInfo where GLib has it (2.86), else Gio's - quietly."""
    global _DESKTOP_APP_INFO
    if _DESKTOP_APP_INFO is None:
        try:
            gi.require_version("GioUnix", "2.0")
            from gi.repository import GioUnix
            _DESKTOP_APP_INFO = GioUnix.DesktopAppInfo
        except (ImportError, ValueError):
            _DESKTOP_APP_INFO = Gio.DesktopAppInfo
    return _DESKTOP_APP_INFO.new(app_id)


_DESKTOP_APP_INFO = None


@command("notifications.apps")
def cmd_notification_apps(agent, args):
    """The apps that have shown notifications, with their notification
    settings - GNOME keeps them per app under a path of their own."""
    if _schema_key("org.gnome.desktop.notifications", "application-children")[1] is None:
        return []
    children = Gio.Settings.new("org.gnome.desktop.notifications").get_strv(
        "application-children")
    out = []
    for child in children:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", child):
            continue
        path = "/org/gnome/desktop/notifications/application/%s/" % child
        st = _settings(NOTIFY_APP_SCHEMA, path)
        app_id = st.get_string("application-id")
        name = app_id[:-8] if app_id.endswith(".desktop") else app_id or child
        try:
            info = _desktop_app_info(app_id) if app_id else None
        except TypeError:
            info = None
        if info is not None:
            name = info.get_name() or name
        out.append({"child": child, "path": path, "app_id": app_id, "name": name,
                    "installed": info is not None,
                    "values": {k: st.get_boolean(k) for k in NOTIFY_APP_KEYS}})
    out.sort(key=lambda a: a["name"].lower())
    return out


@command("gsettings.search")
def cmd_gs_search(agent, args):
    words = (args.get("query") or "").lower().split()
    limit = int(args.get("limit", 200))
    source = Gio.SettingsSchemaSource.get_default()
    found = []
    schemas = source.list_schemas(True)[0] if source else []
    for schema in sorted(schemas):
        s = source.lookup(schema, True)
        settings = None
        for key in sorted(s.list_keys()):
            k = s.get_key(key)
            hay = " ".join((schema, key, k.get_summary() or "")).lower()
            if all(w in hay for w in words):
                settings = settings or Gio.Settings.new(schema)
                found.append(_describe(schema, key, settings))
                if len(found) >= limit:
                    return found
    return found


@command("volume.set")
def cmd_volume(agent, args):
    if "level" in args:
        level = max(0.0, min(1.0, float(args["level"])))
        run_checked("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "%.2f" % level)
    if "muted" in args:
        run_checked("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@",
                    "1" if args["muted"] else "0")
    agent.schedule_refresh(100, slow=True)
    return agent.volume()


@command("data.set")
def cmd_data(agent, args):
    if not agent.modem:
        raise RuntimeError("no modem")
    call(agent.system, "org.ofono", agent.modem, "org.ofono.ConnectionManager",
         "SetProperty", GLib.Variant("(sv)", ("Powered",
                                              GLib.Variant("b", bool(args["on"])))))
    agent.schedule_refresh(300)
    return bool(args["on"])


@command("wifi.set")
def cmd_wifi(agent, args):
    run_checked("nmcli", "radio", "wifi", "on" if args["on"] else "off")
    agent.schedule_refresh(300, slow=True)
    return bool(args["on"])


@command("power.set")
def cmd_power(agent, args):
    set_prop(agent.system, "org.freedesktop.UPower.PowerProfiles",
             "/org/freedesktop/UPower/PowerProfiles",
             "org.freedesktop.UPower.PowerProfiles", "ActiveProfile",
             GLib.Variant("s", args["profile"]))
    agent.schedule_refresh(300, slow=True)
    return args["profile"]


@command("feedback.set")
def cmd_feedback(agent, args):
    set_prop(agent.session, "org.sigxcpu.Feedback", "/org/sigxcpu/Feedback",
             "org.sigxcpu.Feedback", "Profile", GLib.Variant("s", args["profile"]))
    agent.schedule_refresh(300, slow=True)
    return args["profile"]


@command("ring")
def cmd_ring(agent, args):
    """Rings like an incoming call - to find the phone."""
    return call(agent.session, "org.sigxcpu.Feedback", "/org/sigxcpu/Feedback",
                "org.sigxcpu.Feedback", "TriggerFeedback",
                GLib.Variant("(ssa{sv}i)", (APP_ID, "phone-incoming-call",
                                            {"profile": GLib.Variant("s", "full")},
                                            int(args.get("seconds", 15)))),
                "(u)")[0]


@command("ring.stop")
def cmd_ring_stop(agent, args):
    call(agent.session, "org.sigxcpu.Feedback", "/org/sigxcpu/Feedback",
         "org.sigxcpu.Feedback", "EndFeedback", GLib.Variant("(u)", (args["id"],)))
    return True


DIALABLE = re.compile(r"\+?[0-9*#]+")


@command("call")
def cmd_call(agent, args):
    """Calls a number - on a chosen line (lines.list), or as GNOME Calls
    would by default. Only digits, * and # (and a leading +) are dialled -
    a name from an SMS sender must not turn into an option."""
    number = normalize(args["number"], args.get("country", "49"))
    if not DIALABLE.fullmatch(number):
        raise RuntimeError("not a number to call")
    if args.get("line"):
        dial_on_line(agent, number, args["line"])
        return number
    env = dict(os.environ)
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    subprocess.Popen(["gnome-calls", "--dial=" + number], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return number


# --- iCalendar and vCard text ------------------------------------------------

def _escape(value):
    return (value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n")
            .replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;"))


def _one_line(value):
    """Line breaks out of a value that must stay one line (a number, an
    address) - they would start a property of their own."""
    return re.sub(r"[\r\n]+", " ", value or "").strip()


def _prop(line):
    """'DTSTART;TZID=Europe/Berlin:20261007T100000' -> name, params, value.
    Quoted parameter values may hold ':' and ';'."""
    i, quoted = 0, False
    while i < len(line):
        c = line[i]
        if c == '"':
            quoted = not quoted
        elif c == ":" and not quoted:
            break
        i += 1
    head, value = line[:i], line[i + 1:]
    parts = re.findall(r'(?:[^;"]|"[^"]*")+', head)
    params = {}
    for p in parts[1:]:
        k, _eq, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return (parts[0] if parts else "").split(".")[-1].upper(), params, value


def _fold(line):
    """Lines longer than 75 octets are folded, as RFC 5545/6350 want."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def components(text, kind):
    """The BEGIN:<kind> ... END:<kind> blocks of an iCalendar text, as line
    lists (unfolded); nested blocks (VALARM) stay inside."""
    out, cur, depth = [], None, 0
    for line in _unfold(text):
        if line == "BEGIN:" + kind and cur is None:
            cur, depth = [line], 0
            continue
        if cur is not None:
            cur.append(line)
            if line.startswith("BEGIN:"):
                depth += 1
            elif line.startswith("END:"):
                if depth == 0 and line == "END:" + kind:
                    out.append(cur)
                    cur = None
                else:
                    depth -= 1
    return out


# --- contacts ------------------------------------------------------------------

PHONE_TYPES = {"mobile": "CELL", "home": "HOME,VOICE", "work": "WORK,VOICE",
               "other": "VOICE"}
# what the editor owns in a vCard; everything else stays as it was
CONTACT_OWNED = {"FN", "N", "ORG", "TEL", "EMAIL", "BDAY", "NOTE",
                 "X-EVOLUTION-FILE-AS"}


def _phone_type(head):
    h = head.upper()
    if "CELL" in h or "MOBILE" in h:
        return "mobile"
    if "WORK" in h:
        return "work"
    if "HOME" in h:
        return "home"
    return "other"


def contact_from_vcard(text):
    c = {"uid": "", "name": "", "given": "", "family": "", "org": "",
         "phones": [], "emails": [], "birthday": "", "note": "", "avatar": None}
    photo = None
    for line in _unfold(text):
        if ":" not in line:
            continue
        key, params, value = _prop(line)
        if key == "UID":
            c["uid"] = value
        elif key == "FN":
            c["name"] = _unescape(value).strip()
        elif key == "N":
            parts = [_unescape(p).strip() for p in re.split(r"(?<!\\);", value)]
            c["family"] = parts[0] if parts else ""
            c["given"] = parts[1] if len(parts) > 1 else ""
        elif key == "ORG":
            c["org"] = _unescape(re.split(r"(?<!\\);", value)[0]).strip()
        elif key == "TEL":
            number = value[4:] if value.lower().startswith("tel:") else value
            if number.strip():
                c["phones"].append({"type": _phone_type(line.split(":", 1)[0]),
                                    "value": number.strip()})
        elif key == "EMAIL" and value.strip():
            c["emails"].append(value.strip())
        elif key == "BDAY":
            m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", value)
            c["birthday"] = "%s-%s-%s" % m.groups() if m else ""
        elif key == "NOTE":
            c["note"] = _unescape(value)
    _n, _nums, photo = parse_vcard(text)
    if photo and photo[0] == "file" and not os.path.isfile(photo[1]):
        photo = None
    c["avatar"] = avatar_key(photo)
    if not c["name"]:
        c["name"] = " ".join(p for p in (c["given"], c["family"]) if p) or c["org"]
    return c


def vcard_from_contact(c, original=None, photo=None):
    """A vCard with the editor's fields; from `original` everything else is
    kept (UID, ETag, addresses, extra fields). photo: None keeps the picture,
    "" removes it, else (mime, bytes) sets it."""
    lines = _unfold(original) if original else ["BEGIN:VCARD", "VERSION:3.0", "END:VCARD"]
    owned = CONTACT_OWNED | ({"PHOTO"} if photo is not None else set())
    kept = [l for l in lines
            if l != "END:VCARD" and not (":" in l and _prop(l)[0] in owned)]
    given, family = c.get("given", "").strip(), c.get("family", "").strip()
    org = c.get("org", "").strip()
    name = " ".join(p for p in (given, family) if p) or org or c.get("name", "").strip()
    new = ["FN:" + _escape(name),
           "N:%s;%s;;;" % (_escape(family), _escape(given)),
           "X-EVOLUTION-FILE-AS:" + _escape(
               ", ".join(p for p in (family, given) if p) or name)]
    if org:
        new.append("ORG:" + _escape(org))
    for p in c.get("phones", []):
        if p.get("value", "").strip():
            new.append("TEL;TYPE=%s:%s" % (PHONE_TYPES.get(p.get("type"), "VOICE"),
                                           _one_line(p["value"])))
    for e in c.get("emails", []):
        if e.strip():
            new.append("EMAIL;TYPE=INTERNET:" + _one_line(e))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", c.get("birthday") or ""):
        new.append("BDAY:" + c["birthday"])
    if c.get("note", "").strip():
        new.append("NOTE:" + _escape(c["note"].strip()))
    if photo:
        mime, raw = photo
        kind = "PNG" if "png" in mime else "JPEG"
        new.append("PHOTO;ENCODING=b;TYPE=%s:%s" % (kind, base64.b64encode(raw).decode()))
    return "\r\n".join(_fold(l) for l in kept + new + ["END:VCARD"]) + "\r\n"


# --- events ----------------------------------------------------------------------

def local_zone_name():
    """The phone's time zone, as an Olson name."""
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            name = f.read().strip()
            if name:
                return name
    except OSError:
        pass
    try:
        link = os.path.realpath("/etc/localtime")
        m = re.search(r"zoneinfo/(.+)$", link)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "UTC"


def _zone(tzid):
    from zoneinfo import ZoneInfo
    if tzid:
        m = re.search(r"([A-Za-z]+(?:/[A-Za-z0-9_+\-]+)+|UTC)$", tzid)
        if m:
            try:
                return ZoneInfo(m.group(1))
            except Exception:  # noqa: BLE001 - unknown zone: the phone's own
                pass
    try:
        return ZoneInfo(local_zone_name())
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def parse_time(value, params):
    """-> ("date", date) or ("time", aware datetime)."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return "date", dt.date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    naive = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return "time", naive.replace(tzinfo=ZoneInfo("UTC"))
    return "time", naive.replace(tzinfo=_zone(params.get("TZID")))


def _duration(text):
    import datetime as dt
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
                     text.strip())
    if not m:
        return None
    sign, w, d, h, mi, s = m.groups()
    delta = dt.timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0),
                         minutes=int(mi or 0), seconds=int(s or 0))
    return -delta if sign == "-" else delta


WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _nth_weekdays(year, month, spec):
    """'2MO' / '-1FR' / 'TU' -> the matching days of that month."""
    import calendar
    import datetime as dt
    m = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", spec)
    if not m:
        return []
    wd = WEEKDAYS.index(m.group(2))
    days = [d for d in range(1, calendar.monthrange(year, month)[1] + 1)
            if dt.date(year, month, d).weekday() == wd]
    if m.group(1):
        n = int(m.group(1))
        return [days[n - 1 if n > 0 else n]] if -len(days) <= n <= len(days) and n else []
    return days


def recurrences(start, rule, until_limit, exdates=(), max_steps=20000):
    """Start values (date or naive datetime, as `start`) of a recurring
    event, from `start` up to `until_limit` (same kind). Daily, weekly,
    monthly and yearly rules with INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY
    and BYMONTH - what calendars write in practice."""
    import calendar
    import datetime as dt
    r = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = r.get("FREQ", "")
    interval = max(1, int(r.get("INTERVAL", "1") or 1))
    count = int(r["COUNT"]) if r.get("COUNT", "").isdigit() else None
    until = None
    if r.get("UNTIL"):
        kind, u = parse_time(r["UNTIL"], {})
        if isinstance(start, dt.datetime):
            until = (u if kind == "time" else dt.datetime(u.year, u.month, u.day, 23, 59, 59,
                                                          tzinfo=start.tzinfo))
            if until.tzinfo is not None and start.tzinfo is not None:
                until = until.astimezone(start.tzinfo)
            until = until.replace(tzinfo=None)
        else:
            until = u if kind == "date" else u.date()
    is_time = isinstance(start, dt.datetime)
    naive_start = start.replace(tzinfo=None) if is_time else start
    byday = [d for d in r.get("BYDAY", "").split(",") if d]
    bymonthday = [int(d) for d in r.get("BYMONTHDAY", "").split(",") if d.lstrip("-").isdigit()]
    bymonth = [int(m) for m in r.get("BYMONTH", "").split(",") if m.isdigit()]
    excluded = set(exdates)

    def at(day):
        if is_time:
            return dt.datetime.combine(day, naive_start.time())
        return day

    def candidates():
        base = naive_start.date() if is_time else naive_start
        step = 0
        while step < max_steps:
            if freq == "DAILY":
                yield [base + dt.timedelta(days=step * interval)]
            elif freq == "WEEKLY":
                week = base - dt.timedelta(days=base.weekday()) + dt.timedelta(weeks=step * interval)
                days = [WEEKDAYS.index(d[-2:]) for d in byday if d[-2:] in WEEKDAYS] \
                    or [base.weekday()]
                yield [week + dt.timedelta(days=d) for d in sorted(set(days))]
            elif freq in ("MONTHLY", "YEARLY"):
                if freq == "MONTHLY":
                    months = [(base.year * 12 + base.month - 1 + step * interval)]
                    months = [(m // 12, m % 12 + 1) for m in months]
                else:
                    year = base.year + step * interval
                    months = [(year, m) for m in (bymonth or [base.month])]
                out = []
                for year, month in months:
                    last = calendar.monthrange(year, month)[1]
                    if byday:
                        days = sorted({d for spec in byday for d in _nth_weekdays(year, month, spec)})
                    else:
                        days = []
                        for md in (bymonthday or [base.day]):
                            d = md if md > 0 else last + 1 + md
                            if 1 <= d <= last:
                                days.append(d)
                    out += [dt.date(year, month, d) for d in sorted(days)]
                yield out
            else:
                yield [base] if step == 0 else []
                return
            step += 1

    n = 0
    for group in candidates():
        for day in group:
            value = at(day)
            if value < naive_start:
                continue
            if until is not None and value > until:
                return
            if count is not None and n >= count:
                return
            n += 1
            if value > until_limit:
                return
            if value not in excluded:
                yield value.replace(tzinfo=start.tzinfo) if is_time else value
    return


def _alarm_minutes(lines):
    for i, line in enumerate(lines):
        if line.startswith("TRIGGER"):
            key, params, value = _prop(line)
            d = _duration(value)
            if d is not None and params.get("RELATED", "START") == "START":
                return int(-d.total_seconds() // 60)
    return None


def event_from_lines(lines):
    """The fields of one VEVENT (its own lines; alarms read separately)."""
    ev = {"uid": "", "summary": "", "location": "", "description": "", "rrule": "",
          "exdates": [], "rid": "", "start": None, "end": None, "duration": None,
          "status": ""}
    depth = 0
    alarm_lines = []
    for line in lines[1:-1]:
        if line.startswith("BEGIN:"):
            depth += 1
            continue
        if line.startswith("END:"):
            depth -= 1
            continue
        if depth:
            alarm_lines.append(line)
            continue
        if ":" not in line:
            continue
        key, params, value = _prop(line)
        if key == "UID":
            ev["uid"] = value
        elif key in ("SUMMARY", "LOCATION", "DESCRIPTION"):
            ev[key.lower()] = _unescape(value)
        elif key == "DTSTART":
            ev["start"] = parse_time(value, params)
        elif key == "DTEND":
            ev["end"] = parse_time(value, params)
        elif key == "DURATION":
            ev["duration"] = _duration(value)
        elif key == "RRULE":
            ev["rrule"] = value
        elif key == "EXDATE":
            for v in value.split(","):
                ev["exdates"].append(parse_time(v, params))
        elif key == "RECURRENCE-ID":
            ev["rid"] = value
            ev["rid_time"] = parse_time(value, params)
        elif key == "STATUS":
            ev["status"] = value.upper()
    ev["alarm"] = _alarm_minutes(alarm_lines)
    return ev


def _end_of(ev):
    import datetime as dt
    kind, start = ev["start"]
    if ev["end"] is not None:
        return ev["end"][1]
    if ev["duration"] is not None:
        return start + ev["duration"]
    return start + dt.timedelta(days=1) if kind == "date" else start


def _rid_text(value):
    if hasattr(value, "hour"):
        from zoneinfo import ZoneInfo
        return value.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    return value.strftime("%Y%m%d")


def expand_events(ics_objects, range_start, range_end):
    """Occurrences between two epoch times: (fields, start, end) with start
    and end as date (all-day) or aware datetime."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    utc = ZoneInfo("UTC")
    lo = dt.datetime.fromtimestamp(range_start, utc)
    hi = dt.datetime.fromtimestamp(range_end, utc)
    events = []
    for text in ics_objects:
        events += [event_from_lines(c) for c in components(text, "VEVENT")]
    overridden = {}
    for ev in events:
        if ev["rid"]:
            overridden.setdefault(ev["uid"], set()).add(_rid_text(ev["rid_time"][1]))
    out = []
    for ev in events:
        if ev["start"] is None or ev["status"] == "CANCELLED":
            continue
        kind, start = ev["start"]
        length = _end_of(ev) - start
        if ev["rrule"] and not ev["rid"]:
            if kind == "date":
                limit = hi.date()
                ex = [v for k, v in ev["exdates"] if k == "date"]
            else:
                limit = hi.astimezone(start.tzinfo).replace(tzinfo=None)
                ex = [(v.astimezone(start.tzinfo).replace(tzinfo=None) if k == "time" else
                       dt.datetime.combine(v, start.time())) for k, v in ev["exdates"]]
            starts = recurrences(start, ev["rrule"], limit, ex)
        else:
            starts = [start]
        skip = overridden.get(ev["uid"], set()) if not ev["rid"] else set()
        for s in starts:
            e = s + length
            if kind == "date":
                if e <= lo.date() or s > hi.date():
                    continue
            elif e < lo or s > hi:
                continue
            rid = ev["rid"] or (_rid_text(s) if ev["rrule"] else "")
            if ev["rrule"] and _rid_text(s) in skip:
                continue
            out.append((ev, s, e, rid))
    return out


def _ical_value(kind, value, zone_name):
    """DTSTART/DTEND parameters and value for a date or an epoch time."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    if kind == "date":
        return ";VALUE=DATE", value.replace("-", "")
    t = dt.datetime.fromtimestamp(value, ZoneInfo(zone_name))
    if zone_name == "UTC":
        return "", t.strftime("%Y%m%dT%H%M%SZ")
    return ";TZID=" + zone_name, t.strftime("%Y%m%dT%H%M%S")


ZONE_RE = r"([A-Za-z]+(?:/[A-Za-z0-9_+\-]+)+|UTC)$"


def event_zone(lines):
    """The zone an event's times are written in: its TZID, "UTC" for times
    ending in Z, else (dates, floating times) the phone's."""
    for line in lines:
        if line.startswith("DTSTART"):
            key, params, value = _prop(line)
            if params.get("TZID"):
                m = re.search(ZONE_RE, params["TZID"])
                if m:
                    return m.group(1)
            if value.strip().endswith("Z"):
                return "UTC"
    return local_zone_name()


def add_exdate(master_lines, occurrence_start):
    """The master VEVENT with one more EXDATE, written like its DTSTART -
    takes one occurrence out of a series."""
    old = event_from_lines(master_lines)
    if old["start"][0] == "date":
        day = (occurrence_start if isinstance(occurrence_start, str) else
               time.strftime("%Y-%m-%d", time.localtime(occurrence_start)))
        line = "EXDATE;VALUE=DATE:" + day.replace("-", "")
    else:
        zone = event_zone(master_lines)
        if isinstance(occurrence_start, str):
            raise RuntimeError("expected a time for this event")
        p, v = _ical_value("time", occurrence_start, zone)
        line = "EXDATE%s:%s" % (p, v)
    return master_lines[:-1] + [line, "END:VEVENT"]


def _shift_times(line, delta):
    """An EXDATE (or similar) line with every time moved by delta, written
    as before."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    key, params, value = _prop(line)
    head = line[:len(line) - len(value) - 1]
    out = []
    for v in value.split(","):
        kind, t = parse_time(v, params)
        if kind == "date":
            out.append((t + dt.timedelta(days=delta.days or round(delta.total_seconds() / 86400)))
                       .strftime("%Y%m%d"))
        elif v.strip().endswith("Z"):
            out.append((t + delta).astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ"))
        else:
            # same wall-clock arithmetic as the series: local time + delta
            moved = (t.replace(tzinfo=None) + delta).replace(tzinfo=t.tzinfo)
            out.append(moved.strftime("%Y%m%dT%H%M%S"))
    return head + ":" + ",".join(out)


def vevent_from_fields(f, original=None, shift=None):
    """A VEVENT with the editor's fields. From `original` (a VEVENT text)
    everything else stays: recurrence, attendees, ETag, other alarms.
    f: summary, location, description, allday, start, end (epoch, or
    'YYYY-MM-DD' for all-day; end exclusive), alarm (minutes, None = none,
    "keep" = leave alarms alone). shift: for a series edited through one
    of its occurrences, (old start, new start) of that occurrence - the
    series moves by the same amount."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    comps = components(original, "VEVENT") if original else []
    lines = comps[0] if comps else ["BEGIN:VEVENT", "END:VEVENT"]
    old = event_from_lines(lines) if comps else None
    zone = event_zone(lines) if old else local_zone_name()
    allday = bool(f.get("allday"))
    start, end = f["start"], f["end"]
    series_delta = None
    if shift and old and old["rrule"]:
        # move the series' first start by what the occurrence moved
        o_start, n_start = shift
        kind, first = old["start"]
        length = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)
                  if allday else dt.timedelta(seconds=end - start))
        if allday:
            delta = dt.date.fromisoformat(n_start) - dt.date.fromisoformat(o_start)
            series_delta = delta
            base = first if kind == "date" else first.date()
            new_first = base + delta
            start, end = new_first.isoformat(), (new_first + length).isoformat()
        else:
            delta = dt.timedelta(seconds=n_start - o_start)
            series_delta = delta
            first_t = first if kind == "time" else dt.datetime.combine(
                first, dt.time(9), ZoneInfo(zone))
            new_first = first_t + delta
            start = int(new_first.timestamp())
            end = int((new_first + length).timestamp())
    owned = {"SUMMARY", "LOCATION", "DESCRIPTION", "DTSTART", "DTEND", "DURATION",
             "DTSTAMP", "LAST-MODIFIED", "SEQUENCE"}
    keep_alarms = f.get("alarm", "keep") == "keep"
    out, depth, skipping = [], 0, False
    sequence = 0
    for line in lines[1:-1]:
        if line.startswith("BEGIN:"):
            depth += 1
            skipping = line == "BEGIN:VALARM" and not keep_alarms
            if not skipping:
                out.append(line)
            continue
        if line.startswith("END:"):
            depth -= 1
            if not skipping:
                out.append(line)
            if depth == 0:
                skipping = False
            continue
        if skipping:
            continue
        if depth == 0 and ":" in line:
            key = _prop(line)[0]
            if key == "EXDATE" and series_delta:
                # the days taken out of the series move with it
                out.append(_shift_times(line, series_delta))
                continue
            if key == "SEQUENCE":
                v = line.split(":", 1)[1]
                sequence = int(v) + 1 if v.strip().isdigit() else 1
            if key in owned:
                continue
        out.append(line)
    new = []
    if not any(l.startswith("UID") for l in out):
        new.append("UID:" + hashlib.sha1(("%s%f%s" % (now, time.time(), f.get("summary")))
                                         .encode()).hexdigest())
    new += ["DTSTAMP:" + now, "LAST-MODIFIED:" + now, "SEQUENCE:%d" % sequence]
    if not old:
        new.append("CREATED:" + now)
    kind = "date" if allday else "time"
    p, v = _ical_value(kind, start, zone)
    new.append("DTSTART%s:%s" % (p, v))
    p, v = _ical_value(kind, end, zone)
    new.append("DTEND%s:%s" % (p, v))
    new.append("SUMMARY:" + _escape(f.get("summary", "").strip()))
    if f.get("location", "").strip():
        new.append("LOCATION:" + _escape(f["location"].strip()))
    if f.get("description", "").strip():
        new.append("DESCRIPTION:" + _escape(f["description"].strip()))
    body = new + out
    if not keep_alarms and f.get("alarm") is not None:
        minutes = int(f["alarm"])
        body += ["BEGIN:VALARM", "ACTION:DISPLAY",
                 "DESCRIPTION:" + _escape(f.get("summary", "").strip() or "Reminder"),
                 "TRIGGER;RELATED=START:%sPT%dM" % ("-" if minutes >= 0 else "", abs(minutes)),
                 "END:VALARM"]
    return "\r\n".join(_fold(l) for l in ["BEGIN:VEVENT"] + body + ["END:VEVENT"]) + "\r\n"


# --- evolution-data-server over D-Bus ---------------------------------------------

EDS_SOURCES = "org.gnome.evolution.dataserver.Sources5"
EDS_BOOKS = "org.gnome.evolution.dataserver.AddressBook10"
EDS_CALENDARS = "org.gnome.evolution.dataserver.Calendar8"
EDS = "org.gnome.evolution.dataserver."


class Pim:
    """Address books and calendars of evolution-data-server - the same
    store GNOME Contacts and Calendar use, so changes sync to the accounts."""

    def __init__(self, conn):
        self.conn = conn
        self._open = {}

    def sources(self):
        objs = call(self.conn, EDS_SOURCES, "/org/gnome/evolution/dataserver/SourceManager",
                    "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                    None, "(a{oa{sa{sv}}})", timeout=15000)[0]
        raw = {}
        for ifaces in objs.values():
            s = ifaces.get(EDS + "Source")
            if s and s.get("UID"):
                kf = GLib.KeyFile()
                try:
                    data = s.get("Data", "")
                    kf.load_from_data(data, len(data.encode("utf-8")), GLib.KeyFileFlags.NONE)
                except GLib.Error:
                    continue
                raw[s["UID"]] = kf

        def get(kf, group, key, default=""):
            try:
                return kf.get_string(group, key)
            except GLib.Error:
                return default

        out = []
        for uid, kf in raw.items():
            kinds = [k for k, g in (("contacts", "Address Book"), ("calendar", "Calendar"))
                     if kf.has_group(g)]
            if not kinds or get(kf, "Data Source", "Enabled", "true") == "false":
                continue
            parent = raw.get(get(kf, "Data Source", "Parent"))
            account = get(parent, "Data Source", "DisplayName") if parent else ""
            for kind in kinds:
                group = "Address Book" if kind == "contacts" else "Calendar"
                out.append({"uid": uid, "kind": kind,
                            "name": get(kf, "Data Source", "DisplayName") or uid,
                            "account": account,
                            "backend": get(kf, group, "BackendName"),
                            "color": get(kf, group, "Color")})
        return out

    def _handle(self, kind, uid):
        if (kind, uid) not in self._open:
            if kind == "contacts":
                path, name = call(self.conn, EDS_BOOKS,
                                  "/org/gnome/evolution/dataserver/AddressBookFactory",
                                  EDS + "AddressBookFactory", "OpenAddressBook",
                                  GLib.Variant("(s)", (uid,)), "(ss)", timeout=30000)
                iface = EDS + "AddressBook"
            else:
                path, name = call(self.conn, EDS_CALENDARS,
                                  "/org/gnome/evolution/dataserver/CalendarFactory",
                                  EDS + "CalendarFactory", "OpenCalendar",
                                  GLib.Variant("(s)", (uid,)), "(ss)", timeout=30000)
                iface = EDS + "Calendar"
            call(self.conn, name, path, iface, "Open", None, "(as)", timeout=60000)
            writable = get_prop(self.conn, name, path, iface, "Writable")
            self._open[(kind, uid)] = (name, path, iface, bool(writable))
        return self._open[(kind, uid)]

    def writable(self, kind, uid):
        return self._handle(kind, uid)[3]

    GONE = ("org.freedesktop.DBus.Error.UnknownObject",
            "org.freedesktop.DBus.Error.UnknownMethod",
            "org.freedesktop.DBus.Error.ServiceUnknown",
            "org.freedesktop.DBus.Error.NameHasNoOwner")

    def call(self, kind, uid, method, args, reply=None, timeout=60000):
        name, path, iface, _w = self._handle(kind, uid)
        try:
            return call(self.conn, name, path, iface, method, args, reply, timeout)
        except DBusFailure as e:
            # EDS ends idle backends: then the object is gone - open it again
            # and try once more. Not after anything else (a timeout may have
            # done the change already), and never a creation twice.
            if e.name not in self.GONE or method.startswith("Create"):
                raise
            self._open.pop((kind, uid), None)
            name, path, iface, _w = self._handle(kind, uid)
            return call(self.conn, name, path, iface, method, args, reply, timeout)


# --- the phone: call history (GNOME Calls, read only) and calls in progress ---------

CALLS_DB = os.environ.get("PHONEBRIDGE_CALLS_DB",
                          os.path.join(HOME, ".local/share/calls/records.db"))


def _iso(value):
    import datetime as dt
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def call_history(limit=200, path=None, book=BOOK, country="49", numbers=None):
    """The newest calls; with `numbers`, only those with one of them (the
    calls of one person - however far back)."""
    path = path or CALLS_DB
    if not os.path.exists(path):
        return []
    wanted = {normalize(n, country) for n in numbers} if numbers else None
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3)
    out = []
    with db:
        rows = db.execute("SELECT id, target, inbound, start, answered, end FROM calls"
                          " ORDER BY id DESC" + ("" if wanted else " LIMIT %d" % int(limit)))
        for rid, target, inbound, start, answered, end in rows:
            number = (target or "").strip()
            if wanted is not None:
                if normalize(number, country) not in wanted:
                    continue
                if len(out) >= int(limit):
                    break
            t0, ta, t1 = _iso(start), _iso(answered), _iso(end)
            contact = book.lookup(number, country) if (book is not None and number) else None
            out.append({"id": rid, "number": number, "inbound": bool(inbound),
                        "answered": ta is not None, "start": t0,
                        "duration": int(t1 - ta) if ta and t1 else 0,
                        "name": contact[0] if contact else "",
                        "avatar": avatar_key(contact[1]) if contact else None})
    db.close()
    return out


@command("pim.sources", threaded=True)
def cmd_pim_sources(agent, args):
    out = []
    for s in agent.pim.sources():
        try:
            s["writable"] = agent.pim.writable(s["kind"], s["uid"])
        except RuntimeError:
            s["writable"] = False
            s["broken"] = True
        out.append(s)
    return out


@command("contacts.list", threaded=True)
def cmd_contacts(agent, args):
    out = []
    for s in agent.pim.sources():
        if s["kind"] != "contacts":
            continue
        try:
            vcards = agent.pim.call("contacts", s["uid"], "GetContactList",
                                    GLib.Variant("(s)", ("",)), "(as)")[0]
            writable = agent.pim.writable("contacts", s["uid"])
        except RuntimeError:
            continue
        for v in vcards:
            if re.search(r"^X-EVOLUTION-LIST:TRUE", v, re.M | re.I):
                continue
            c = contact_from_vcard(v)
            c["source"] = s["uid"]
            c["book"] = s["name"]
            c["writable"] = writable
            out.append(c)
    out.sort(key=lambda c: (c["name"] or "~").lower())
    return out


@command("contacts.save", threaded=True)
def cmd_contact_save(agent, args):
    source = args["source"]
    photo = None
    if "photo" in args:
        p = args["photo"]
        photo = "" if not p else (p.get("mime", "image/jpeg"), base64.b64decode(p["data"]))
    uid = args.get("uid")
    old_source = args.get("old_source") or source
    original = None
    if uid:
        original = agent.pim.call("contacts", old_source, "GetContact",
                                  GLib.Variant("(s)", (uid,)), "(s)")[0]
    if uid and old_source != source:
        # another address book: a new contact there, the old one goes
        text = vcard_from_contact(args["contact"], re.sub(
            r"^(UID|X-EVOLUTION-WEBDAV-ETAG|REV)[;:].*\r?\n", "", original, flags=re.M),
            photo)
        new = agent.pim.call("contacts", source, "CreateContacts",
                             GLib.Variant("(asu)", ([text], 0)), "(as)")[0]
        agent.pim.call("contacts", old_source, "RemoveContacts",
                       GLib.Variant("(asu)", ([uid], 0)))
        return {"uid": new[0] if new else ""}
    text = vcard_from_contact(args["contact"], original, photo)
    if uid:
        agent.pim.call("contacts", source, "ModifyContacts",
                       GLib.Variant("(asu)", ([text], 0)))
        return {"uid": uid}
    new = agent.pim.call("contacts", source, "CreateContacts",
                         GLib.Variant("(asu)", ([text], 0)), "(as)")[0]
    return {"uid": new[0] if new else ""}


@command("contacts.delete", threaded=True)
def cmd_contact_delete(agent, args):
    agent.pim.call("contacts", args["source"], "RemoveContacts",
                   GLib.Variant("(asu)", ([args["uid"]], 0)))
    return True


def _make_time(epoch):
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))


@command("calendar.events", threaded=True)
def cmd_events(agent, args):
    start, end = int(args["start"]), int(args["end"])
    query = '(occur-in-time-range? (make-time "%s") (make-time "%s"))' % (
        _make_time(start), _make_time(end))
    out = []
    for s in agent.pim.sources():
        if s["kind"] != "calendar":
            continue
        try:
            objs = agent.pim.call("calendar", s["uid"], "GetObjectList",
                                  GLib.Variant("(s)", (query,)), "(as)")[0]
            writable = agent.pim.writable("calendar", s["uid"])
        except RuntimeError:
            continue
        for ev, s0, e0, rid in expand_events(objs, start, end):
            allday = not hasattr(s0, "hour")
            out.append({
                "source": s["uid"], "calendar": s["name"], "color": s["color"],
                "writable": writable, "uid": ev["uid"], "rid": rid,
                "recurring": bool(ev["rrule"] or ev["rid"]),
                "override": bool(ev["rid"]),
                "summary": ev["summary"], "location": ev["location"],
                "description": ev["description"], "alarm": ev["alarm"],
                "allday": allday,
                "start": s0.isoformat() if allday else int(s0.timestamp()),
                "end": e0.isoformat() if allday else int(e0.timestamp()),
            })
    out.sort(key=lambda e: (e["start"] if not e["allday"] else
                            time.mktime(time.strptime(e["start"], "%Y-%m-%d"))))
    return out


@command("calendar.save", threaded=True)
def cmd_event_save(agent, args):
    source = args["source"]
    uid = args.get("uid")
    old_source = args.get("old_source") or source
    fields = args["event"]
    original = None
    if uid and args.get("override") and args.get("rid") and old_source == source:
        # an occurrence the calendar keeps as an exception of its own
        original = agent.pim.call("calendar", source, "GetObject",
                                  GLib.Variant("(ss)", (uid, args["rid"])), "(s)")[0]
        mine = [c for c in components(original, "VEVENT")
                if any(l.startswith("RECURRENCE-ID") for l in c)]
        text = vevent_from_fields(fields, "\r\n".join(mine[0]) if mine else original)
        agent.pim.call("calendar", source, "ModifyObjects",
                       GLib.Variant("(assu)", ([text], "this", 0)))
        return {"uid": uid}
    if uid:
        original = agent.pim.call("calendar", old_source, "GetObject",
                                  GLib.Variant("(ss)", (uid, "")), "(s)")[0]
        masters = [c for c in components(original, "VEVENT")
                   if not any(l.startswith("RECURRENCE-ID") for l in c)]
        original = "\r\n".join(masters[0]) if masters else original
    shift = None
    if args.get("occurrence_start") is not None:
        shift = (args["occurrence_start"], fields["start"])
    if uid and old_source != source:
        text = vevent_from_fields(fields, re.sub(
            r"^(X-EVOLUTION-CALDAV-ETAG|X-EVOLUTION-CALDAV-HREF)[;:].*\r?\n", "",
            original, flags=re.M), shift)
        agent.pim.call("calendar", source, "CreateObjects",
                       GLib.Variant("(asu)", ([text], 0)), "(as)")
        agent.pim.call("calendar", old_source, "RemoveObjects",
                       GLib.Variant("(a(ss)su)", ([(uid, "")], "all", 0)))
        return {"uid": uid}
    text = vevent_from_fields(fields, original, shift)
    if uid:
        agent.pim.call("calendar", source, "ModifyObjects",
                       GLib.Variant("(assu)", ([text], "all", 0)))
        return {"uid": uid}
    new = agent.pim.call("calendar", source, "CreateObjects",
                         GLib.Variant("(asu)", ([text], 0)), "(as)")[0]
    return {"uid": new[0] if new else ""}


@command("calendar.delete", threaded=True)
def cmd_event_delete(agent, args):
    """scope "all" removes the event (a whole series); "this" takes one
    occurrence out: an EXDATE on the series, or - for an occurrence the
    calendar keeps as an exception of its own - that exception."""
    source, uid = args["source"], args["uid"]
    if args.get("scope") != "this":
        agent.pim.call("calendar", source, "RemoveObjects",
                       GLib.Variant("(a(ss)su)", ([(uid, "")], "all", 0)))
        return True
    if args.get("override"):
        agent.pim.call("calendar", source, "RemoveObjects",
                       GLib.Variant("(a(ss)su)", ([(uid, args["rid"])], "this", 0)))
        return True
    original = agent.pim.call("calendar", source, "GetObject",
                              GLib.Variant("(ss)", (uid, "")), "(s)")[0]
    masters = [c for c in components(original, "VEVENT")
               if not any(l.startswith("RECURRENCE-ID") for l in c)]
    if not masters:
        raise RuntimeError("no such event")
    text = "\r\n".join(_fold(l) for l in add_exdate(masters[0], args["start"])) + "\r\n"
    agent.pim.call("calendar", source, "ModifyObjects",
                   GLib.Variant("(assu)", ([text], "all", 0)))
    return True


@command("calls.history", threaded=True)
def cmd_call_history(agent, args):
    country = args.get("country", "49")
    calls = call_history(args.get("limit", 200), country=country,
                         numbers=args.get("numbers"))
    if voicebox_installed():
        match_voicebox(calls, voicebox_messages(book=None), country)
    return calls


@command("calls.active")
def cmd_calls_active(agent, args):
    return agent.active_calls()


@command("call.answer")
def cmd_answer(agent, args):
    call(agent.system, "org.ofono", args["path"], "org.ofono.VoiceCall", "Answer",
         timeout=15000)
    return True


@command("call.hangup")
def cmd_hangup(agent, args):
    try:
        call(agent.system, "org.ofono", args["path"], "org.ofono.VoiceCall", "Hangup",
             timeout=15000)
    except RuntimeError:
        # ofono's Hangup fails now and then; ModemManager's hangup works
        if not agent.modem:
            raise
        call(agent.system, "org.ofono", agent.modem, "org.ofono.VoiceCallManager",
             "HangupAll", timeout=15000)
    return True


# --- VoiceBox (misc-de/VoiceBox), when it is installed ------------------------

VOICEBOX_DATA = os.environ.get("PHONEBRIDGE_VOICEBOX",
                               os.path.join(HOME, ".local/share/voicebox"))
VOICEBOX_CONFIG = os.environ.get("PHONEBRIDGE_VOICEBOX_CONFIG",
                                 os.path.join(HOME, ".config/voicebox/config.json"))
VOICEBOX_AUDIO_MAX = 32 * 1024 * 1024


def voicebox_dir():
    return os.path.join(VOICEBOX_DATA, "messages")


def voicebox_installed():
    return os.path.isfile(VOICEBOX_CONFIG) or os.path.isdir(voicebox_dir())


def _voicebox_id(mid):
    """Message ids are file names VoiceBox made from the time - nothing else
    gets near the file system."""
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z_-]*", mid or ""):
        raise RuntimeError("no such message")
    return mid


def voicebox_boxes():
    try:
        with open(VOICEBOX_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return []
    return [{"id": str(b.get("id")), "name": str(b.get("name") or ""),
             "active": bool(b.get("active"))}
            for b in cfg.get("boxes") or [] if isinstance(b, dict)]


def voicebox_messages(book=BOOK, country="49"):
    """VoiceBox's messages, newest first, read like VoiceBox's store.py: one
    JSON per message, a WAV next to it unless VoiceBox answered and nobody
    spoke ("missed"); hidden files are recordings in progress."""
    out = []
    try:
        names = os.listdir(voicebox_dir())
    except OSError:
        return out
    for fn in names:
        if fn.startswith(".") or not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(voicebox_dir(), fn), encoding="utf-8") as f:
                m = json.load(f)
            mid = fn[:-5]
            number = str(m.get("number", ""))
            name = str(m.get("name", ""))
            contact = book.lookup(number, country) if (book is not None and number) else None
            out.append({"id": mid, "number": number,
                        "name": name or (contact[0] if contact else ""),
                        "time": float(m.get("time", 0)),
                        "duration": float(m.get("duration", 0)),
                        "new": bool(m.get("new")), "box": str(m.get("box") or "global"),
                        "missed": bool(m.get("missed")),
                        "audio": os.path.exists(os.path.join(voicebox_dir(), mid + ".wav")),
                        "avatar": avatar_key(contact[1]) if contact else None})
        except (OSError, ValueError, TypeError):
            continue
    out.sort(key=lambda m: m["time"], reverse=True)
    return out


def voicebox_signature():
    try:
        return tuple(sorted((fn, os.stat(os.path.join(voicebox_dir(), fn)).st_mtime_ns)
                            for fn in os.listdir(voicebox_dir()) if not fn.startswith(".")))
    except OSError:
        return ()


def voicebox_mark_read(mid):
    path = os.path.join(voicebox_dir(), _voicebox_id(mid) + ".json")
    with open(path, encoding="utf-8") as f:
        meta = json.load(f)
    if meta.get("new"):
        meta["new"] = False
        tmp = path + ".tmp"     # as VoiceBox writes it: whole, then in place
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp, path)


def voicebox_delete(mid):
    base = os.path.join(voicebox_dir(), _voicebox_id(mid))
    for p in (base + ".json", base + ".wav"):      # the JSON first, like VoiceBox
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def match_voicebox(calls, messages, country="49"):
    """Marks the calls VoiceBox answered: same number, and the message
    written between the call's start and a few minutes after it ended."""
    used = set()
    for c in calls:
        if not c.get("start"):
            continue
        number = normalize(c["number"], country)
        end = c["start"] + c.get("duration", 0) + 300
        best = None
        for m in messages:
            if m["id"] in used or normalize(m["number"], country) != number:
                continue
            if c["start"] - 30 <= m["time"] <= end:
                if best is None or abs(m["time"] - c["start"]) < abs(best["time"] - c["start"]):
                    best = m
        if best is not None:
            used.add(best["id"])
            c["voicebox"] = {"id": best["id"], "missed": best["missed"],
                             "audio": best["audio"], "duration": best["duration"],
                             "new": best["new"]}
    return calls


@command("voicebox.list", threaded=True)
def cmd_voicebox(agent, args):
    if not voicebox_installed():
        return {"installed": False, "boxes": [], "messages": []}
    return {"installed": True, "boxes": voicebox_boxes(),
            "messages": voicebox_messages(country=args.get("country", "49"))}


VOICEBOX_CHUNK = 512 * 1024


@command("voicebox.audio", threaded=True)
def cmd_voicebox_audio(agent, args):
    """A recording in pieces ("offset", up to 512 KB each) - one huge line
    would hold up every event behind it."""
    path = os.path.join(voicebox_dir(), _voicebox_id(args["id"]) + ".wav")
    offset = max(0, int(args.get("offset", 0)))
    try:
        total = os.path.getsize(path)
        if total > VOICEBOX_AUDIO_MAX:
            raise RuntimeError("recording too large")
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(VOICEBOX_CHUNK)
        return {"data": base64.b64encode(data).decode("ascii"), "total": total,
                "offset": offset}
    except FileNotFoundError:
        raise RuntimeError("no such message")


@command("voicebox.read")
def cmd_voicebox_read(agent, args):
    try:
        voicebox_mark_read(args["id"])
    except FileNotFoundError:
        raise RuntimeError("no such message")
    return True


@command("voicebox.delete")
def cmd_voicebox_delete(agent, args):
    voicebox_delete(args["id"])
    return True


# --- the call's sound on the PC: muting the phone's microphone ------------------
# The stream itself does not pass the agent: the PC opens a second SSH
# connection with pw-record/pw-play on droid-call-source/-sink (the patched
# spa-droid plugin, as VoiceBox uses it). Here only the uplink is muted in
# the modem - during a cellular call the microphone never passes PipeWire,
# so this is the only mute that holds - read back, kept muted while the PC
# has the sound, and handed back as it was when the PC lets go or the call
# ends.

def pipewire_state():
    """(PipeWire runs, the call audio nodes are there) - asked live: PipeWire
    may stop or crash, and without it the call's sound cannot reach the PC."""
    if run("pw-cli", "info", "0", timeout=3) is None:
        return False, False
    out = run("pw-cli", "ls", "Node", timeout=3) or ""
    return True, "droid-call-sink" in out and "droid-call-source" in out


def call_audio_nodes():
    return pipewire_state()[1]


def _uplink_muted(agent):
    props = call(agent.system, "org.ofono", agent.modem, "org.ofono.CallVolume",
                 "GetProperties", None, "(a{sv})")[0]
    return bool(props.get("Muted"))


def _set_uplink_muted(agent, on):
    call(agent.system, "org.ofono", agent.modem, "org.ofono.CallVolume", "SetProperty",
         GLib.Variant("(sv)", ("Muted", GLib.Variant("b", bool(on)))))


def pc_audio_guard(agent):
    """Polled while the PC has the sound: muted it stays."""
    if not getattr(agent, "pc_audio", False):
        return False
    try:
        if not _uplink_muted(agent):
            _set_uplink_muted(agent, True)
    except Exception:  # noqa: BLE001 - the guard must stay
        pass
    return True


def pc_audio_release(agent):
    if not getattr(agent, "pc_audio", False):
        return
    agent.pc_audio = False
    try:
        _set_uplink_muted(agent, agent.pc_audio_was_muted)
    except RuntimeError:
        pass


@command("callaudio.mute")
def cmd_callaudio_mute(agent, args):
    """on: mute the phone's microphone for the PC and confirm it;
    off: hand back what it was before."""
    if not agent.modem:
        raise RuntimeError("no modem")
    if args.get("on"):
        # checked right now, before the microphone goes: without PipeWire
        # (or its call audio nodes) the sound could not reach the PC
        running, nodes = pipewire_state()
        if not running:
            raise RuntimeError("PipeWire is not running on the phone")
        if not nodes:
            raise RuntimeError("the phone has no call audio nodes (droid-call-sink/-source)")
        if not getattr(agent, "pc_audio", False):
            agent.pc_audio_was_muted = _uplink_muted(agent)
        _set_uplink_muted(agent, True)
        if not _uplink_muted(agent):
            raise RuntimeError("the phone's microphone could not be muted")
        if not getattr(agent, "pc_audio", False):
            agent.pc_audio = True
            GLib.timeout_add(1500, lambda: pc_audio_guard(agent))
        return True
    pc_audio_release(agent)
    return False


# --- deleting a conversation from chatty's store --------------------------------
# chatty has no interface for it, and it keeps its conversations in memory:
# deleting under a running chatty could make it file the next message into
# a conversation that is gone. So chatty is ended, the store copied, the
# conversation deleted the way chatty deletes one (foreign keys on: its
# messages, members and ModemManager records go with it), and chatty started
# again exactly as it ran. An SMS arriving meanwhile waits in the modem.

BACKUPS = 3


def processes_named(name):
    """(pid, argv, environ) of the user's running processes called `name`."""
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            st = os.stat("/proc/" + pid)
            if st.st_uid != UID:
                continue
            with open("/proc/%s/comm" % pid) as f:
                if f.read().strip() != name[:15]:
                    continue
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                argv = [a.decode() for a in f.read().split(b"\0") if a]
            with open("/proc/%s/environ" % pid, "rb") as f:
                env = dict(e.decode("utf-8", "replace").split("=", 1)
                           for e in f.read().split(b"\0") if b"=" in e)
            out.append((int(pid), argv, env))
        except (OSError, ValueError):
            continue
    return out


def chatty_processes():
    return processes_named("chatty")


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().split(")")[-1].split()[0] == "Z"
    except OSError:
        return True


def stop_chatty(timeout=8, procs=None, name="chatty"):
    procs = processes_named(name) if procs is None else procs
    for pid, _argv, _env in procs:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    end = time.time() + timeout
    while time.time() < end and not all(_gone(pid) for pid, _a, _e in procs):
        time.sleep(0.1)
    if not all(_gone(pid) for pid, _a, _e in procs):
        raise RuntimeError("%s did not stop" % name)
    return procs


def start_chatty(procs, name="chatty"):
    """Starts the programs again as they ran - argv and environment - in
    the user's session."""
    for _pid, argv, env in procs:
        if len(argv) > 1 and os.path.basename(argv[0]) != name \
                and os.path.basename(argv[1]) == name:
            argv = argv[1:]     # started through its interpreter (#!): start it as such
        if argv and shutil.which("systemd-run"):
            # in the user's session, not in this SSH login's scope (which may
            # take chatty along when the PC disconnects)
            argv = ["systemd-run", "--user", "--scope", "--collect", "--quiet", "--"] + argv
        if argv:
            subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, cwd=HOME)


def backup_store(path=None):
    path = path or CHATTY_DB
    d = os.path.join(DATA_DIR, "backups")
    _private_dir(d)
    target = os.path.join(d, "chatty-history-%s.db" % time.strftime("%Y%m%d-%H%M%S"))
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    src = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    for old in sorted(glob.glob(os.path.join(d, "chatty-history-*.db")))[:-BACKUPS]:
        os.remove(old)
    return target


def delete_thread_rows(thread, path=None):
    """Deletes one conversation from chatty's store; the number of its messages."""
    db = sqlite3.connect(path or CHATTY_DB, timeout=10)
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA secure_delete = ON")   # deleted is overwritten, not just unlinked
        with db:
            ids = [r[0] for r in db.execute("SELECT id FROM threads WHERE name = ?", (thread,))]
            if not ids:
                return 0
            marks = ",".join("?" * len(ids))
            n = db.execute("SELECT COUNT(*) FROM messages WHERE thread_id IN (%s)" % marks,
                           ids).fetchone()[0]
            db.execute("DELETE FROM messages WHERE thread_id IN (%s)" % marks, ids)
            db.execute("DELETE FROM thread_members WHERE thread_id IN (%s)" % marks, ids)
            db.execute("DELETE FROM threads WHERE id IN (%s)" % marks, ids)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return n
    finally:
        db.close()


def drop_sent(number, path=None):
    """Our own log of sent messages without that number."""
    path = path or SENT_LOG
    entries = read_sent(path)
    keep = [e for e in entries if e.get("to") != number]
    if len(keep) == len(entries):
        return 0
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for e in keep:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return len(entries) - len(keep)


@command("sms.delete_thread", threaded=True)
def cmd_delete_thread(agent, args):
    thread = args["thread"]
    country = args.get("country", "49")
    deleted = 0
    procs = []
    if os.path.exists(CHATTY_DB):
        backup = backup_store()
        procs = chatty_processes()
        try:
            stop_chatty(procs=procs)
            deleted = delete_thread_rows(thread)
        finally:
            # every chatty that ended comes back - also when one would not stop
            start_chatty([p for p in procs if _gone(p[0])])
        try:
            os.remove(backup)           # deleted for good means no copy either
        except OSError:
            pass
    deleted += drop_sent(normalize(thread, country))
    agent.last_sms_id = store_last_id()
    send({"event": "sms", "new": []})
    return {"deleted": deleted, "restarted": bool(procs)}


# --- lines: the SIM cards and SIP accounts a call can go out on -----------------
# A SIM is known by its ICCID, not its slot (as in VoiceBox): swap the cards
# and the choice follows the card. GNOME Calls gives every SIM the same
# origin id ("ofono"), so a SIM is dialled on its modem through ofono
# directly - the way Calls itself dials, and Calls shows the call as usual.
# SIP accounts are GNOME Calls' own (sip-account.cfg); they dial through
# Calls' dial-sip action with the account's id.

SIP_KEYFILE = os.environ.get("PHONEBRIDGE_SIP_KEYFILE", os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"),
    "calls", "sip-account.cfg"))


def _slot(modem):
    m = re.search(r"(\d+)$", modem or "")
    return int(m.group(1)) + 1 if m else 0


def sim_lines(agent):
    out = []
    try:
        modems = call(agent.system, "org.ofono", "/", "org.ofono.Manager", "GetModems",
                      None, "(a(oa{sv}))")[0]
    except RuntimeError:
        return out
    for modem, _props in sorted(modems):
        try:
            sim = call(agent.system, "org.ofono", modem, "org.ofono.SimManager",
                       "GetProperties", None, "(a{sv})")[0]
        except RuntimeError:
            continue
        if not sim.get("Present"):
            continue
        try:
            reg = call(agent.system, "org.ofono", modem, "org.ofono.NetworkRegistration",
                       "GetProperties", None, "(a{sv})")[0]
        except RuntimeError:
            reg = {}
        numbers = [n for n in sim.get("SubscriberNumbers") or [] if re.search(r"\d", n)]
        out.append({"id": "sim:" + (sim.get("CardIdentifier") or modem), "kind": "sim",
                    "slot": _slot(modem), "modem": modem,
                    "operator": reg.get("Name") or sim.get("ServiceProviderName") or "",
                    "number": numbers[0] if numbers else "",
                    "ready": reg.get("Status") in ("registered", "roaming")})
    return out


def sip_lines(path=None):
    kf = GLib.KeyFile()
    try:
        kf.load_from_file(path or SIP_KEYFILE, GLib.KeyFileFlags.NONE)
    except GLib.Error:
        return []
    out = []
    for group in kf.get_groups()[0]:
        def get(key):
            try:
                return kf.get_string(group, key)
            except GLib.Error:
                return ""
        account_id = get("Id") or group
        user, host = get("User"), get("Host")
        out.append({"id": "sip:" + account_id, "kind": "sip", "account": account_id,
                    "name": get("DisplayName"),
                    "address": "%s@%s" % (user, host) if user and host else "",
                    "ready": True})
    return out


@command("lines.list")
def cmd_lines(agent, args):
    return sim_lines(agent) + sip_lines()


def dial_on_line(agent, number, line):
    if line.startswith("sim:"):
        sims = {l["id"]: l for l in sim_lines(agent)}
        if line not in sims:
            raise RuntimeError("this SIM card is not in the phone")
        call(agent.system, "org.ofono", sims[line]["modem"], "org.ofono.VoiceCallManager",
             "Dial", GLib.Variant("(ss)", (number, "default")), "(o)", timeout=30000)
        return
    if line.startswith("sip:"):
        accounts = {l["id"] for l in sip_lines()}
        if line not in accounts:
            raise RuntimeError("no such SIP account")
        call(agent.session, "org.gnome.Calls", "/org/gnome/Calls", "org.gtk.Actions",
             "Activate", GLib.Variant("(sava{sv})", (
                 "dial-sip", [GLib.Variant("(ss)", (number, line[4:]))], {})),
             timeout=15000)
        return
    raise RuntimeError("unknown line")


# --- SIP accounts of GNOME Calls ---------------------------------------------------
# Written as Calls writes them: a group in sip-account.cfg, the password in
# the phone's keyring (schema sm.puri.Calls, attributes server, username,
# protocol "sip"). Calls reads the file only when it starts and writes it
# whole from memory later - so it is ended for the change and started again
# as it ran; never during a call.

SIP_FIELDS = ("Id", "Host", "Proxy", "User", "DisplayName", "Protocol", "Port",
              "AutoConnect", "DirectMode", "LocalPort", "CanTel", "MediaEncryption")


def _calls_secret_schema():
    gi.require_version("Secret", "1")
    from gi.repository import Secret
    return Secret, Secret.Schema.new(
        "sm.puri.Calls", Secret.SchemaFlags.DONT_MATCH_NAME,
        {"username": Secret.SchemaAttributeType.STRING,
         "server": Secret.SchemaAttributeType.STRING,
         "protocol": Secret.SchemaAttributeType.STRING})


def _sip_secret(host, user, password=None, clear=False):
    if os.environ.get("PHONEBRIDGE_KEYRING") == "memory":   # the tests: never a keyring
        with open(os.environ.get("FAKE_LOG") or os.devnull, "a") as f:
            f.write("secret %s %s@%s\n" % ("clear" if clear else "store", user, host))
        return
    Secret, schema = _calls_secret_schema()
    attrs = {"server": host, "username": user, "protocol": "sip"}
    if clear:
        Secret.password_clear_sync(schema, attrs, None)
    else:
        Secret.password_store_sync(schema, attrs, Secret.COLLECTION_DEFAULT,
                                   "Calls Password for %s" % password[0], password[1], None)


def sip_accounts(path=None):
    """GNOME Calls' SIP accounts with their settings (not the passwords)."""
    kf = GLib.KeyFile()
    try:
        kf.load_from_file(path or SIP_KEYFILE, GLib.KeyFileFlags.KEEP_COMMENTS)
    except GLib.Error:
        return []
    out = []
    for group in kf.get_groups()[0]:
        def get(key, kind=str, default=None):
            try:
                if kind is bool:
                    return kf.get_boolean(group, key)
                if kind is int:
                    return kf.get_integer(group, key)
                return kf.get_string(group, key)
            except GLib.Error:
                return default
        out.append({"id": get("Id") or group, "host": get("Host", default=""),
                    "user": get("User", default=""), "display_name": get("DisplayName", default=""),
                    "protocol": get("Protocol", default="UDP") or "UDP",
                    "port": get("Port", int, 0), "auto_connect": get("AutoConnect", bool, True),
                    "can_tel": get("CanTel", bool, False),
                    "media_encryption": get("MediaEncryption", int, 0)})
    return out


def write_sip_account(account, path=None, remove=False):
    """Adds, changes (by "id") or removes one group of sip-account.cfg; the
    other accounts stay as they are. -> the group written."""
    path = path or SIP_KEYFILE
    kf = GLib.KeyFile()
    try:
        kf.load_from_file(path, GLib.KeyFileFlags.KEEP_COMMENTS)
    except GLib.Error:
        pass
    groups = kf.get_groups()[0]
    group = None
    for g in groups:
        try:
            gid = kf.get_string(g, "Id")
        except GLib.Error:
            gid = g
        if gid == account.get("id"):
            group = g
    if remove:
        if group is None:
            raise RuntimeError("no such SIP account")
        kf.remove_group(group)
    else:
        if group is None:
            n = 0
            while "sip-%02d" % n in groups:
                n += 1
            group = "sip-%02d" % n
        host, user = account["host"].strip(), account["user"].strip()
        if not host or not user or re.search(r"[\s\[\]=]", host + user):
            raise RuntimeError("server and user are needed")
        protocol = account.get("protocol", "UDP")
        if protocol not in ("UDP", "TCP", "TLS"):
            raise RuntimeError("unknown transport")
        kf.set_string(group, "Id", account.get("id") or "%s@%s" % (user, host))
        kf.set_string(group, "Host", host)
        kf.set_string(group, "Proxy", account.get("proxy", "") or "")
        kf.set_string(group, "User", user)
        kf.set_string(group, "DisplayName", _one_line(account.get("display_name", "")))
        kf.set_string(group, "Protocol", protocol)
        kf.set_integer(group, "Port", max(0, min(65535, int(account.get("port") or 0))))
        kf.set_boolean(group, "AutoConnect", bool(account.get("auto_connect", True)))
        kf.set_boolean(group, "DirectMode", False)
        kf.set_integer(group, "LocalPort", 0)
        kf.set_boolean(group, "CanTel", bool(account.get("can_tel", False)))
        kf.set_integer(group, "MediaEncryption", max(0, min(2, int(account.get("media_encryption") or 0))))
    _private_dir(os.path.dirname(path))
    data = kf.to_data()[0]
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)
    return group


def _with_calls_stopped(agent, change):
    """Runs change() while GNOME Calls is ended, then starts Calls again."""
    if agent.active_calls():
        raise RuntimeError("not during a call")
    procs = processes_named("gnome-calls")
    try:
        stop_chatty(procs=procs, name="gnome-calls")
        return change()
    finally:
        start_chatty([p for p in procs if _gone(p[0])], name="gnome-calls")


@command("sip.list", threaded=True)
def cmd_sip_list(agent, args):
    return sip_accounts()


@command("sip.save", threaded=True)
def cmd_sip_save(agent, args):
    """Adds or changes a SIP account; "password" only when it changes."""
    account = dict(args["account"])
    old = next((a for a in sip_accounts() if a["id"] == account.get("id")), None)

    def change():
        if not account.get("id"):
            account["id"] = "%s@%s" % (account["user"].strip(), account["host"].strip())
        write_sip_account(account)
        if old and (old["host"], old["user"]) != (account["host"], account["user"]):
            _sip_secret(old["host"], old["user"], clear=True)
        if args.get("password"):
            _sip_secret(account["host"].strip(), account["user"].strip(),
                        (account["id"], args["password"]))
        return account["id"]

    return {"id": _with_calls_stopped(agent, change)}


@command("sip.delete", threaded=True)
def cmd_sip_delete(agent, args):
    old = next((a for a in sip_accounts() if a["id"] == args["id"]), None)
    if old is None:
        raise RuntimeError("no such SIP account")

    def change():
        write_sip_account({"id": args["id"]}, remove=True)
        _sip_secret(old["host"], old["user"], clear=True)
        return True

    return _with_calls_stopped(agent, change)


# --- the PC's SSH key on the phone (ssh-copy-id) ------------------------------------

AUTHORIZED_KEYS = os.environ.get("PHONEBRIDGE_AUTHORIZED_KEYS",
                                 os.path.join(HOME, ".ssh", "authorized_keys"))
PUBLIC_KEY = re.compile(r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|"
                        r"sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]{40,}( [^\r\n]*)?")


@command("ssh.authorize")
def cmd_authorize(agent, args):
    """Adds the PC's public key to ~/.ssh/authorized_keys, as ssh-copy-id
    does: the directory 0700, the file 0600, a key already there not twice."""
    key = (args.get("key") or "").strip()
    if not PUBLIC_KEY.fullmatch(key):
        raise RuntimeError("not a public SSH key")
    _private_dir(os.path.dirname(AUTHORIZED_KEYS))
    try:
        with open(AUTHORIZED_KEYS, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    if key.split()[:2] in [line.split()[:2] for line in text.splitlines() if line.strip()]:
        return "present"
    fd = os.open(AUTHORIZED_KEYS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        # a last line without its newline would swallow the key
        f.write(("\n" if text and not text.endswith("\n") else "") + key + "\n")
    os.chmod(AUTHORIZED_KEYS, 0o600)
    return "added"


# --- files -----------------------------------------------------------------
# The phone's files for the PC's file browser: folders, thumbnails and
# changes go through here, in a worker of their own; the contents travel
# over an ssh of their own (phonebridge/files.py), raw.

FILES_HOME = os.environ.get("PHONEBRIDGE_FILES_HOME", HOME)
THUMBNAILS = os.environ.get("PHONEBRIDGE_THUMBNAILS",
                            os.path.join(HOME, ".cache", "thumbnails"))
FILES_MAX = 20000               # entries of one folder
THUMB_SIZE = 128
THUMB_FILE_MAX = 40 * 1024 * 1024   # larger pictures get no thumbnail made
THUMBS_AT_ONCE = 24
PLACES = (("documents", "DIRECTORY_DOCUMENTS", "Documents"),
          ("downloads", "DIRECTORY_DOWNLOAD", "Downloads"),
          ("pictures", "DIRECTORY_PICTURES", "Pictures"),
          ("music", "DIRECTORY_MUSIC", "Music"),
          ("videos", "DIRECTORY_VIDEOS", "Videos"))


def files_path(path):
    """An absolute, normalised path; the home when none is given."""
    path = os.path.expanduser(path or FILES_HOME)
    if not os.path.isabs(path) or "\0" in path:
        raise RuntimeError("not an absolute path")
    return os.path.normpath(path)


def file_name(name):
    """A name for a new file or folder: one path element."""
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise RuntimeError("invalid name")
    return name


def _os_error(e):
    return RuntimeError(e.strerror or str(e))


def file_entry(de):
    try:
        st = de.stat()                      # through a link: what it points at
        is_dir = os.path.isdir(de.path)
        size, mtime = (0 if is_dir else st.st_size), int(st.st_mtime)
    except OSError:                         # a broken link
        is_dir, size, mtime = False, 0, 0
    return {"name": de.name, "dir": is_dir, "size": size, "mtime": mtime,
            "link": de.is_symlink()}


@command("files.places")
def cmd_files_places(agent, args):
    places = []
    for pid, special, default in PLACES:
        path = None
        if FILES_HOME == HOME:
            path = GLib.get_user_special_dir(getattr(GLib.UserDirectory, special))
        if not path or path == HOME:
            path = os.path.join(FILES_HOME, default)
        if os.path.isdir(path):
            places.append({"id": pid, "path": path})
    return {"home": FILES_HOME, "places": places}


@command("files.list", threaded="files")
def cmd_files_list(agent, args):
    path = files_path(args.get("path"))
    entries, truncated = [], False
    try:
        with os.scandir(path) as it:
            for de in it:
                if len(entries) >= FILES_MAX:
                    truncated = True
                    break
                entries.append(file_entry(de))
    except OSError as e:
        raise _os_error(e)
    return {"path": path, "parent": None if path == "/" else os.path.dirname(path),
            "writable": os.access(path, os.W_OK), "entries": entries,
            "truncated": truncated}


def thumbnail(path):
    """PNG bytes: the phone's own thumbnail when it is not older than the
    file, else one made here - pictures only, not too large. None when
    there is none."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    digest = hashlib.md5(Gio.File.new_for_path(path).get_uri().encode()).hexdigest()
    for size in ("normal", "large", "x-large", "xx-large"):
        thumb = os.path.join(THUMBNAILS, size, digest + ".png")
        try:
            if os.stat(thumb).st_mtime >= st.st_mtime:
                with open(thumb, "rb") as f:
                    return f.read()
        except OSError:
            continue
    if st.st_size > THUMB_FILE_MAX:
        return None
    try:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, THUMB_SIZE, THUMB_SIZE, True)
        pb = pb.apply_embedded_orientation() or pb
        ok, data = pb.save_to_bufferv("png", [], [])
        return bytes(data) if ok else None
    except (ImportError, ValueError, GLib.Error):
        return None


@command("files.thumbs", threaded="files")
def cmd_files_thumbs(agent, args):
    out = {}
    for path in (args.get("paths") or [])[:THUMBS_AT_ONCE]:
        data = thumbnail(files_path(path))
        out[path] = base64.b64encode(data).decode("ascii") if data else None
    return out


@command("files.mkdir", threaded="files")
def cmd_files_mkdir(agent, args):
    path = os.path.join(files_path(args.get("path")), file_name(args.get("name")))
    try:
        os.mkdir(path)
    except OSError as e:
        raise _os_error(e)
    return {"path": path}


@command("files.rename", threaded="files")
def cmd_files_rename(agent, args):
    path = files_path(args.get("path"))
    target = os.path.join(os.path.dirname(path), file_name(args.get("name")))
    if os.path.lexists(target):
        raise RuntimeError("already exists")
    try:
        os.rename(path, target)
    except OSError as e:
        raise _os_error(e)
    return {"path": target}


@command("files.delete", threaded="files")
def cmd_files_delete(agent, args):
    """For good - the PC asked first. Never / or the home itself."""
    failed = []
    for p in args.get("paths") or []:
        path = files_path(p)
        if path in ("/", os.path.normpath(HOME), os.path.normpath(FILES_HOME)):
            failed.append({"path": path, "error": "not allowed"})
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as e:
            failed.append({"path": path, "error": e.strerror or str(e)})
    return {"failed": failed}


# --- main ------------------------------------------------------------------

def _reader(agent):
    for line in sys.stdin.buffer:
        agent.last_rx = time.monotonic()
        GLib.idle_add(agent.dispatch, line)
    GLib.idle_add(agent.loop.quit)


def _worker(agent, work):
    """Runs the threaded commands, one after the other, in order."""
    while True:
        fn, args, reply = work.get()
        try:
            reply(fn(agent, args))
        except Exception as e:  # noqa: BLE001 - every failure goes back to the PC
            reply(error=e)


def main():
    loop = GLib.MainLoop()
    agent = Agent(loop)
    threading.Thread(target=_worker, args=(agent, agent.work), daemon=True).start()
    threading.Thread(target=_worker, args=(agent, agent.files_work), daemon=True).start()
    threading.Thread(target=_reader, args=(agent,), daemon=True).start()
    agent.schedule_refresh(0)
    try:                            # GLib 2.80 moved it
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        signal_add = GLibUnix.signal_add
    except (ImportError, ValueError):
        signal_add = GLib.unix_signal_add
    for sig in (1, 2, 15):          # HUP (ssh gone), INT, TERM: end cleanly
        signal_add(GLib.PRIORITY_HIGH, sig, lambda: loop.quit() or False)
    try:
        loop.run()
    finally:
        pc_audio_release(agent)     # the PC is gone: never leave the phone muted


if __name__ == "__main__":
    main()
