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


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class App(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from phonebridge.app import PhoneBridgeApp
        cls.home = Home()
        cls.home.store.add(ANNA, "Alt", member_alias="Anna")
        cls.patches = [mock.patch.dict(os.environ, cls.home.env()),
                       mock.patch.object(config, "CONFIG_DIR", cls.home.config),
                       mock.patch.object(config, "AUTOSTART",
                                         os.path.join(cls.home.dir, "autostart.desktop"))]
        for p in cls.patches:
            p.start()
        config.save(dict(config.DEFAULTS, devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        cls.notified = []
        cls.app = PhoneBridgeApp()
        # no notification daemon gets started, not even on the private bus
        cls.app.send_notification = lambda nid, n: cls.notified.append(nid)
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
        mid = self.home.store.add(BERND, "Neu da")
        self.assertTrue(run_loop_until(lambda: new and self.app.unread() == 1, 30))
        self.assertEqual(self.notified, ["sms-test-%d" % mid])
        labels = [i.get("label") for i in self.app.menu_items()]
        self.assertIn("1 unread message", labels)
        self.assertIn("1 unread message", self.app.tray.tooltip[1])

        self.app.mark_seen("test", BERND, mid)
        self.assertEqual(self.app.unread(), 0)
        self.assertEqual(config.load()["seen"]["test"]["threads"][BERND], mid)

    def test_3_window_pages(self):
        win = self.window()
        win.devices_changed()
        self.assertEqual(win.body.get_visible_child_name(), "pages")
        self.assertIn("Connected", win.overview.conn.get_subtitle())
        self.assertEqual(win.overview.wifi.get_subtitle(), "Testnetz · 57 %")
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

        win.settings.load()
        self.assertTrue(run_loop_until(lambda: win.settings.values, 10))

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


if __name__ == "__main__":
    unittest.main()
