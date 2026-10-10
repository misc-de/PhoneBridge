# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""A phone that does not take the key: the password, asked for once, kept in
the keyring (here one in memory - run-tests.sh), and used by every SSH
connection - the agent's and the call sound's. The stand-in ssh refuses the
key and asks SSH_ASKPASS for the password, as real ssh does."""

import os
import stat
import time
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from phonebridge import config, connection, i18n, secrets  # noqa: E402
from phonebridge.connection import Device  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
INFO = {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}


@unittest.skipUnless(os.environ.get("PHONEBRIDGE_KEYRING") == "memory", "keyring in memory only")
class Connection(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        p = mock.patch.dict(os.environ, dict(self.home.env(), FAKE_SSH_PASSWORD="geheim",
                                             XDG_RUNTIME_DIR=self.home.dir))
        p.start()
        self.addCleanup(p.stop)
        self.dev = Device(INFO)
        self.addCleanup(self.dev.stop)
        self.asked = []
        self.dev.connect("auth-needed", lambda d, wrong: self.asked.append(wrong))

    def test_key_refused_then_password(self):
        self.dev.start()
        self.assertTrue(run_loop_until(lambda: self.asked, 10))
        self.assertEqual(self.asked, [False])
        self.assertTrue(self.dev.needs_password)
        self.assertEqual(self.dev.state, "offline")
        self.assertIsNone(self.dev._retry_source or None)       # no useless retries

        self.dev.set_password("falsch")
        self.assertTrue(run_loop_until(lambda: len(self.asked) == 2, 10))
        self.assertEqual(self.asked[1], True)                   # wrong this time
        self.assertIsNone(self.dev.password)

        self.dev.set_password("geheim")
        self.assertTrue(run_loop_until(lambda: self.dev.online, 15))
        self.assertFalse(self.dev.needs_password)
        helper = os.path.join(self.home.dir, "phonebridge-askpass")
        self.assertEqual(stat.S_IMODE(os.stat(helper).st_mode), 0o700)

    def test_ssh_options(self):
        key = connection.ssh_argv(INFO)
        pw = connection.ssh_argv(INFO, password=True)
        self.assertIn("BatchMode=yes", key)
        self.assertIn("BatchMode=no", pw)
        self.assertIn("NumberOfPasswordPrompts=1", pw)
        for argv in (key, pw):
            self.assertIn("StrictHostKeyChecking=accept-new", argv)
        env = secrets.ssh_env("x")
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
        self.assertEqual(env["PHONEBRIDGE_SSH_PASSWORD"], "x")

    def test_askpass_helper_never_one_others_may_change(self):
        # a helper anyone may write to is replaced, not handed the password
        helper = os.path.join(self.home.dir, "phonebridge-askpass")
        with open(helper, "w") as f:
            f.write(secrets.ASKPASS)
        os.chmod(helper, 0o777)
        self.assertEqual(secrets.askpass_helper(), helper)
        self.assertEqual(stat.S_IMODE(os.stat(helper).st_mode), 0o700)
        # a runtime directory others may write to is not used at all
        os.chmod(self.home.dir, 0o777)
        self.addCleanup(os.chmod, self.home.dir, 0o700)
        path = secrets.askpass_helper()
        self.assertNotEqual(os.path.dirname(path), self.home.dir)
        st = os.stat(os.path.dirname(path))
        self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o700)
        # nor is a shared temporary directory when there is none
        with mock.patch.dict(os.environ):
            del os.environ["XDG_RUNTIME_DIR"]
            self.assertEqual(secrets.askpass_helper(), path)

    def test_call_sound_logs_in_too(self):
        from phonebridge import callaudio
        audio = callaudio.CallAudio(INFO, echo_cancel=False, test=True, password="geheim")
        stopped = []
        audio.connect("stopped", lambda a, why: stopped.append(why))
        with mock.patch.dict(os.environ, {"FAKE_AUDIO_DIR": self.home.dir}):
            self.assertTrue(audio.start())
            self.assertTrue(run_loop_until(
                lambda: os.path.exists(os.path.join(self.home.dir, "play-default")), 10))
        audio.stop()
        self.assertEqual(stopped, [None])


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check()
                     and os.environ.get("PHONEBRIDGE_KEYRING") == "memory", "no display")
class App(unittest.TestCase):
    def setUp(self):
        from phonebridge.app import PhoneBridgeApp
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.answers = []
        self.notified = []
        for p in (mock.patch.dict(os.environ, dict(self.home.env(), FAKE_SSH_PASSWORD="geheim",
                                                   XDG_RUNTIME_DIR=self.home.dir)),
                  mock.patch.object(config, "CONFIG_DIR", self.home.config),
                  mock.patch.object(Adw.AlertDialog, "present",
                                    lambda dialog, parent=None: self._answer(dialog, parent))):
            p.start()
            self.addCleanup(p.stop)
        secrets._MEMORY.clear()
        config.save(dict(config.DEFAULTS, language="en", devices=[INFO]))
        self.app = PhoneBridgeApp()
        # one id per test: an app stays on the test bus once registered
        App.count = getattr(App, "count", 0) + 1
        self.app.set_application_id("io.github.miscde.PhoneBridge.TestLogin%d" % App.count)
        self.app.send_notification = lambda nid, n: self.notified.append(nid)
        self.app.withdraw_notification = lambda nid: None
        self.app.register(None)
        from phonebridge.window import MainWindow
        self.app.window = MainWindow(self.app)
        self.app.show_window = lambda page=None: self.app.window
        self.addCleanup(self._close)

    def _close(self):
        self.app.window.destroy()
        self.app.do_shutdown()
        i18n.setup("en")

    def _answer(self, dialog, parent=None):
        fill = self.answers.pop(0) if self.answers else None
        response = fill(dialog) if fill else "cancel"
        GLib.idle_add(lambda: dialog.emit("response", response) and False)

    @staticmethod
    def rows(dialog):
        box = dialog.get_extra_child()
        out, child = [], box.get_first_child()
        while child is not None:
            out.append(child)
            child = child.get_next_sibling()
        return out

    def test_password_asked_kept_and_used(self):
        dev = self.app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.needs_password, 10))
        self.assertIn("login-test", self.notified)                # window not on screen
        self.assertIn("login", [i.get("id") for i in self.app.menu_items()])
        self.assertTrue(self.app.window.banner.get_revealed())

        def wrong(d):
            user, password, keep = self.rows(d)
            password.set_text("falsch")
            return "login"

        def right(d):
            user, password, keep = self.rows(d)
            self.assertIn("not accepted", d.get_body())          # it says so
            password.set_text("geheim")
            self.assertTrue(keep.get_active())
            return "login"

        self.answers += [wrong, right]
        self.app.ask_password(dev)
        self.assertTrue(run_loop_until(lambda: dev.online, 20))
        self.assertTrue(run_loop_until(lambda: secrets._MEMORY, 5))
        self.assertEqual(list(secrets._MEMORY.values()), ["geheim"])  # kept once it worked

        # the next start finds it in the keyring and logs in at once
        self.app.set_devices([])
        self.assertEqual(secrets._MEMORY, {})                     # a phone gone: its password too
        secrets.store(INFO, "geheim")
        self.app.set_devices([INFO])
        dev = self.app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online, 15))
        self.assertFalse(dev.needs_password)

        self.app.forget_password(dev)
        self.assertTrue(run_loop_until(lambda: dev.needs_password, 10))
        self.assertEqual(secrets._MEMORY, {})

    def test_not_kept_when_unwanted(self):
        dev = self.app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.needs_password, 10))

        def once(d):
            user, password, keep = self.rows(d)
            password.set_text("geheim")
            keep.set_active(False)
            return "login"

        self.answers.append(once)
        self.app.ask_password(dev)
        self.assertTrue(run_loop_until(lambda: dev.online, 15))
        time.sleep(0.2)
        run_loop_until(lambda: False, 0.5)
        self.assertEqual(secrets._MEMORY, {})


if __name__ == "__main__":
    unittest.main()
