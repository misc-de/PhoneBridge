# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The whole app against the agent running here: connection, unread
counts, panel menu, window pages, language. The window is built but never
shown."""

import os
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from phonebridge import config, i18n  # noqa: E402

from .support import ANNA, BERND, Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _children(page):
    """The groups of a preferences page."""
    from gi.repository import Adw
    out, stack = [], [page]
    while stack:
        w = stack.pop()
        if isinstance(w, Adw.PreferencesGroup):
            out.append(w)
            continue
        child = w.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return out


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class App(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from phonebridge.app import PhoneBridgeApp
        cls.home = Home()
        cls.home.store.add(ANNA, "Old", member_alias="Anna")
        cls.patches = [mock.patch.dict(os.environ, cls.home.env()),
                       mock.patch.object(config, "CONFIG_DIR", cls.home.config),
                       mock.patch.object(config, "AUTOSTART",
                                         os.path.join(cls.home.dir, "autostart.desktop"))]
        for p in cls.patches:
            p.start()
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        cls.notified = []
        cls.app = PhoneBridgeApp()
        # no notification daemon gets started, not even on the private bus
        cls.app.send_notification = lambda nid, n: cls.notified.append(nid)
        cls.withdrawn = []
        cls.app.withdraw_notification = lambda nid: cls.withdrawn.append(nid)
        cls.app.register(None)
        cls.dev = cls.app.devices["test"]
        assert run_loop_until(lambda: cls.dev.online and "test" in cls.app.threads, 20), \
            cls.dev.error

    @classmethod
    def tearDownClass(cls):
        if cls.app.window is not None:
            cls.app.window.destroy()
        cls.app.do_shutdown()
        for p in cls.patches:
            p.stop()
        cls.home.cleanup()
        i18n.setup("en")

    def window(self):
        if self.app.window is None:
            from phonebridge.window import MainWindow
            self.app.window = MainWindow(self.app)
        return self.app.window

    def test_1_old_messages_are_not_unread(self):
        self.assertEqual(config.load()["seen"]["test"]["baseline"],
                         self.dev.hello["sms_last_id"])
        self.assertEqual(self.app.unread(), 0)

    def test_2_new_message_counts_until_seen(self):
        new = []
        hid = self.dev.connect("sms", lambda d, n: new.extend(n))
        self.addCleanup(self.dev.disconnect, hid)
        mid = self.home.store.add(BERND, "Just in")
        self.assertTrue(run_loop_until(lambda: new and self.app.unread() == 1, 30))
        self.assertEqual(self.notified, ["sms-test-%d" % mid])
        labels = [i.get("label") for i in self.app.menu_items()]
        self.assertIn("1 unread message", labels)
        self.assertIn("1 unread message", self.app.tray.tooltip[1])

        self.app.mark_seen("test", BERND, mid)
        self.assertEqual(self.app.unread(), 0)
        self.assertIn("sms-test-%d" % mid, self.withdrawn)   # read here: gone there too
        self.assertEqual(config.load()["seen"]["test"]["threads"][BERND], mid)

    def test_3_window_pages(self):
        win = self.window()
        win.devices_changed()
        self.assertEqual(win.body.get_visible_child_name(), "pages")
        self.assertEqual(win.overview.conn.value.get_label(), "Connected")
        self.assertEqual(win.overview.wifi.value.get_label(), "Testnet · 57 %")
        # the cards: conversations from the app; calls and appointments are
        # fetched when the page is on screen, so fill them by hand here
        # filled when the window is built - the newest, not only new ones
        self.assertTrue(win.overview.messages_card.list.get_row_at_index(0))
        win.overview.calls = [{"number": "+4915550000001", "name": "Anna", "inbound": True,
                               "answered": False, "start": __import__("time").time(),
                               "duration": 0}]
        win.overview.show_calls()
        first = win.overview.calls_card.list.get_row_at_index(0)
        self.assertTrue(first.has_css_class("fresh"))                 # missed today
        # the settings page carries the switches now
        self.assertIn(win.settings.quick.groups[0], list(_children(win.settings)))
        self.assertFalse(win.banner.get_revealed())

        msgs = win.messages
        self.assertTrue(run_loop_until(lambda: msgs.list.get_row_at_index(0), 5))
        titles = []
        n = 0
        while (row := msgs.list.get_row_at_index(n)) is not None:
            titles.append(row.thread["title"])
            n += 1
        self.assertEqual(titles[-1], "Anna")
        msgs.open_thread("0155 50000001")      # same number, typed differently
        self.assertEqual(msgs.thread, ANNA)
        self.assertTrue(run_loop_until(lambda: msgs.bubbles.get_row_at_index(0), 10))
        self.assertTrue(msgs.compose_bar.get_visible())
        msgs.entry.set_text("x" * 161)
        self.assertEqual(msgs.counter.get_label(), "161 · 2 SMS")
        self.assertTrue(msgs.send_button.get_sensitive())
        msgs.entry.set_text("")
        self.assertFalse(msgs.send_button.get_sensitive())

        from phonebridge.settings_spec import SECTIONS
        appearance = next(sec for sec in SECTIONS if sec["id"] == "appearance")
        win.settings.load(appearance)
        self.assertTrue(run_loop_until(lambda: win.settings.values, 10))
        dark = next(r for r in win.settings.rows["appearance"] if r.key == "color-scheme")
        self.assertTrue(dark.widget.get_visible())          # the PC has the key, too
        missing = next(r for r in win.settings.rows["calls"])
        self.assertFalse(missing.widget.get_visible())      # no GNOME Calls here: hidden

    def test_4_offline(self):
        win = self.window()
        self.dev.stop()
        self.assertFalse(self.dev.online)
        self.assertTrue(win.banner.get_revealed())
        self.assertFalse(win.messages.entry.get_sensitive())
        ids = [i.get("id") for i in self.app.menu_items()]
        self.assertIn("reconnect", ids)
        self.dev.reconnect()
        self.assertTrue(run_loop_until(lambda: self.dev.online, 20))
        self.assertFalse(win.banner.get_revealed())

    def test_5_language(self):
        self.window()
        self.app.activate_action("language", GLib.Variant("s", "de"))
        self.assertEqual(config.load()["language"], "de")
        labels = [i.get("label") for i in self.app.menu_items()]
        self.assertIn("PhoneBridge öffnen", labels)
        self.assertIsNone(self.app.window)     # rebuilt when shown next
        self.app.activate_action("language", GLib.Variant("s", "en"))

    def test_6_devices(self):
        devices = config.load()["devices"] + [
            {"id": "two", "name": "Second", "host": "phone", "user": "me"}]
        self.app.set_devices(devices)
        self.assertEqual(list(self.app.devices), ["test", "two"])
        radios = [i for i in self.app.menu_items() if i.get("radio")]
        self.assertEqual([r["label"] for r in radios], ["Testphone", "Second"])
        self.app.set_active("two")
        self.assertEqual(config.load()["active"], "two")
        self.app.set_devices(devices[:1])
        self.assertEqual(self.app.cfg["active"], "test")
        self.assertNotIn("two", config.load()["seen"])

    def test_7_dial_where_to_talk(self):
        sent = []
        with mock.patch.object(self.dev, "request",
                               lambda cmd, args=None, cb=None: sent.append((cmd, args))):
            # the local "phone" has no call audio nodes: no question, straight away
            self.assertFalse(self.app.call_audio_possible(self.dev))
            self.app.dial("0155 50000001")
        self.assertEqual(sent[0][0], "call")
        self.assertEqual(sent[0][1]["number"], "0155 50000001")

        # dialled to talk at the PC: the sound moves once the call is being set up
        started = []
        with mock.patch.object(self.app, "set_pc_audio",
                               lambda dev, on, test=False: started.append(on)):
            self.app._pc_wanted[self.dev.id] = __import__("time").time()
            self.app._on_calls(self.dev, [{"path": "/c1", "state": "dialing", "number": "1",
                                           "name": "", "avatar": None}])
            self.assertEqual(started, [True])
            self.assertNotIn(self.dev.id, self.app._pc_wanted)
            self.app._on_calls(self.dev, [])

    def test_8_links(self):
        from phonebridge.app import parse_link
        self.assertEqual(parse_link("tel:+49%20155%2050000001"), ("tel", "+4915550000001", ""))
        self.assertEqual(parse_link("callto://0155-50000001;ext=1"), ("tel", "015550000001", ""))
        self.assertEqual(parse_link("sms:+4915550000001,+4915550000002?body=Hi%20there"),
                         ("sms", "+4915550000001", "Hi there"))
        for bad in ("tel:", "tel:abc", "mailto:anna@example.org", "https://example.org", "x"):
            self.assertIsNone(parse_link(bad), bad)
        win = self.window()
        with mock.patch.object(win, "present", lambda: None):
            self.app.open_link("tel:0155%2050000001")
            self.assertEqual(win.current_page(), "phone")
            self.assertEqual(win.phone.number.get_text(), "015550000001")    # not dialled
            self.app.open_link("sms:+4915550000002?body=See%20you")
            self.assertEqual(win.current_page(), "messages")
            self.assertEqual(win.messages.entry.get_text(), "See you")
            win.messages.entry.set_text("")

    def test_9_no_panel_icon(self):
        """No panel shows the icon: told once per desktop, what would show it."""
        self.app.cfg.pop("tray_hint_told", None)
        tray = self.app.tray
        self.addCleanup(setattr, self.app, "tray", tray)
        self.app.tray = mock.Mock(hosted=True)
        self.notified.clear()
        self.app.check_tray()
        self.assertEqual(self.notified, [])                  # shown: nothing to say
        self.app.tray = mock.Mock(hosted=False)
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            self.app.check_tray()
            self.app.check_tray()
        self.assertEqual(self.notified, ["tray"])            # once
        self.assertEqual(self.app.cfg["tray_hint_told"], "gnome")
        info = self.app.debug_info()
        self.assertIn("Panel icon: no panel shows it", info)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class FirstConnection(unittest.TestCase):
    """The window is there before the phone is online - as after setting a
    phone up: the overview must fill by itself, without switching pages."""

    def test_overview_fills_when_the_phone_comes(self):
        from phonebridge.app import PhoneBridgeApp
        from phonebridge.window import MainWindow
        home = Home()
        self.addCleanup(home.cleanup)
        home.store.add(ANNA, "Hello", member_alias="Anna")
        for p in (mock.patch.dict(os.environ, home.env()),
                  mock.patch.object(config, "CONFIG_DIR", home.config),
                  mock.patch.object(Gtk.Widget, "get_mapped", lambda self: True)):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en"))
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestFirst")
        app.send_notification = lambda *a: None
        app.register(None)
        app.window = win = MainWindow(app)           # built with no phone at all
        self.addCleanup(lambda: (win.destroy(), app.do_shutdown()))
        app.set_devices([{"id": "test", "name": "Test", "host": "phone", "user": "me"}])
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online, 15))
        rows = lambda card: card.list.get_row_at_index(0) is not None  # noqa: E731
        self.assertTrue(run_loop_until(lambda: rows(win.overview.messages_card), 10))
        self.assertTrue(run_loop_until(lambda: win.overview._loaded_for is dev, 10))
