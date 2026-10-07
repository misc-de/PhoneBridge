# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Settings of the PC side and the autostart entry."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from phonebridge import config


class Config(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phonebridge-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, value in (("CONFIG_DIR", os.path.join(self.dir, "cfg")),
                            ("AUTOSTART", os.path.join(self.dir, "autostart", "pb.desktop"))):
            p = mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_defaults(self):
        cfg = config.load()
        self.assertEqual(cfg["devices"], [])
        self.assertIsNone(cfg["active"])
        self.assertTrue(cfg["notify"])

    def test_round_trip_and_active_fallback(self):
        cfg = config.load()
        cfg["devices"] = [{"id": "a", "name": "A", "host": "10.0.0.1", "user": "u"},
                          {"id": "broken"}]
        cfg["active"] = "gone"
        config.save(cfg)
        cfg = config.load()
        self.assertEqual([d["id"] for d in cfg["devices"]], ["a"])
        self.assertEqual(cfg["active"], "a")

    def test_damaged_file(self):
        os.makedirs(config.CONFIG_DIR)
        with open(config.path(), "w") as f:
            f.write("{nope")
        self.assertEqual(config.load()["devices"], [])
        with open(config.path(), "w") as f:
            json.dump(["list"], f)
        self.assertEqual(config.load()["language"], "system")

    def test_device_ids(self):
        self.assertEqual(config.new_device_id("FLX1s Büro", set()), "flx1s-b-ro")
        self.assertEqual(config.new_device_id("Phone", {"phone"}), "phone-2")
        self.assertEqual(config.new_device_id("", set()), "phone")

    def test_seen(self):
        cfg = config.load()
        seen = config.seen_for(cfg, "a")
        self.assertEqual(seen, {"baseline": None, "threads": {}})
        self.assertIs(config.seen_for(cfg, "a"), seen)

    def test_autostart(self):
        self.assertFalse(config.autostart_enabled())
        config.set_autostart(True, "/opt/pb/phonebridge")
        with open(config.AUTOSTART) as f:
            self.assertIn("Exec=/opt/pb/phonebridge --background", f.read())
        config.set_autostart(False)
        config.set_autostart(False)
        self.assertFalse(config.autostart_enabled())


if __name__ == "__main__":
    unittest.main()
