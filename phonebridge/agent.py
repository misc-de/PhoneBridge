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

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time

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

VERSION = 1
APP_ID = "io.github.miscde.PhoneBridge"
HOME = os.path.expanduser("~")
CHATTY_DB = os.environ.get(
    "PHONEBRIDGE_CHATTY_DB",
    os.path.join(HOME, ".purple/chatty/db/chatty-history.db"))
DATA_DIR = os.environ.get(
    "PHONEBRIDGE_DATA", os.path.join(HOME, ".local/share/phonebridge"))
SENT_LOG = os.path.join(DATA_DIR, "sent.jsonl")
# A message the phone sent shows up in chatty's store as well when chatty
# sent it; our own log then holds a duplicate within this many seconds.
DUPLICATE_WINDOW = 600

COMMANDS = {}
_out_lock = threading.Lock()


def command(name, deferred=False):
    """Registers a handler. A deferred one gets a reply(result, error)
    callback and answers later; the others return their result."""
    def wrap(fn):
        COMMANDS[name] = (fn, deferred)
        return fn
    return wrap


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
       m.id AS mid, m.body, m.direction, m.time
  FROM threads t
  JOIN messages m ON m.id = (SELECT id FROM messages WHERE thread_id = t.id
                              ORDER BY time DESC, id DESC LIMIT 1)
