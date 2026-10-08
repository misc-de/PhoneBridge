#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The README's screenshots: PhoneBridge with invented people, messages,
calls and appointments. The "phone" is the agent running on this PC (the
tests' stand-in ssh) with a made-up chatty store, call history, VoiceBox
message and a stand-in evolution-data-server - nothing real is shown.

    DEMO_SHOT=data/screenshots/overview-light.png \
        dbus-run-session -- python3 tools/screenshot-demo.py
    DEMO_DARK=1 DEMO_SHOT=data/screenshots/overview-dark.png \
        dbus-run-session -- python3 tools/screenshot-demo.py
    python3 tools/screenshot-diagonal.py data/screenshots/overview-light.png \
        data/screenshots/overview-dark.png data/screenshots/overview.png

With DEMO_SHOT the window is drawn into that PNG once everything has come
and the demo ends; without, it stays open for DEMO_SECONDS (20). A made-up
music player plays, so the overview shows its bar.

DEMO_PAGE picks the page (overview, phone, messages, contacts, calendar,
files, screen, settings); DEMO_WIDTH and DEMO_HEIGHT the window's size;
DEMO_CONTACT a contact to open on the contacts page (with its calls,
messages and appointments); DEMO_LINES=1 opens the choice of the line on
the telephone page (two SIM cards and a SIP account, made up);
DEMO_THREAD a conversation (its number) to open on the messages page.
The files are a made-up home, never this PC's; the screen page shows the
desktop session (sharing the screen would record this PC's here)."""

import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import time

ROOT = os.getcwd()
sys.path.insert(0, ROOT)
work = tempfile.mkdtemp(prefix="phonebridge-demo-")
os.environ.update({
    "PATH": os.path.join(ROOT, "tests", "fakebin") + os.pathsep + os.environ["PATH"],
    # no desktop of its own: light or dark as asked, not as this desktop's theme
    "XDG_CURRENT_DESKTOP": "PhoneBridgeDemo",
    "PHONEBRIDGE_CONFIG": os.path.join(work, "config"),
    "XDG_CONFIG_HOME": os.path.join(work, "xdg"),
    "XDG_CACHE_HOME": os.path.join(work, "cache"),
    "PHONEBRIDGE_CHATTY_DB": os.path.join(work, "chatty.db"),
    "PHONEBRIDGE_CALLS_DB": os.path.join(work, "records.db"),
    "PHONEBRIDGE_VOICEBOX": os.path.join(work, "voicebox"),
    "PHONEBRIDGE_VOICEBOX_CONFIG": os.path.join(work, "voicebox.json"),
    "PHONEBRIDGE_DATA": os.path.join(work, "data"),
    "PHONEBRIDGE_ADDRESSBOOKS": os.path.join(work, "none"),
    "PHONEBRIDGE_KEYRING": "memory", "PHONEBRIDGE_LANGUAGE": "en", "LANGUAGE": "en",
    # English dates in GTK too (the calendar), the week from Monday
    "LC_ALL": "en_GB.UTF-8", "LANG": "en_GB.UTF-8",
    "GSETTINGS_BACKEND": "memory", "ADW_DISABLE_PORTAL": "1", "GDK_DEBUG": "no-portals",
    # the phone's files: a made-up home - the agent runs here
    "PHONEBRIDGE_FILES_HOME": os.path.join(work, "phone-home"),
    "PHONEBRIDGE_THUMBNAILS": os.path.join(work, "thumbnails"),
    # what a desktop session needs, as if the phone had it all
    "PHONEBRIDGE_GNOME_SHELL": os.path.join(work, "bin", "gnome-shell"),
    "PHONEBRIDGE_GRD": os.path.join(work, "bin", "gnome-remote-desktop-daemon"),
})
os.environ["PATH"] = os.path.join(work, "bin") + os.pathsep + os.environ["PATH"]
os.makedirs(os.path.join(work, "bin"))
for name, body in (("gnome-shell", "exit 0"), ("gnome-remote-desktop-daemon", "exit 0"),
                   ("grdctl", "exit 0"),
                   # sudo: may run iptables (desktop.check asks "-n -l"), nothing else
                   ("sudo", '[ "$1 $2" = "-n -l" ] && exit 0; exit 1')):
    with open(os.path.join(work, "bin", name), "w") as f:
        f.write("#!/bin/sh\n%s\n" % body)
    os.chmod(os.path.join(work, "bin", name), 0o755)
home = os.environ["PHONEBRIDGE_FILES_HOME"]
for folder, files in (("Documents", ("Invoice 2026-09.pdf", "Notes.txt", "Packing list.odt")),
                      ("Downloads", ("timetable.pdf", "recipe-lasagne.pdf")),
                      ("Music", ()), ("Pictures", ()), ("Videos", ()), ("Recordings", ()),
                      ("Pictures/Screenshots", ())):
    os.makedirs(os.path.join(home, folder), exist_ok=True)
    for name in files:
        with open(os.path.join(home, folder, name), "w") as f:
            f.write("demo\n" * 200)

from tests.support import Store  # noqa: E402

now = time.time()
today = dt.datetime.now().replace(second=0, microsecond=0)


def at(days=0, hour=None, minute=0):
    d = today + dt.timedelta(days=days)
    if hour is not None:
        d = d.replace(hour=hour, minute=minute)
    return d.timestamp()


# --- messages (invented) ---------------------------------------------------------
store = Store(os.environ["PHONEBRIDGE_CHATTY_DB"])
people = {"+4915550001001": "Anna Becker", "+4915550001002": "Tom Richter",
          "+4915550001003": "Lena Hoffmann", "+4915550001004": "Max Weber",
          "+4915550001005": "Sophie Krüger", "+4915550001006": "Paul Schulz",
          "+4915550001007": "Mia Wagner"}
msgs = [("+4915550001001", "Have you seen the new climbing gym by the river?", True,
         at(-1, 18, 2)),
        ("+4915550001001", "Not yet! Want to try it on Saturday?", False, at(-1, 18, 10)),
        ("+4915550001001", "Sure - 10 am?", True, at(-1, 18, 12)),
        ("+4915550001001", "Perfect 👍 I'll bring the shoes", False, at(-1, 18, 13)),
        ("+4915550001001", "Are we still on for lunch tomorrow?", True, at(0, 9, 12)),
        ("+4915550001001", "Yes, 12:30 at the usual place!", False, at(0, 9, 20)),
        ("+4915550001001", "Great, see you there", True, at(0, 9, 41)),
        ("+4915550001002", "The package arrived, thanks!", True, at(0, 8, 5)),
        ("+4915550001003", "Can you send me the photos from Saturday?", False, at(-1, 18, 30)),
        ("+4915550001004", "Running ten minutes late", True, at(-1, 17, 2)),
        ("+4915550001005", "Happy birthday! 🎉", False, at(-2, 8, 0)),
        ("+4915550001006", "Meeting moved to Thursday", True, at(-3, 14, 15)),
        ("+4915550001007", "Thanks for the recipe", True, at(-4, 20, 45))]
for number, body, incoming, when in msgs:
    store.add(number, body, incoming=incoming, at=when, member_alias=people[number])

# --- call history -------------------------------------------------------------------
db = sqlite3.connect(os.environ["PHONEBRIDGE_CALLS_DB"])
db.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, target TEXT, inbound INTEGER,"
           " start BLOB, answered BLOB, end BLOB, protocol TEXT)")


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


calls = [("+4915550001002", 1, at(0, 10, 2), None, 20),        # missed today
         ("+4915550001003", 1, at(0, 8, 47), 3, 260),
         ("+4915550001008", 1, at(0, 7, 58), 25, 60),          # voicebox, unknown number
         ("+4915550001004", 0, at(-1, 16, 40), 5, 95),
         ("+4915550001001", 1, at(-1, 12, 10), 2, 412),
         ("+4915550001006", 0, at(-2, 11, 30), None, 30),
         ("+4915550001005", 1, at(-3, 19, 5), 4, 180),
         ("+4915550001001", 0, at(-5, 9, 15), 6, 140),
         ("+4915550001001", 1, at(-9, 18, 2), None, 25)]
for i, (number, inbound, start, answered, length) in enumerate(calls, 1):
    db.execute("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, 'tel')",
               (i, number, inbound, iso(start),
                iso(start + answered) if answered is not None else None,
                iso(start + (answered or 0) + length)))
db.commit()
db.close()
# names for the call history come from the address book; the agent has none
# here, so chatty's names are what the threads show - calls show numbers
# unless named: give the agent a tiny vCard address book
book = os.path.join(work, "book", "system")
os.makedirs(book)
bdb = sqlite3.connect(os.path.join(book, "contacts.db"))
bdb.execute("CREATE TABLE folders (folder_id TEXT)")
bdb.execute("INSERT INTO folders VALUES ('folder_id')")
bdb.execute("CREATE TABLE folder_id (uid TEXT, is_list INTEGER, vcard TEXT)")
for number, name in people.items():
    bdb.execute("INSERT INTO folder_id VALUES (?, 0, ?)",
                (number, "BEGIN:VCARD\nFN:%s\nTEL:%s\nEND:VCARD" % (name, number)))
bdb.commit()
bdb.close()
os.environ["PHONEBRIDGE_ADDRESSBOOKS"] = os.path.join(work, "book")

# --- a VoiceBox message --------------------------------------------------------------
os.makedirs(os.path.join(work, "voicebox", "messages"))
with open(os.environ["PHONEBRIDGE_VOICEBOX_CONFIG"], "w") as f:
    json.dump({"boxes": [{"id": "global", "name": "General", "active": True}]}, f)
mid = time.strftime("%Y%m%d-%H%M%S", time.localtime(at(0, 7, 59)))
with open(os.path.join(work, "voicebox", "messages", mid + ".wav"), "wb") as f:
    f.write(b"RIFF....WAVEdemo")
with open(os.path.join(work, "voicebox", "messages", mid + ".json"), "w") as f:
    json.dump({"number": "+4915550001008", "name": "", "time": at(0, 7, 59),
               "duration": 12.0, "new": True, "box": "global", "missed": False}, f)

# --- the app --------------------------------------------------------------------------
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from phonebridge import config  # noqa: E402
from tests.fake_eds import CAL_UID, FakeEDS  # noqa: E402

eds = FakeEDS()


def vevent(uid, summary, start, end, location="", allday=False):
    if allday:
        s = "DTSTART;VALUE=DATE:%s\r\nDTEND;VALUE=DATE:%s" % (start, end)
    else:
        f = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
        s = "DTSTART:%s\r\nDTEND:%s" % (f(start), f(end))
    return ("BEGIN:VEVENT\r\nUID:%s\r\nSUMMARY:%s\r\n%s\r\n%sEND:VEVENT\r\n"
            % (uid, summary, s, ("LOCATION:%s\r\n" % location) if location else ""))


d = lambda days: (today + dt.timedelta(days=days)).strftime("%Y%m%d")  # noqa: E731
events = [("e1", "Lunch with Anna", at(1, 12, 30), at(1, 13, 30), "Café Central", False),
          ("e2", "Dentist", at(0, 16, 0), at(0, 16, 45), "Dr. Neumann", False),
          ("e3", "Team meeting", at(3, 10, 0), at(3, 11, 0), "Office", False),
          ("e4", "Tom's birthday", d(4), d(5), "", True),
          ("e5", "Yoga", at(5, 18, 30), at(5, 19, 30), "Studio 3", False),
          ("e6", "Train to Hamburg", at(7, 7, 42), at(7, 11, 5), "Main station", False)]
for uid, summary, s, e, loc, allday in events:
    eds.events[uid] = vevent(uid, summary, s, e, loc, allday)
for n, (number, name) in enumerate(sorted(people.items(), key=lambda p: p[1]), 1):
    first, last = name.split(" ", 1)
    extra = ("TEL;TYPE=WORK,VOICE:+49 30 5550 1234\r\nORG:Example GmbH\r\n"
             "EMAIL;TYPE=WORK:anna.becker@example.com\r\nBDAY:1991-04-17\r\n"
             "NOTE:Met at the climbing gym\r\n" if name == "Anna Becker" else "")
    eds.contacts["c%d" % n] = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c%d\r\nFN:%s\r\nN:%s;%s;;;\r\n"
        "TEL;TYPE=CELL:%s\r\nEMAIL;TYPE=INTERNET:%s@example.org\r\n%sEND:VCARD\r\n"
        % (n, name, last, first, number, first.lower(), extra))

