# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The phone's own notifications on the desktop: a made-up notification
daemon on the private session bus stands in for Phosh's; the agent
watches it as a monitor and the PC shows - and closes - what it sees."""

import os
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from phonebridge import agent, config  # noqa: E402

from .fake_notify import FakeNotifyDaemon  # noqa: E402
from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class Pieces(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(agent.plain_text("<b>Hi</b> &amp; <i>bye</i> &lt;3"), "Hi & bye <3")

    def test_what_phonebridge_tells_itself_stays_out(self):
        for app, entry in (("Chats", ""), ("Calls", ""), ("x", "sm.puri.Chatty"),
                           ("x", "org.gnome.Calls"), ("VoiceBox", ""), ("PhoneBridge", "")):
            self.assertTrue(agent.notification_skipped(app, entry), (app, entry))
        self.assertFalse(agent.notification_skipped("Fractal", "org.gnome.Fractal"))


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class OnThePC(unittest.TestCase):
    def test_shown_and_closed_both_ways(self):
        from phonebridge.app import PhoneBridgeApp
        daemon = FakeNotifyDaemon()
        self.addCleanup(daemon.close)
        home = Home()
        self.addCleanup(home.cleanup)
        for p in (mock.patch.dict(os.environ, home.env()),
                  mock.patch.object(config, "CONFIG_DIR", home.config)):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestPhoneNotes")
        shown, withdrawn = [], []
        app.send_notification = lambda nid, n: shown.append(nid)
        app.withdraw_notification = withdrawn.append
        app.register(None)
        self.addCleanup(app.do_shutdown)
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online, 20))
        watching = []
        dev.request("notifications.watch", {"on": True}, lambda r, e: watching.append((r, e)))
        self.assertTrue(run_loop_until(lambda: watching, 10))
        self.assertEqual(watching[0], (True, None))

        titles, bodies = [], []
        orig_new = Gio.Notification.new
        with mock.patch.object(Gio.Notification, "new",
                               lambda t: titles.append(t) or orig_new(t)), \
                mock.patch.object(Gio.Notification, "set_body",
                                  lambda self, b: bodies.append(b)):
            nid = daemon.notify("Fractal", "Bob", "<b>Hi</b> &amp; bye",
                                {"desktop-entry": GLib.Variant("s", "org.gnome.Fractal")})
            self.assertTrue(run_loop_until(lambda: "phone-test-%d" % nid in shown, 10))
            self.assertIn("Fractal: Bob", titles)
            from phonebridge.app import notification_text
            self.assertIn(notification_text("Hi & bye"), bodies)    # escaped for the daemon
            daemon.notify("Chats", "Anna", "an SMS - PhoneBridge tells that itself")
            # an app that runs on this PC too tells it here itself
            with mock.patch.object(app.local_apps, "running",
                                   lambda name, entry="": name == "Element"):
                daemon.notify("Element", "Carl", "twice?")
                daemon.notify("Calendar", "Dentist", "")
                self.assertTrue(run_loop_until(lambda: len(shown) == 2, 10))
                run_loop_until(lambda: False, 0.3)
            self.assertNotIn("Chats: Anna", titles)
            self.assertNotIn("Element: Carl", titles)
            # ... unless wanted twice
            app.activate_action("phone-notifications-twice", None)
            self.assertTrue(app.cfg["phone_notifications_twice"])
            with mock.patch.object(app.local_apps, "running", lambda *a: True):
                daemon.notify("Element", "Carl", "twice!")
                self.assertTrue(run_loop_until(lambda: "Element: Carl" in titles, 10))
            app.activate_action("phone-notifications-twice", None)
            shown[2:] = []

        # closed here with the button: closed on the phone, gone here
        app.activate_action("phone-dismiss", GLib.Variant("(su)", ("test", nid)))
        self.assertTrue(run_loop_until(lambda: nid in daemon.closed, 10))
        self.assertIn("phone-test-%d" % nid, withdrawn)
        # closed on the phone: gone here
        other = int(shown[1].rsplit("-", 1)[1])
        daemon.conn.emit_signal(None, "/org/freedesktop/Notifications",
                                "org.freedesktop.Notifications", "NotificationClosed",
                                GLib.Variant("(uu)", (other, 2)))
        self.assertTrue(run_loop_until(lambda: "phone-test-%d" % other in withdrawn, 10))

        # switched off: nothing more comes
        app.activate_action("phone-notifications", None)
        self.assertFalse(app.cfg["phone_notifications"])
        run_loop_until(lambda: False, 0.5)
        daemon.notify("Fractal", "Bob", "later")
        run_loop_until(lambda: False, 1)
        self.assertEqual(len(shown), 2)


if __name__ == "__main__":
    unittest.main()
