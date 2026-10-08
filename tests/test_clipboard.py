# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The clipboard shared with the phone. The phone's clipboard is a file
(stand-ins for wl-copy and wl-paste), the PC's a dictionary: your own
clipboard is never touched."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from phonebridge import agent, config  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


class OnThePhone(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.clip = os.path.join(d, "clipboard")
        p = mock.patch.dict(os.environ, {"FAKE_CLIPBOARD": self.clip})
        p.start()
        self.addCleanup(p.stop)

    def test_get_and_set(self):
        self.assertEqual(agent.clipboard_get(), "")
        agent.clipboard_set("Grüße\nzweite Zeile")
        self.assertTrue(run_loop_until(lambda: read(self.clip) == "Grüße\nzweite Zeile", 5))
        self.assertEqual(agent.clipboard_get(), "Grüße\nzweite Zeile")
        too_big = mock.Mock(clip_last=None)
        with self.assertRaisesRegex(RuntimeError, "too large"):
            agent.cmd_clipboard_set(too_big, {"text": "x" * (agent.CLIPBOARD_MAX + 1)})

    def test_watch(self):
        with open(self.clip, "w") as f:
            f.write("there before")
        events = []
        a = mock.Mock(clip_last=None)
        watch = agent.ClipboardWatch(a)
        with mock.patch.object(agent, "send", events.append), \
                mock.patch.object(agent.ClipboardWatch, "QUIET", 0.6):
            watch.start()
            self.addCleanup(watch.stop)
            run_loop_until(lambda: False, 0.9)
            self.assertEqual(events, [])                  # what was there: not passed on
            with open(self.clip, "w") as f:
                f.write("copied on the phone")
            self.assertTrue(run_loop_until(lambda: events, 5))
            self.assertEqual(events, [{"event": "clipboard", "text": "copied on the phone"}])
            a.clip_last = "from the PC"                   # what the PC itself put there
            with open(self.clip, "w") as f:
                f.write("from the PC")
            run_loop_until(lambda: False, 0.8)
            self.assertEqual(len(events), 1)
        watch.stop()


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class OnThePC(unittest.TestCase):
    def test_both_ways(self):
        from phonebridge.app import PhoneBridgeApp
        home = Home()
        self.addCleanup(home.cleanup)
        clip = os.path.join(home.dir, "phone-clipboard")
        for p in (mock.patch.dict(os.environ, dict(home.env(), FAKE_CLIPBOARD=clip)),
                  mock.patch.object(config, "CONFIG_DIR", home.config)):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestClipboard")
        app.send_notification = lambda *a: None
        pc = {"text": None}
        app.set_pc_clipboard = lambda value: pc.update(text=value) or setattr(
            app, "_clip_last", value)
        app.read_pc_clipboard = lambda then: then(pc["text"])
        app._watch_pc_clipboard = lambda on: None          # never your clipboard
        told = []
        app.tell = told.append
        app.register(None)
        self.addCleanup(app.do_shutdown)
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online, 20))

        # by hand
        pc["text"] = "from the PC"
        app.clipboard_to_phone()
        self.assertTrue(run_loop_until(lambda: read(clip) == "from the PC", 10))
        with open(clip, "w") as f:
            f.write("from the phone")
        app.clipboard_from_phone()
        self.assertTrue(run_loop_until(lambda: pc["text"] == "from the phone", 10))
        self.assertIn("The phone's clipboard is on this PC now", told)

        # shared by itself
        app.activate_action("clipboard-sync", None)
        self.assertTrue(app.cfg["clipboard_sync"])
        run_loop_until(lambda: False, 1.5)                 # the watcher's quiet start
        with open(clip, "w") as f:
            f.write("copied on the phone")
        self.assertTrue(run_loop_until(lambda: pc["text"] == "copied on the phone", 10))
        app.pc_clipboard_text("copied on the PC")
        self.assertTrue(run_loop_until(lambda: read(clip) == "copied on the PC", 10))
        app.activate_action("clipboard-sync", None)        # off again
        self.assertFalse(app.cfg["clipboard_sync"])
        app.pc_clipboard_text("not shared")
        run_loop_until(lambda: False, 0.5)
        self.assertEqual(read(clip), "copied on the PC")


if __name__ == "__main__":
    unittest.main()