config.CONFIG_DIR = os.environ["PHONEBRIDGE_CONFIG"]
# two conversations unread (Anna's last message, Tom), the others read - by
# the messages' ids: Anna 1-7, Tom 8, Lena 9, Max 10, Sophie 11, Paul 12, Mia 13
config.save(dict(config.DEFAULTS, language="en", devices=[
    {"id": "flx1s", "name": "FLX1s", "host": "phone", "user": "furios"}],
    screen={"quality": "normal", "mode": "desktop", "desktop_size": [1600, 900]},
    seen={"flx1s": {"baseline": 6, "threads": {
        "+4915550001004": 10, "+4915550001006": 12, "+4915550001007": 13}}}))

import phonebridge.tray as tray_mod  # noqa: E402
from phonebridge.app import PhoneBridgeApp  # noqa: E402


class NoTray:                           # no second icon in the user's panel
    def __init__(self, *a):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


tray_mod.Tray = NoTray
# the lines calls can go out on: two SIM cards and a SIP account (there is
# no ofono here to ask)
LINES = [{"id": "sim:demo1", "kind": "sim", "slot": 1, "operator": "Telco",
          "number": "+4915550009001", "ready": True},
         {"id": "sim:demo2", "kind": "sim", "slot": 2, "operator": "Mobilnet",
          "number": "+4915550009002", "ready": True},
         {"id": "sip:demo", "kind": "sip", "account": "demo", "name": "Office",
          "address": "me@sip.example", "ready": True}]


