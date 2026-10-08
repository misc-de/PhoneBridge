# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Photo backup: the phone's camera folder (in a home of the test's own)
comes to a folder of the test's own - new files once, deleted ones not
again, only on Wi-Fi when it runs by itself."""

import os
import tempfile
import shutil
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from phonebridge import agent, backup, config  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class Sources(unittest.TestCase):
    def test_camera_folders(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "Pictures", "furios-camera"))
        os.makedirs(os.path.join(d, "DCIM"))
        os.symlink(os.path.join(d, "DCIM"), os.path.join(d, "Videos"))   # the same, twice
        with mock.patch.object(agent, "FILES_HOME", d):
            paths = [s["path"] for s in agent.cmd_backup_sources(None, {})]
        self.assertEqual(paths, [os.path.join(d, "Pictures", "furios-camera"),
                                 os.path.join(d, "DCIM")])

    def test_wifi(self):
        self.assertTrue(backup.on_wifi(mock.Mock(status={"wifi": {"ssid": "Home"}})))
        self.assertFalse(backup.on_wifi(mock.Mock(status={"wifi": {"ssid": ""}})))
        self.assertFalse(backup.on_wifi(mock.Mock(status=None)))


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class Running(unittest.TestCase):
    def test_backup(self):
        from phonebridge.app import PhoneBridgeApp
        home = Home()
        self.addCleanup(home.cleanup)
        camera = os.path.join(home.dir, "phone-home", "Pictures", "furios-camera")
        write(os.path.join(camera, "IMG_1.jpg"), b"1" * 100)
        write(os.path.join(camera, "IMG_2.jpg"), b"2" * 200)
        write(os.path.join(camera, ".pending.jpg"))
        write(os.path.join(camera, "VID_3.mp4.part"))
        target = os.path.join(home.dir, "backup")
        for p in (mock.patch.dict(os.environ, home.env()),
                  mock.patch.object(config, "CONFIG_DIR", home.config)):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestBackup")
        app.send_notification = lambda *a: None
        told = []
        app.tell = told.append
        app.register(None)
        self.addCleanup(app.do_shutdown)
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online, 20))
        b = app.backup

        self.assertFalse(b.run(dev, quiet=True))             # off: the timer does nothing
        b.set(dev, target=target)
        self.assertEqual(b.target(dev), target)
        b.set(dev, on=True)                                   # on: right away
        self.assertTrue(run_loop_until(lambda: dev.id not in b.running and told, 15))
        self.assertEqual(sorted(os.listdir(target)), ["IMG_1.jpg", "IMG_2.jpg"])
        self.assertEqual(told[-1], "2 photos and videos backed up")
        self.assertEqual(b.settings(dev.id)["count"], 2)

        # deleted here: not fetched again; a new one on the phone: fetched
        os.remove(os.path.join(target, "IMG_1.jpg"))
        write(os.path.join(camera, "IMG_4.jpg"), b"4")
        told.clear()
        b.run(dev)
        self.assertTrue(run_loop_until(lambda: dev.id not in b.running and told, 15))
        self.assertEqual(sorted(os.listdir(target)), ["IMG_2.jpg", "IMG_4.jpg"])
        self.assertEqual(told, ["1 photo or video backed up"])

        told.clear()
        b.run(dev)                                            # by hand: says so
        self.assertTrue(run_loop_until(lambda: told, 15))
        self.assertEqual(told, ["Nothing new to back up"])

        # by itself only on Wi-Fi
        with mock.patch.object(backup, "on_wifi", lambda d: False):
            self.assertFalse(b.run(dev, quiet=True))


if __name__ == "__main__":
    unittest.main()
