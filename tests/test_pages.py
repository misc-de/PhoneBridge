# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The pages at work against the agent running here and a stand-in
evolution-data-server: contacts and appointments added, changed and
deleted through their editors, the devices dialog, the call bar, the panel
menu, VoiceBox's messages. Dialogs are answered by the test; the window is
never shown and nothing is played."""

import datetime as dt
import json
import os
import time
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from phonebridge import config, i18n  # noqa: E402

from .support import ANNA, Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class Answer:
    """Answers the next alert dialogs: each entry is a response id, or a
    function(dialog) returning one (to fill in the dialog's fields first)."""

    def __init__(self):
        self.queue = []
        self.seen = []

    def present(self, dialog, parent=None):
        self.seen.append(dialog.get_heading())
        answer = self.queue.pop(0) if self.queue else "cancel"
        if callable(answer):
            answer = answer(dialog)
        GLib.idle_add(lambda: dialog.emit("response", answer) and False)


def entry_rows(widget):
    out, stack = [], [widget]
    while stack:
        w = stack.pop(0)
        if isinstance(w, Adw.EntryRow):
            out.append(w)
        c = w.get_first_child()
        while c is not None:
            stack.append(c)
            c = c.get_next_sibling()
    return out


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from phonebridge.app import PhoneBridgeApp
        from .fake_eds import FakeEDS
        cls.eds = FakeEDS()
        cls.addClassCleanup(cls.eds.close)       # also when setting up fails
        cls.home = Home()
        cls.addClassCleanup(cls.home.cleanup)
        cls.home.store.add(ANNA, "Hallo", member_alias="Anna")
        cls.vb = os.path.join(cls.home.dir, "voicebox")
        os.makedirs(os.path.join(cls.vb, "messages"))
        with open(os.path.join(cls.vb, "messages", "20261007-080000.json"), "w") as f:
            json.dump({"number": ANNA, "name": "Anna", "time": time.time() - 60,
                       "duration": 7.0, "new": True, "box": "global", "missed": False}, f)
        with open(os.path.join(cls.vb, "messages", "20261007-080000.wav"), "wb") as f:
            f.write(b"RIFF....WAVEfake")
        cls.answer = Answer()
        cls.patches = [
            mock.patch.dict(os.environ, dict(cls.home.env(), PHONEBRIDGE_VOICEBOX=cls.vb,
                                             PHONEBRIDGE_SIP_KEYFILE=os.path.join(
                                                 cls.home.dir, "calls", "sip-account.cfg"),
                                             XDG_CACHE_HOME=os.path.join(cls.home.dir, "c"))),
            mock.patch.object(config, "CONFIG_DIR", cls.home.config),
            mock.patch.object(config, "AUTOSTART", os.path.join(cls.home.dir, "auto.desktop")),
            mock.patch.object(Adw.AlertDialog, "present",
                              lambda dialog, parent=None: cls.answer.present(dialog, parent)),
        ]
        for p in cls.patches:
            p.start()
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        cls.notified = []
        cls.app = PhoneBridgeApp()
        # test_app's app is still on the bus under the real id
        cls.app.set_application_id("io.github.miscde.PhoneBridge.TestPages")
        cls.app.send_notification = lambda nid, n: cls.notified.append(nid)
        cls.app.withdraw_notification = lambda nid: None
        cls.app.register(None)
        cls.dev = cls.app.devices["test"]
        assert run_loop_until(lambda: cls.dev.online and "test" in cls.app.voicebox, 20)
        from phonebridge.window import MainWindow
        cls.app.window = cls.win = MainWindow(cls.app)
        # showing the window is presenting it - not in a test
        cls.app.show_window = lambda page=None: (page and cls.win.show_page(page)) or cls.win

    @classmethod
    def tearDownClass(cls):
        cls.win.destroy()
        cls.app.do_shutdown()
        for p in cls.patches:
            p.stop()
        i18n.setup("en")

    def wait(self, predicate, timeout=15):
        self.assertTrue(run_loop_until(predicate, timeout))

    # -- contacts --------------------------------------------------------------------
    def test_contacts_add_change_delete(self):
        page = self.win.contacts
        page.load(force=True)
        self.wait(lambda: page.sources)
        editor_holder = {}
        orig = Adw.Dialog.present

        def capture(dialog, parent=None):
            editor_holder["d"] = dialog
        with mock.patch.object(Adw.Dialog, "present", capture):
            page.edit(None, number="0155 50000003")
        editor = editor_holder["d"]
        self.assertFalse(editor.save.get_sensitive())        # a name is needed
        editor.given.set_text("Carla")
        editor.family.set_text("Neu")
        editor.birthday.set_text("1.5.1985")
        self.assertTrue(editor.save.get_sensitive())
        editor.birthday.set_text("nonsense")
        self.assertFalse(editor.save.get_sensitive())
        editor.birthday.set_text("01.05.1985")
        editor.emails.add("carla@example.org", None)
        editor._save()
        self.wait(lambda: any("Carla" in v for v in self.eds.contacts.values()))
        self.wait(lambda: any(c["name"] == "Carla Neu" for c in page.contacts))
        carla = next(c for c in page.contacts if c["name"] == "Carla Neu")
        self.assertEqual(carla["birthday"], "1985-05-01")
        self.assertEqual(carla["phones"][0]["value"], "0155 50000003")
        self.assertIs(page.find_number("+4915550000003"), carla)
        self.assertTrue(page.list.get_row_at_index(0).matches("carla"))
        self.assertTrue(page.list.get_row_at_index(0).matches("50000003"))

        page._show(carla)
        self.assertTrue(page.edit_button.get_sensitive())
        with mock.patch.object(Adw.Dialog, "present", capture):
            page.edit(carla)
        editor = editor_holder["d"]
        editor.org.set_text("Firma")
        editor._drop_photo()
        editor._save()
        self.wait(lambda: any(c.get("org") == "Firma" for c in page.contacts))

        self.answer.queue.append("delete")
        page.delete(next(c for c in page.contacts if c["org"] == "Firma"))
        self.wait(lambda: not self.eds.contacts)
        self.wait(lambda: not page.contacts)
        Adw.Dialog.present = orig

    # -- appointments -------------------------------------------------------------------
    def test_appointments_add_and_delete(self):
        page = self.win.calendar
        page.load(force=True)
        self.wait(lambda: page.sources)
        holder = {}
        with mock.patch.object(Adw.Dialog, "present",
                               lambda d, parent=None: holder.update(d=d)):
            page.edit(None)
        editor = holder["d"]
        self.assertFalse(editor.save.get_sensitive())        # a title is needed
        editor.summary.set_text("Zahnarzt")
        tomorrow = dt.date.today() + dt.timedelta(days=1)
        editor.start_date.set_date(tomorrow)
        editor.start_time.set_time(10, 0)
        editor._on_start()
        self.assertEqual(editor.end_date.get_date(), tomorrow)  # the end follows
        editor.end_time.set_time(9, 0)
        editor._check()
        self.assertFalse(editor.save.get_sensitive())        # ends before it starts
        editor.end_time.set_time(11, 0)
        editor._check()
        editor.location.set_text("Praxis")
        editor._save()
        self.wait(lambda: any("Zahnarzt" in v for v in self.eds.events.values()))
        self.wait(lambda: any(e["summary"] == "Zahnarzt" for e in page.events))
        page.select(tomorrow)
        page._fill()
        self.assertEqual(page.agenda_stack.get_visible_child_name(), "agenda")
        ev = next(e for e in page.events if e["summary"] == "Zahnarzt")
        self.assertEqual(ev["location"], "Praxis")
        self.assertEqual(time.localtime(ev["start"]).tm_hour, 10)

        self.answer.queue.append("delete")
        page.show_event(ev)                                   # details: delete ...
        self.answer.queue.append("all")                       # ... confirmed
        self.wait(lambda: not self.eds.events)

    def test_calendar_toggle(self):
        page = self.win.calendar
        page.load(force=True)
        self.wait(lambda: page.cal_list.get_row_at_index(0))
        row = page.cal_list.get_row_at_index(0)
        row.set_active(False)
        self.assertIn("cal-personal", self.app.cfg["hidden_calendars"])
        row.set_active(True)
        self.assertNotIn("cal-personal", self.app.cfg["hidden_calendars"])

    # -- devices ------------------------------------------------------------------------
    def test_devices_dialog(self):
        from phonebridge.devices import DevicesDialog
        dialog = DevicesDialog(self.app)
        self.assertEqual(len(dialog.rows), 1)

        def fill(d):
            name, host, user, port = entry_rows(d.get_extra_child())
            name.set_text("Zweites")
            host.set_text("phone")
            port.set_text("2222")
            return "save"
        self.answer.queue.append(fill)
        dialog.edit(None)
        self.wait(lambda: len(self.app.cfg["devices"]) == 2)
        added = self.app.cfg["devices"][1]
        self.assertEqual((added["id"], added["port"], added["user"]), ("zweites", 2222, "furios"))
        self.answer.queue.append("remove")
        dialog.remove(added)
        self.wait(lambda: len(self.app.cfg["devices"]) == 1)
        self.assertEqual(len(dialog.rows), 1)
        dialog.close()

    # -- calls, the panel menu, VoiceBox ----------------------------------------------
    def test_call_bar(self):
        bar = self.win.callbar
        call = {"path": "/c1", "state": "incoming", "number": ANNA, "name": "Anna",
                "avatar": None}
        self.app._on_calls(self.dev, [call])
        self.assertTrue(bar.get_reveal_child())
        self.assertTrue(bar.answer.get_visible())
        self.assertEqual(bar.who.get_label(), "Anna")
        self.assertIn("call-%s-/c1" % self.dev.id, self.notified)
        labels = [i.get("label") for i in self.app.menu_items()]
        self.assertIn("Answer", labels)
        self.app._on_calls(self.dev, [dict(call, state="active")])
        self.assertFalse(bar.answer.get_visible())
        self.assertIsNotNone(self.app.calls["test"][0].get("since"))
        self.app._on_calls(self.dev, [])
        self.assertFalse(bar.get_reveal_child())

    def test_tray_items(self):
        sent = []
        with mock.patch.object(self.dev, "request",
                               lambda cmd, args=None, cb=None: sent.append(cmd)):
            for item in ("show", "messages", "phone", "compose", "ring"):
                self.app.on_tray_item(item)
        self.assertIn("ring", sent)
        self.assertEqual(self.win.current_page(), "messages")
        self.answer.queue.clear()

    def test_voicebox_in_the_calls(self):
        page = self.win.phone
        page.calls = []
        page._refill()
        row = page.list.get_row_at_index(0)
        self.assertEqual(row.call["voicebox"]["id"], "20261007-080000")
        self.assertTrue(row.call["voicebox"]["new"])
        self.assertEqual(self.app.new_voicemails(), 1)
        played = []
        fake = mock.Mock()
        fake.get_ended.return_value = False
        with mock.patch.object(Gtk.MediaFile, "new_for_filename",
                               lambda path: played.append(path) or fake):
            page.toggle_play("20261007-080000")
            self.wait(lambda: played)
        self.assertTrue(played[0].endswith("test-20261007-080000.wav"))
        self.wait(lambda: self.app.new_voicemails() == 0)    # heard
        page.toggle_play("20261007-080000")                   # stop
        self.assertIsNone(page.playing)
        self.answer.queue.append("delete")
        page.delete_voicemail({"id": "20261007-080000"}, "Anna")
        self.wait(lambda: not self.app.voicemails("test"))
        self.assertFalse(os.path.exists(os.path.join(self.vb, "messages",
                                                     "20261007-080000.json")))


    def test_sip_account(self):
        page = self.win.settings
        holder = {}
        with mock.patch.object(Adw.Dialog, "present", lambda d, parent=None: holder.update(d=d)):
            page.edit_sip(None)
        editor = holder["d"]
        self.assertFalse(editor.save.get_sensitive())        # server, user, password needed
        editor.host.set_text("voip.example.net")
        editor.user.set_text("me")
        editor.password.set_text("geheim")
        editor.name.set_text("Privat")
        editor.protocol.set_selected(2)                       # TLS
        self.assertTrue(editor.save.get_sensitive())
        editor._save()
        self.wait(lambda: page.sip_group.rows_)
        row = page.sip_group.rows_[0]
        self.assertEqual(row.get_title(), "Privat")
        self.assertIn("TLS", row.get_subtitle())
        self.wait(lambda: any(l["id"] == "sip:me@voip.example.net"
                              for l in self.app.lines.get("test", [])))
        self.answer.queue.append("remove")
        page.delete_sip({"id": "me@voip.example.net", "display_name": "Privat",
                         "user": "me", "host": "voip.example.net"})
        self.wait(lambda: not page.sip_group.rows_)


class Widgets(unittest.TestCase):
    def test_parse_date(self):
        from phonebridge.widgets import parse_date
        self.assertEqual(parse_date("7.10.2026"), dt.date(2026, 10, 7))
        self.assertEqual(parse_date("2026-10-07"), dt.date(2026, 10, 7))
        self.assertEqual(parse_date("07.10.26"), dt.date(2026, 10, 7))
        self.assertEqual(parse_date("24.12."), dt.date(dt.date.today().year, 12, 24))
        self.assertIsNone(parse_date("morgen"))

    def test_day_title(self):
        from phonebridge.widgets import day_title
        today = dt.date(2026, 10, 7)
        self.assertEqual(day_title(today, today), "Today, 7 October")
        self.assertEqual(day_title(dt.date(2026, 10, 8), today), "Tomorrow, 8 October")
        self.assertEqual(day_title(dt.date(2026, 10, 10), today), "Saturday, 10 October")
        self.assertEqual(day_title(dt.date(2027, 1, 1), today), "Friday, 1 January 2027")

    @unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
    def test_date_and_time(self):
        from phonebridge.widgets import DateButton, TimeEntry
        b = DateButton(dt.date(2026, 10, 7))
        changed = []
        b.connect("changed", lambda *a: changed.append(1))
        b.calendar.select_day(GLib.DateTime.new_local(2026, 10, 9, 0, 0, 0))
        self.assertEqual(b.get_date(), dt.date(2026, 10, 9))
        self.assertEqual(changed, [1])
        t = TimeEntry(9, 30)
        self.assertEqual(t.get_time(), (9, 30))


if __name__ == "__main__":
    unittest.main()
