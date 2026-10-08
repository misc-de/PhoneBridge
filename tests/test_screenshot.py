# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""A screenshot of the phone: a stand-in grim gives a made-up screen (or
none - the screen is off), the PC shows it and saves it."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, GdkPixbuf, Gtk  # noqa: E402

from phonebridge import agent, config  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def screen(path, w=72, h=160):
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, w, h)
    pb.fill(0x241f31ff)
    pb.savev(path, "png", [], [])


class OnThePhone(unittest.TestCase):
    def test_screen_on_and_off(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        png = os.path.join(d, "screen.png")
        screen(png)
        with mock.patch.dict(os.environ, {"FAKE_SCREEN": png}):
            data = agent.screenshot(None)
        self.assertTrue(data.startswith(b"\x89PNG"))
        with mock.patch.dict(os.environ, {"FAKE_SCREEN": ""}):
            with self.assertRaisesRegex(RuntimeError, "screen is off or locked"):
                agent.screenshot(None)


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class OnThePC(unittest.TestCase):
    def test_shown_and_saved(self):
        from phonebridge.app import PhoneBridgeApp
        home = Home()
        self.addCleanup(home.cleanup)
        png = os.path.join(home.dir, "screen.png")
        screen(png)
        for p in (mock.patch.dict(os.environ, dict(home.env(), FAKE_SCREEN=png)),
                  mock.patch.object(config, "CONFIG_DIR", home.config)):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestScreenshot")
        app.send_notification = lambda *a: None
        told = []
        app.tell = told.append
        app.register(None)
        self.addCleanup(app.do_shutdown)
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online, 20))
        shown = []
        win = Gtk.Window()
        self.addCleanup(win.destroy)
        app.show_window = lambda page=None: win
        with mock.patch.object(Adw.Dialog, "present", lambda d, parent=None: shown.append(d)):
            app.activate_action("screenshot", None)
            self.assertTrue(run_loop_until(lambda: shown, 10))
        dialog = shown[0]
        self.assertEqual((dialog.texture.get_width(), dialog.texture.get_height()), (72, 160))
        self.assertTrue(dialog.name.startswith("Phone screenshot ") and
                        dialog.name.endswith(".png"))
        target = os.path.join(home.dir, "saved.png")
        with mock.patch.object(dialog, "close", lambda: None):
            dialog.write(target)
        self.assertEqual(GdkPixbuf.Pixbuf.new_from_file(target).get_height(), 160)
        self.assertEqual(told[-1], "Saved: saved.png")


if __name__ == "__main__":
    unittest.main()
