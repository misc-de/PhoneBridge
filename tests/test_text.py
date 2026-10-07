# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Status in words, SMS counting, times."""

import time
import unittest

from phonebridge import text

STATUS = {
    "battery": {"percent": 84, "state": "charging", "time_to_full": 2700,
                "time_to_empty": 0},
    "network": {"status": "registered", "operator": "Netz", "technology": "lte",
                "strength": 60},
    "wifi": {"enabled": True, "ssid": "Zuhause", "signal": 70},
}


class FakeDevice:
    def __init__(self, state="online", status=None, error=None):
        self.state, self.status, self.error, self.name = state, status, error, "Phone"

    @property
    def online(self):
        return self.state == "online"


class Words(unittest.TestCase):
    def test_battery(self):
        self.assertEqual(text.battery(STATUS), "84 % · charging · full in 45 min")
        s = {"battery": {"percent": 30, "state": "discharging", "time_to_empty": 4 * 3600 + 60}}
        self.assertEqual(text.battery(s), "30 % · on battery · 4 h 1 min left")
        self.assertIsNone(text.battery({}))

    def test_network(self):
        self.assertEqual(text.network(STATUS), "Netz · LTE · signal 60 %")
        self.assertEqual(text.network({"network": {"status": "searching"}}), "No network")

    def test_wifi(self):
        self.assertEqual(text.wifi(STATUS), "Zuhause · 70 %")
        self.assertEqual(text.wifi({"wifi": {"enabled": False}}), "Off")

    def test_tooltip(self):
        title, body = text.tooltip(FakeDevice(status=STATUS), 2)
        self.assertEqual(title, "PhoneBridge – Phone")
        self.assertIn("Battery: 84 %", body)
        self.assertIn("2 unread messages", body)
        title, body = text.tooltip(FakeDevice("offline", error="connection lost"), 0)
        self.assertEqual(body, "Not connected – connection lost")
        self.assertEqual(text.tooltip(None, 0)[1], "No phone set up")

    def test_when(self):
        now = time.mktime((2026, 10, 7, 15, 0, 0, 0, 0, -1))
        self.assertEqual(text.when(now - 3600, now), "14:00")
        self.assertEqual(text.when(now - 86400 * 2, now), "2026-10-05")


class SmsParts(unittest.TestCase):
    def test_gsm(self):
        self.assertEqual(text.sms_parts(""), (0, 0))
        self.assertEqual(text.sms_parts("ä" * 160), (160, 1))
        self.assertEqual(text.sms_parts("a" * 161), (161, 2))
        self.assertEqual(text.sms_parts("a" * 307), (307, 3))

    def test_extension_counts_double(self):
        self.assertEqual(text.sms_parts("€" * 80), (160, 1))
        self.assertEqual(text.sms_parts("€" * 81), (162, 2))

    def test_unicode(self):
        self.assertEqual(text.sms_parts("😀" * 70), (70, 1))
        self.assertEqual(text.sms_parts("ł" * 71), (71, 2))


if __name__ == "__main__":
    unittest.main()