def demo_lines(self, dev):
    self.lines[dev.id] = [dict(line) for line in LINES]
    if self.window is not None:
        self.window.lines_changed(dev)


PhoneBridgeApp.refresh_lines = demo_lines
app = PhoneBridgeApp()
app.set_application_id("io.github.miscde.PhoneBridge.Demo")
app.send_notification = lambda *a: None
STATUS = {"hostname": "FuriPhone",
          "battery": {"percent": 78, "state": "charging", "time_to_full": 2400,
                      "time_to_empty": 0},
          "network": {"status": "registered", "operator": "Telco",
                      "technology": "lte", "strength": 82},
          "mobile_data": True, "wifi": {"enabled": True, "ssid": "HomeNet", "signal": 74},
          "volume": {"level": 0.5, "muted": False}, "power_profile": "balanced",
          "feedback_profile": "full", "pipewire": True, "call_audio": True}


def ready():
    dev = app.devices.get("flx1s")
    if dev is None or not dev.online or not app.threads.get("flx1s"):
        return True
    dev.status = dict(STATUS)
    dev.emit("changed")
    from phonebridge.window import MainWindow
    from gi.repository import Adw
    app.window = MainWindow(app)
    app.window.set_default_size(int(os.environ.get("DEMO_WIDTH", "1600")),
                                int(os.environ.get("DEMO_HEIGHT", "640")))
    page = os.environ.get("DEMO_PAGE", "overview")
    app.show_window(page)
    # after showing (that follows the desktop) - light or dark as asked, never
    # as this desktop is
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_DARK if os.environ.get("DEMO_DARK") else Adw.ColorScheme.FORCE_LIGHT)
    if page == "overview":
        app.window.overview.load(force=True)
    if os.environ.get("DEMO_CONTACT"):
        GLib.timeout_add(300, open_contact)
    if os.environ.get("DEMO_THREAD"):
        GLib.timeout_add(1000, lambda: app.window.messages.open_thread(
            os.environ["DEMO_THREAD"]) and False)
    if os.environ.get("DEMO_LINES"):
        GLib.timeout_add(3000, lambda: app.window.phone.line_picker.activate() and False)
    if os.environ.get("DEMO_SHOT"):
        GLib.timeout_add(3300, clear_typing)
        GLib.timeout_add(4000, shoot)
    return False


