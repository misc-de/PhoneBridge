# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Updates: what is installed, what GitHub says (answers made up here -
no network), installing a fetched commit (a tarball from a file://
address), install.sh recording the version, and the hint and question in
the window."""

import io
import os
import subprocess
import tarfile
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from phonebridge import config, update  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD, NEW = "a" * 40, "b" * 40


def commit(message):
    return {"commit": {"message": message}}


class Installed(unittest.TestCase):
    def test_version_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "VERSION")
            with mock.patch.object(update, "VERSION_FILE", path):
                self.assertIsNone(update.installed())       # the source tree
                with open(path, "w") as f:
                    f.write(OLD + "\n")
                self.assertEqual(update.installed(), OLD)
                with open(path, "w") as f:
                    f.write("unknown\n")
                self.assertEqual(update.installed(), "unknown")

    def test_launcher_next_to_lib(self):
        with mock.patch.object(update, "LIB", "/x/.local/lib/phonebridge"):
            self.assertEqual(update.launcher(), "/x/.local/bin/phonebridge")


class Check(unittest.TestCase):
    def answers(self, compare):
        def get(url):
            if url.endswith("/commits/main"):
                return {"sha": NEW}
            self.assertIn("/compare/%s...%s" % (OLD, NEW), url)
            if isinstance(compare, Exception):
                raise compare
            return compare
        return mock.patch.object(update, "_get_json", get)

    def test_up_to_date(self):
        with mock.patch.object(update, "_get_json", lambda url: {"sha": OLD}):
            self.assertIsNone(update.check(OLD))

    def test_newer_with_changes_newest_first(self):
        with self.answers({"status": "ahead", "ahead_by": 2,
                           "commits": [commit("First\n\nmore"), commit("Second")]}):
            info = update.check(OLD)
        self.assertEqual(info, {"sha": NEW, "count": 2, "changes": ["Second", "First"]})

    def test_installed_is_newer(self):
        with self.answers({"status": "behind", "ahead_by": 0, "commits": []}):
            self.assertIsNone(update.check(OLD))

    def test_commit_github_does_not_know(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with self.answers(err):
            self.assertEqual(update.check(OLD), {"sha": NEW, "count": None, "changes": []})
        with mock.patch.object(update, "_get_json", lambda url: {"sha": NEW}):
            self.assertEqual(update.check("unknown")["count"], None)

    def test_github_unreachable_or_strange(self):
        def down(url):
            raise urllib.error.URLError("offline")
        with mock.patch.object(update, "_get_json", down):
            self.assertRaises(OSError, update.check, OLD)
        with mock.patch.object(update, "_get_json", lambda url: {"message": "rate limit"}):
            self.assertRaises(ValueError, update.check, OLD)
        with self.answers(urllib.error.HTTPError("u", 500, "x", {}, None)):
            self.assertRaises(OSError, update.check, OLD)


def tarball(path, script):
    """A tarball like GitHub's: one top folder with install.sh."""
    with tarfile.open(path, "w:gz") as tar:
        data = script.encode()
        info = tarfile.TarInfo("PhoneBridge-" + NEW + "/install.sh")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))


class Install(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phonebridge-test-update-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.dir])
        self.marker = os.path.join(self.dir, "ran")
        p = mock.patch.object(update, "TARBALL", "file://" + self.dir + "/{sha}.tar.gz")
        p.start()
        self.addCleanup(p.stop)

    def test_runs_the_fetched_install_sh(self):
        tarball(os.path.join(self.dir, NEW + ".tar.gz"),
                '#!/bin/bash\necho "$PHONEBRIDGE_VERSION $PHONEBRIDGE_NO_RESTART '
                '${NO_AUTOSTART:-auto} $(basename "$PWD")" > %s\n' % self.marker)
        with mock.patch.dict(os.environ, {"NO_AUTOSTART": "1"}):
            self.assertIsNone(update.install(NEW, autostart=True))
        with open(self.marker) as f:
            self.assertEqual(f.read().split(), [NEW, "1", "auto", "PhoneBridge-" + NEW])
        self.assertIsNone(update.install(NEW, autostart=False))
        with open(self.marker) as f:
            self.assertEqual(f.read().split()[2], "1")
        leftovers = [n for n in os.listdir(tempfile.gettempdir())
                     if n.startswith("phonebridge-update-")]
        self.assertEqual(leftovers, [])

    def test_failures_are_told(self):
        self.assertEqual(update.install("not a sha", True), "no valid version")
        self.assertIsInstance(update.install(NEW, True), str)      # no tarball there
        tarball(os.path.join(self.dir, NEW + ".tar.gz"),
                '#!/bin/bash\necho "missing: something"\nexit 1\n')
        self.assertEqual(update.install(NEW, True), "missing: something")

    def test_restart_waits_for_the_old_process(self):
        launcher = os.path.join(self.dir, "phonebridge")
        with open(launcher, "w") as f:
            f.write('#!/bin/sh\necho "$@" > %s\n' % self.marker)
        os.chmod(launcher, 0o755)
        old = subprocess.Popen(["sleep", "0.5"])
        with mock.patch.object(update, "launcher", lambda: launcher):
            update.restart_after(old.pid, ["--messages"])
        time.sleep(0.2)
        self.assertFalse(os.path.exists(self.marker))       # the old one still runs
        old.wait()
        for _ in range(50):
            if os.path.exists(self.marker):
                break
            time.sleep(0.1)
        with open(self.marker) as f:
            self.assertEqual(f.read().strip(), "--messages")


