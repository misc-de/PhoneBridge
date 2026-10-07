# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Setting up a phone (the first start, "Add phone"), the PC's SSH key put
on the phone like ssh-copy-id, and what the app needs on the PC - checked
at start. ~/.ssh is never touched: run-tests.sh points the paths elsewhere."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from phonebridge import agent, config, deps, i18n, secrets, sshkeys  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
KEY = "ssh-ed25519 " + "A" * 68 + " user@pc"


class Authorize(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phonebridge-keys-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, ".ssh", "authorized_keys")
        p = mock.patch.object(agent, "AUTHORIZED_KEYS", self.path)
        p.start()
        self.addCleanup(p.stop)

    def test_added_once_private(self):
        self.assertEqual(agent.cmd_authorize(None, {"key": KEY}), "added")
        self.assertEqual(agent.cmd_authorize(None, {"key": KEY + " other comment"}), "present")
        with open(self.path) as f:
            self.assertEqual(f.read(), KEY + "\n")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self.path)).st_mode & 0o777, 0o700)

    def test_a_last_line_without_newline(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("ssh-rsa " + "B" * 60 + " old")
        agent.cmd_authorize(None, {"key": KEY})
        with open(self.path) as f:
            self.assertEqual(f.read().splitlines()[-1], KEY)

    def test_only_keys(self):
        for bad in ("", "hello", KEY + "\ncommand=\"rm -rf\" " + KEY, "ssh-ed25519 short"):
            with self.assertRaises(RuntimeError):
                agent.cmd_authorize(None, {"key": bad})


class Keys(unittest.TestCase):
    def test_make_and_find(self):
        d = tempfile.mkdtemp(prefix="phonebridge-ssh-")
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.object(sshkeys, "SSH_DIR", os.path.join(d, ".ssh")):
            self.assertIsNone(sshkeys.public_key_path())
            if not shutil.which("ssh-keygen"):
                self.skipTest("no ssh-keygen")
            key = sshkeys.make_key()
            self.assertTrue(key.startswith("ssh-ed25519 "))
            self.assertTrue(agent.PUBLIC_KEY.fullmatch(key))
            self.assertEqual(sshkeys.public_key(), key)
            with self.assertRaises(OSError):
                sshkeys.make_key()                       # never over an existing one


class Dependencies(unittest.TestCase):
    def test_checks(self):
        self.assertTrue(deps._ok(("program", "sh")))
        self.assertFalse(deps._ok(("program", "no-such-program-here")))
        self.assertTrue(deps._ok(("module", "json")))
        self.assertTrue(deps._ok(("gi", "Gtk", "4.0")))
        self.assertFalse(deps._ok(("gi", "NoSuchLibrary", "9")))
        self.assertEqual(deps.missing_required(), [])

    def test_install_hint(self):
        missing = [deps.OPTIONAL[0], deps.OPTIONAL[1]]
        with mock.patch.object(deps, "distro", return_value="arch"):
            self.assertEqual(deps.install_hint(missing), "sudo pacman -S libsecret pipewire")
        with mock.patch.object(deps, "distro", return_value="debian"):
            self.assertEqual(deps.install_hint(missing),
                             "sudo apt install gir1.2-secret-1 pipewire-bin")
        with mock.patch.object(deps, "distro", return_value=None):
            self.assertIsNone(deps.install_hint(missing))

    def test_tell_uses_what_there_is(self):
        runs = []
        with mock.patch.object(deps.shutil, "which",
                               side_effect=lambda p: p == "notify-send" and "/x"), \
                mock.patch.object(deps.subprocess, "run", lambda argv, **kw: runs.append(argv)), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch("sys.stderr"):
            deps.tell("hi")
        self.assertEqual(runs[0][0], "notify-send")

    def test_start_without_ssh_says_so(self):
        """bin/phonebridge, with ssh nowhere on the PATH: exits, says why."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, os.path.join(root, "bin", "phonebridge")],
                           env=dict(os.environ, PATH="/nonexistent", LANGUAGE="en",
                                    PHONEBRIDGE_LANGUAGE="en"),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("PhoneBridge cannot start", r.stderr)
        self.assertIn("OpenSSH", r.stderr)


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class Setup(unittest.TestCase):
    def setUp(self):
        from phonebridge.app import PhoneBridgeApp
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        for p in (mock.patch.dict(os.environ, dict(self.home.env(),
                                                   XDG_RUNTIME_DIR=self.home.dir)),
                  mock.patch.object(config, "CONFIG_DIR", self.home.config)):
            p.start()
            self.addCleanup(p.stop)
        secrets._MEMORY.clear()
        config.save(dict(config.DEFAULTS, language="en"))
        self.app = PhoneBridgeApp()
        Setup.count = getattr(Setup, "count", 0) + 1
        self.app.set_application_id("io.github.miscde.PhoneBridge.TestSetup%d" % Setup.count)
        self.app.send_notification = lambda *a: None
        self.app.register(None)
        from phonebridge.window import MainWindow
        self.app.window = MainWindow(self.app)
        self.app.show_window = lambda page=None: self.app.window
        self.addCleanup(self._close)
        from phonebridge.setup import SetupDialog
        self.dialog = SetupDialog(self.app, first=True)
        self.dialog.make_key.set_active(False)           # no key making here

    def _close(self):
        self.dialog._drop()
        self.app.window.destroy()
        self.app.do_shutdown()
        i18n.setup("en")

    def fill(self, host="phone"):
        self.assertFalse(self.dialog.go.get_sensitive())  # an address is needed
        self.dialog.host.set_text(host)
        self.assertTrue(self.dialog.go.get_sensitive())

    def test_key_works(self):
        self.fill()
        self.dialog.connect_now()
        self.assertTrue(run_loop_until(lambda: self.app.cfg["devices"], 15))
        dev = self.app.cfg["devices"][0]
        self.assertEqual((dev["host"], dev["user"]), ("phone", "furios"))
        self.assertEqual(dev["name"], os.uname().nodename)  # the phone's host name
        self.assertEqual(self.dialog.stack.get_visible_child_name(), "done")

    def test_password_then_key(self):
        keys = os.path.join(self.home.dir, "phone-keys")
        with mock.patch.dict(os.environ, {"FAKE_SSH_PASSWORD": "geheim",
                                          "PHONEBRIDGE_AUTHORIZED_KEYS": keys}), \
                mock.patch.object(sshkeys, "public_key", return_value=KEY):
            self.fill()
            self.dialog.connect_now()
            self.assertTrue(run_loop_until(
                lambda: self.dialog.stack.get_visible_child_name() == "login", 10))
            self.dialog.password.set_text("falsch")
            self.dialog.login_now()
            self.assertTrue(run_loop_until(lambda: self.dialog.login_error.get_visible(), 10))
            self.dialog.password.set_text("geheim")
            self.assertTrue(self.dialog.put_key.get_active())
            self.assertFalse(self.dialog.keep_password.get_active())
            self.dialog.login_now()
            self.assertTrue(run_loop_until(lambda: self.app.cfg["devices"], 15))
        with open(keys) as f:
            self.assertEqual(f.read(), KEY + "\n")        # ssh-copy-id done
        self.assertEqual(secrets._MEMORY, {})              # not kept: not wanted
        self.assertIn("takes this PC's SSH key", self.dialog.done_label.get_label())

    def test_unreachable(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_FAIL": "1"}):
            self.fill()
            self.dialog.connect_now()
            self.assertTrue(run_loop_until(lambda: self.dialog.error.get_visible(), 10))
        self.assertIn("No route to host", self.dialog.error.get_label())
        self.assertEqual(self.dialog.stack.get_visible_child_name(), "form")
        self.assertEqual(self.app.cfg["devices"], [])       # added only once it worked


if __name__ == "__main__":
    unittest.main()