def open_contact():
    """The contact DEMO_CONTACT, once the address book has come."""
    contacts = app.window.contacts
    found = [c for c in contacts.contacts if c["name"] == os.environ["DEMO_CONTACT"]]
    if not found:
        return True
    contacts.show_contact(found[0])
    return False


def clear_typing():
    """The window shows on this desktop and may take what is typed there
    meanwhile: the search fields and the message being written emptied."""
    win = app.window
    for entry in (win.messages.search, win.contacts.search, win.messages.entry):
        if entry.get_text():
            entry.set_text("")
    return False


def open_popovers(widget):
    """Popovers shown inside widget - each a surface of its own, not in the
    window's picture."""
    from gi.repository import Gtk
    found = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Popover) and child.get_visible():
            found.append(child)
        found += open_popovers(child)
        child = child.get_next_sibling()
    return found


def shoot():
    """The window, drawn into DEMO_SHOT - an open popover (a list) on it,
    where it shows."""
    from gi.repository import Graphene, Gtk
    win = app.window
    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable(widget=win).snapshot(snap, win.get_width(), win.get_height())
    for pop in open_popovers(win):
        ok, bounds = pop.compute_bounds(win)
        if ok:
            snap.save()
            snap.translate(Graphene.Point().init(bounds.get_x(), bounds.get_y()))
            Gtk.WidgetPaintable(widget=pop).snapshot(snap, bounds.get_width(),
                                                     bounds.get_height())
            snap.restore()
    win.get_renderer().render_texture(snap.to_node(), None).save_to_png(os.environ["DEMO_SHOT"])
    print("saved %s (%dx%d)" % (os.environ["DEMO_SHOT"], win.get_width(), win.get_height()))
    app.quit()
    return False


def keep_status():
    dev = app.devices.get("flx1s")
    if dev is not None and dev.online and dev.status != STATUS:
        dev.status = dict(STATUS)       # the agent reports this PC's own battery ...
        dev.emit("changed")
    return True


from tests.fake_mpris import FakePlayer  # noqa: E402

player = FakePlayer("demo", title="Blue Morning", artist="The Examples")
player.status = "Playing"
GLib.timeout_add(300, ready)
GLib.timeout_add(500, keep_status)
GLib.timeout_add_seconds(int(os.environ.get("DEMO_SECONDS", "20")), lambda: app.quit() or False)
app.run(["phonebridge", "--background"])
eds.close()
player.close()
