# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Music on the phone: the agent sees the players (a made-up one on the
private session bus), tells the PC what plays, and passes play/pause and
next on."""

import os
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, Gtk  # noqa: E402

from phonebridge import agent, config  # noqa: E402

from .fake_mpris import FakePlayer  # noqa: E402
from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def session():
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception:  # noqa: BLE001
        return None


@unittest.skipUnless(session() is not None, "no session bus")
class Players(unittest.TestCase):
    def test_players_playing_first(self):
        a = FakePlayer("first", title="Quiet", artist="Nobody")
        b = FakePlayer("second")
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.status = "Playing"
        players = [p for p in agent.media_players(session())
                   if p["bus"] in (a.name, b.name)]
        self.assertEqual([p["bus"] for p in players], [b.name, a.name])
        p = players[0]
        self.assertEqual((p["title"], p["artist"], p["identity"], p["status"]),
                         ("Blue Morning", "The Examples", "Example Player", "Playing"))
        self.assertTrue(p["can_next"])
        self.assertFalse(p["can_prev"])

    def test_control_checks_what_it_is_asked(self):
        fake = mock.Mock(session=session())
        for args in ({"bus": "org.freedesktop.DBus", "action": "PlayPause"},
                     {"bus": "org.mpris.MediaPlayer2.x", "action": "Quit"}):
            with self.assertRaisesRegex(RuntimeError, "unknown player"):
                agent.cmd_media_control(fake, args)


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class OnThePC(unittest.TestCase):
    def test_what_plays_and_control(self):
        from phonebridge.app import PhoneBridgeApp, media_line
        from phonebridge.window import MainWindow
        home = Home()
        self.addCleanup(home.cleanup)
        for p in (mock.patch.dict(os.environ, home.env()),
                  mock.patch.object(config, "CONFIG_DIR", home.config)):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestMedia")
        app.send_notification = lambda *a: None
        app.register(None)
        self.addCleanup(app.do_shutdown)
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online, 20))
        app.window = win = MainWindow(app)
        self.addCleanup(win.destroy)
        self.assertFalse(win.overview.media.get_visible())

        fake = FakePlayer("pbtest")                 # a player starts on the phone
        self.addCleanup(fake.close)
        mine = lambda: [p for p in app.media.get("test", []) if p["bus"] == fake.name]  # noqa
        self.assertTrue(run_loop_until(mine, 10))
        self.assertEqual(media_line(mine()[0]), "♪ Blue Morning – The Examples")
        labels = [i.get("label") for i in app.menu_items()]
        self.assertIn("♪ Blue Morning – The Examples", labels)
        self.assertIn("Play", labels)
        self.assertTrue(win.overview.media.get_visible())

        with mock.patch.object(app, "player", lambda dev_id=None: mine()[0]):
            app.media_control("PlayPause")
        self.assertTrue(run_loop_until(lambda: mine() and mine()[0]["status"] == "Playing", 10))
        self.assertEqual(fake.calls, ["PlayPause"])

        fake.close()                                # it ends: gone on the PC as well
        self.assertTrue(run_loop_until(lambda: not mine(), 10))


if __name__ == "__main__":
    unittest.main()
