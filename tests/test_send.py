# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Sending to the phone: web links into its browser (a stand-in xdg-open,
logged - nothing opens on the desktop), files into its Downloads. The
"phone" is the agent running here with a home of its own."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from phonebridge import agent, config, send  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class Links(unittest.TestCase):
    def test_web_links(self):
        for good in ("https://example.org", " http://example.org/a?b=c "):
            self.assertTrue(send.is_web_link(good), good)
        for bad in ("example.org", "file:///etc/passwd", "javascript:alert(1)", "https://",
                    "/home/user/a.txt", "", None):
            self.assertFalse(send.is_web_link(bad), bad)

    def test_agent_opens_only_web_links(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        log = os.path.join(d, "log")
        with mock.patch.dict(os.environ, {"FAKE_LOG": log}):
            for bad in ("file:///etc/passwd", "javascript:x", "https://a\nb"):
                with self.assertRaisesRegex(RuntimeError, "only web links"):
                    agent.cmd_open_uri(None, {"uri": bad})
            agent.cmd_open_uri(None, {"uri": "https://example.org/x"})
            self.assertTrue(run_loop_until(lambda: os.path.exists(log) and "xdg-open" in
                                           open(log).read(), 5))
        lines = open(log).read().splitlines()
        self.assertIn("--user", lines[0])                  # in the session, not the login
        self.assertEqual(lines[-1], "xdg-open https://example.org/x")

    def test_session_env(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "C.UTF-8", "XDG_RUNTIME_DIR": "/nonexistent"}):
            os.environ.pop("WAYLAND_DISPLAY", None)
            env = agent.session_env()
        self.assertNotIn("LC_ALL", env)                    # the user's language, not C

    def test_free_name(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for n in ("a.txt", "a (2).txt"):
            open(os.path.join(d, n), "w").close()
        free = lambda n: agent.cmd_files_free_name(None, {"path": d, "name": n})["path"]  # noqa
        self.assertEqual(free("b.txt"), os.path.join(d, "b.txt"))
        self.assertEqual(free("a.txt"), os.path.join(d, "a (3).txt"))


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class Sending(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from phonebridge.app import PhoneBridgeApp
        cls.home = Home()
        cls.addClassCleanup(cls.home.cleanup)
        cls.phone = os.path.join(cls.home.dir, "phone-home")
        os.makedirs(os.path.join(cls.phone, "Downloads"))
        cls.log = os.path.join(cls.home.dir, "fake.log")
        for p in (mock.patch.dict(os.environ, dict(cls.home.env(), FAKE_LOG=cls.log)),
                  mock.patch.object(config, "CONFIG_DIR", cls.home.config)):
            p.start()
            cls.addClassCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        cls.app = app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestSend")
        app.send_notification = lambda *a: None
        app.register(None)
        cls.addClassCleanup(app.do_shutdown)
        cls.dev = app.devices["test"]
        assert run_loop_until(lambda: cls.dev.online, 20)
        cls.told = []
        app.tell = cls.told.append

    def test_files_and_links(self):
        local = os.path.join(self.home.dir, "letter.txt")
        with open(local, "w") as f:
            f.write("Dear Anna")
        os.makedirs(os.path.join(self.home.dir, "album"))
        open(os.path.join(self.home.dir, "album", "1.jpg"), "w").close()
        send.send(self.app, [local, os.path.join(self.home.dir, "album"),
                             "https://example.org/page", "/nonexistent/x"])
        downloads = os.path.join(self.phone, "Downloads")
        self.assertTrue(run_loop_until(
            lambda: sorted(os.listdir(downloads)) == ["album", "letter.txt"], 10))
        self.assertTrue(run_loop_until(lambda: len(self.told) == 4, 10), self.told)
        self.assertIn("Not found: /nonexistent/x", self.told)
        self.assertIn("Opened on Testphone: https://example.org/page", self.told)
        self.assertIn("Sent to Testphone: letter.txt", self.told)
        self.assertIn("xdg-open https://example.org/page", open(self.log).read())

        send.send(self.app, [local])                         # taken: a name of its own
        self.assertTrue(run_loop_until(
            lambda: "letter (2).txt" in os.listdir(downloads), 10))
        with open(os.path.join(downloads, "letter.txt")) as f:
            self.assertEqual(f.read(), "Dear Anna")

    def test_offline(self):
        self.told.clear()
        with mock.patch.object(type(self.dev), "online", property(lambda d: False)):
            send.send(self.app, ["https://example.org"])
        self.assertEqual(self.told, ["Testphone is not connected"])


if __name__ == "__main__":
    unittest.main()