"""


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


def append_sent(entry, path=None):
    path = path or SENT_LOG
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _is_duplicate(entry, outgoing):
    return any(m["body"] == entry["body"]
               and abs(m["time"] - entry["time"]) <= DUPLICATE_WINDOW
               for m in outgoing)


def list_threads(baseline=0, seen=None, country="49", store=None, sent=None):
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
                threads[r["name"]] = {
                    "thread": r["name"], "title": r["title"],
                    "group": r["type"] == 1,
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
            threads[name] = {"thread": name, "title": name, "group": False,
                             "last": None, "unread": 0}
        last = threads[name]["last"]
        if last is None or e["time"] >= last["time"]:
            threads[name]["last"] = {"id": e["id"], "body": e["body"],
                                     "out": True, "time": e["time"]}
    for name, n in unread.items():
        if name in threads:
            threads[name]["unread"] = n
    return sorted(threads.values(), key=lambda t: t["last"]["time"], reverse=True)


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


def new_incoming(after_id, store=None):
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
        return [{"id": r["id"], "thread": r["name"], "title": r["title"],
                 "body": r["body"], "time": r["time"]} for r in rows]


# --- D-Bus helpers ---------------------------------------------------------

def bus(kind):
    try:
        return Gio.bus_get_sync(kind, None)
    except GLib.Error:
        return None


def call(conn, name, path, iface, method, args=None, reply=None, timeout=5000):
    if conn is None:
        raise RuntimeError("%s is not reachable" % name)
    try:
        v = conn.call_sync(name, path, iface, method, args,
                           GLib.VariantType(reply) if reply else None,
                           Gio.DBusCallFlags.NONE, timeout, None)
    except GLib.Error as e:
        Gio.DBusError.strip_remote_error(e)
        raise RuntimeError(e.message)
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
        self._sms_pending = 0
        self.last_sms_id = store_last_id()
        self.modem = self._ofono_modem()
        self._watch()

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
                        "GetProperties", None, "(a{sv})")[0]
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

    def collect(self):
        return {"hostname": socket.gethostname(), "battery": self.battery(),
                "network": self.network(), "mobile_data": self.mobile_data(),
                "wifi": self.wifi(), "volume": self.volume(),
                "power_profile": self.power_profile(),
                "feedback_profile": self.feedback_profile()}

    # -- watching ---------------------------------------------------------
    def _watch(self):
        if self.system is not None:
            for sender, iface in (("org.freedesktop.UPower", None),
                                  ("org.ofono", None),
                                  ("org.freedesktop.UPower.PowerProfiles", None)):
                self.system.signal_subscribe(
                    sender, iface, None, None, None, Gio.DBusSignalFlags.NONE,
                    self._on_signal)
        if self.session is not None:
            self.session.signal_subscribe(
                "org.sigxcpu.Feedback", None, None, None, None,
                Gio.DBusSignalFlags.NONE, self._on_signal)
        store = Gio.File.new_for_path(CHATTY_DB)
        try:
            self._monitor = store.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", lambda *a: self.schedule_sms_check())
        except GLib.Error:
            self._monitor = None
        GLib.timeout_add_seconds(30, self._tick)
        GLib.timeout_add_seconds(20, self._sms_tick)

    def _on_signal(self, conn, sender, path, iface, signal, params):
        if iface == "org.ofono.MessageManager" and signal == "IncomingMessage":
            # chatty stores it a moment later; the file monitor catches that
            # too, this is the safety net when it does not fire.
            GLib.timeout_add_seconds(3, lambda: self.check_sms() and False)
        self.schedule_refresh()

    def _tick(self):
        self.schedule_refresh()
        return True

    def _sms_tick(self):
        self.check_sms()
        return True

    def schedule_refresh(self, delay=800):
        if not self._refresh_pending:
            self._refresh_pending = GLib.timeout_add(delay, self._refresh)

    def _refresh(self):
        self._refresh_pending = 0
        status = self.collect()
        if status != self.status:
            self.status = status
            send({"event": "status", "data": status})
        return False

    def schedule_sms_check(self):
        if not self._sms_pending:
            self._sms_pending = GLib.timeout_add(700, self._sms_check_once)

    def _sms_check_once(self):
        self._sms_pending = 0
        self.check_sms()
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
        fn, deferred = entry
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
            "has": {"sms": os.path.exists(CHATTY_DB),
                    "ofono": agent.modem is not None,
                    "calls": run("sh", "-c", "command -v gnome-calls") is not None}}


@command("status")
def cmd_status(agent, args):
    agent.status = agent.collect()
    return agent.status


@command("sms.threads")
def cmd_threads(agent, args):
    return list_threads(args.get("baseline", 0), args.get("seen"),
                        args.get("country", "49"))


@command("sms.messages")
def cmd_messages(agent, args):
    return list_messages(args["thread"], args.get("limit", 300),
                         args.get("country", "49"))


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


def _describe(schema, key, settings=None):
    s, k = _schema_key(schema, key)
    if k is None:
        return None
    settings = settings or Gio.Settings.new(schema)
    value = settings.get_value(key)
    rng = k.get_range().unpack()
    return {"schema": schema, "key": key, "value": value.unpack(),
            "text": value.print_(False), "type": k.get_value_type().dup_string(),
            "range": list(rng), "summary": k.get_summary() or "",
            "description": k.get_description() or "",
            "default": not settings.get_user_value(key)}


@command("gsettings.get")
def cmd_gs_get(agent, args):
    return {"%s %s" % (schema, key): _describe(schema, key)
            for schema, key in args["keys"]}


@command("gsettings.set")
def cmd_gs_set(agent, args):
    schema, key = args["schema"], args["key"]
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
        value = GLib.Variant(vtype, v)
    if not k.range_check(value):
        raise RuntimeError("value out of range")
    settings = Gio.Settings.new(schema)
    if not settings.set_value(key, value):
        raise RuntimeError("the setting is read only")
    Gio.Settings.sync()
    return _describe(schema, key, settings)


@command("gsettings.reset")
def cmd_gs_reset(agent, args):
    settings = Gio.Settings.new(args["schema"])
    settings.reset(args["key"])
    Gio.Settings.sync()
    return _describe(args["schema"], args["key"], settings)


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
    agent.schedule_refresh(100)
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
    agent.schedule_refresh(300)
    return bool(args["on"])


@command("power.set")
def cmd_power(agent, args):
    set_prop(agent.system, "org.freedesktop.UPower.PowerProfiles",
             "/org/freedesktop/UPower/PowerProfiles",
             "org.freedesktop.UPower.PowerProfiles", "ActiveProfile",
             GLib.Variant("s", args["profile"]))
    agent.schedule_refresh(300)
    return args["profile"]


@command("feedback.set")
def cmd_feedback(agent, args):
    set_prop(agent.session, "org.sigxcpu.Feedback", "/org/sigxcpu/Feedback",
             "org.sigxcpu.Feedback", "Profile", GLib.Variant("s", args["profile"]))
    agent.schedule_refresh(300)
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


@command("call")
def cmd_call(agent, args):
    number = normalize(args["number"], args.get("country", "49"))
    env = dict(os.environ)
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    subprocess.Popen(["gnome-calls", "--dial", number], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return number


# --- main ------------------------------------------------------------------

def _reader(agent):
    for line in sys.stdin.buffer:
        GLib.idle_add(agent.dispatch, line)
    GLib.idle_add(agent.loop.quit)


def main():
    loop = GLib.MainLoop()
    agent = Agent(loop)
    threading.Thread(target=_reader, args=(agent,), daemon=True).start()
    agent.schedule_refresh(0)
    loop.run()


if __name__ == "__main__":
    main()