class InstallScript(unittest.TestCase):
    """install.sh into a home of its own: the version is recorded, and the
    app's updater can keep it from restarting anything."""

    def test_records_the_version(self):
        home = tempfile.mkdtemp(prefix="phonebridge-test-home-")
        self.addCleanup(subprocess.run, ["rm", "-rf", home])
        env = dict(os.environ, HOME=home, PHONEBRIDGE_VERSION=NEW,
                   PHONEBRIDGE_NO_RESTART="1", NO_AUTOSTART="1",
                   XDG_DATA_HOME=os.path.join(home, ".local", "share"))
        p = subprocess.run(["bash", os.path.join(ROOT, "install.sh")], env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lib = os.path.join(home, ".local", "lib", "phonebridge")
        with open(os.path.join(lib, "VERSION")) as f:
            self.assertEqual(f.read().strip(), NEW)
        self.assertTrue(os.path.isfile(os.path.join(lib, "phonebridge", "update.py")))
        self.assertTrue(os.access(os.path.join(home, ".local", "bin", "phonebridge"), os.X_OK))
        # tel: links go to PhoneBridge (no other program had them here)
        q = subprocess.run(["xdg-mime", "query", "default", "x-scheme-handler/tel"], env=env,
                           capture_output=True, text=True)
        if q.returncode == 0:
            self.assertEqual(q.stdout.strip(), "io.github.miscde.PhoneBridge.desktop")


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class Window(unittest.TestCase):
    def setUp(self):
        from phonebridge.app import PhoneBridgeApp
        home = Home()
        self.addCleanup(home.cleanup)
        for p in (mock.patch.dict(os.environ, home.env()),
                  mock.patch.object(config, "CONFIG_DIR", home.config),
                  mock.patch.object(config, "AUTOSTART",
                                    os.path.join(home.dir, "autostart.desktop")),
                  mock.patch.object(update, "installed", lambda: OLD)):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en"))
        self.app = app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestUpdate."
                               + self._testMethodName.replace("_", ""))
        app.send_notification = lambda *a: None
        app.register(None)
        self.win = app.show_window()
        self.win.set_visible(False)
        self.addCleanup(lambda: (self.win.destroy(), app.do_shutdown()))
        self.toasts = []
        app.toast = self.toasts.append

    def look(self, info):
        with mock.patch.object(update, "check", lambda current: info):
            self.app.look_for_update()
            run_loop_until(lambda: self.app.update_available == info, 5)

    def test_hint_question_and_update(self):
        button = self.win.update_button
        self.assertFalse(button.get_visible())
        self.look(None)
        self.assertFalse(button.get_visible())

        info = {"sha": NEW, "count": 10, "changes": ["Change %d" % i for i in range(10)]}
        self.look(info)
        self.assertTrue(button.get_visible())
        self.assertEqual(button.get_label(), "Update available")

        asked = []
        from gi.repository import Adw
        with mock.patch.object(Adw.AlertDialog, "present",
                               lambda d, parent=None: asked.append(d)):
            self.app.ask_update()
        body = asked[0].get_body()
        self.assertIn("10 changes:", body)
        self.assertIn("• Change 0", body)
        self.assertNotIn("Change 8", body)          # only the first ones, then "…"
        self.assertIn("…", body)

        # failed: told, and the hint is back
        with mock.patch.object(update, "install", lambda sha, autostart: "no network"):
            self.app.start_update()
            self.assertFalse(button.get_sensitive())
            self.assertEqual(button.get_label(), "Updating …")
            run_loop_until(lambda: not self.app.updating, 5)
        self.assertEqual(self.toasts, ["The update failed: no network"])
        self.assertTrue(button.get_sensitive())

        # done: starts anew - on the page it showed, or in the background
        self.assertTrue(self.win.is_visible())          # the question brought it up
        restarted, installed = [], []
        with mock.patch.object(update, "install",
                               lambda sha, autostart: installed.append((sha, autostart))), \
                mock.patch.object(update, "restart_after",
                                  lambda pid, args: restarted.append((pid, args))), \
                mock.patch.object(self.app, "quit", lambda: None):
            self.app.start_update()
            run_loop_until(lambda: restarted, 5)
        self.assertEqual(installed, [(NEW, False)])         # no autostart entry here
        self.assertEqual(restarted, [(os.getpid(), ["--overview"])])
        self.win.set_visible(False)
        with mock.patch.object(update, "install", lambda sha, autostart: None), \
                mock.patch.object(update, "restart_after",
                                  lambda pid, args: restarted.append((pid, args))), \
                mock.patch.object(self.app, "quit", lambda: None):
            self.app.start_update()
            run_loop_until(lambda: len(restarted) == 2, 5)
        self.assertEqual(restarted[1][1], ["--background"])

    def test_not_during_a_call_and_switched_off(self):
        self.look({"sha": NEW, "count": None, "changes": []})
        self.app.devices["x"] = object()
        self.app.calls["x"] = [{"state": "active"}]
        self.addCleanup(lambda: (self.app.devices.pop("x"), self.app.calls.pop("x")))
        self.app.ask_update()
        self.assertEqual(self.toasts, ["Update after the call."])

        self.app.activate_action("updates", None)            # off: the hint goes
        self.assertFalse(self.app.cfg["updates"])
        self.assertIsNone(self.app.update_available)
        self.assertFalse(self.win.update_button.get_visible())
        with mock.patch.object(update, "check", lambda current: self.fail("asked")):
            self.app.look_for_update()


if __name__ == "__main__":
    unittest.main()
