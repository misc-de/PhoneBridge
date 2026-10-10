# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The settings page's rows (settings_spec) and the agent's GSettings
commands, relocatable schemas included. Writes go to GSettings' memory
backend (run-tests.sh), never to a real dconf."""

import os
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from phonebridge import agent  # noqa: E402
from phonebridge.settings_spec import SECTIONS, keys_of  # noqa: E402


HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class Spec(unittest.TestCase):
    def test_well_formed(self):
        ids = [s["id"] for s in SECTIONS]
        self.assertEqual(len(ids), len(set(ids)))
        kinds = {"switch", "choice", "enum", "spin", "scale", "flag", "entry"}
        for section in SECTIONS:
            for _title, _desc, rows in section.get("groups", ()):
                for row in rows:
                    self.assertEqual(len(row), 6, row)
                    self.assertIn(row[0], kinds, row)
                    if row[0] == "switch" and row[4] is not None:
                        self.assertEqual(len(row[4]), 2, row)

    def test_keys_of_with_requirements(self):
        screen = next(s for s in SECTIONS if s["id"] == "screen")
        keys = keys_of(screen)
        self.assertIn(["io.furios.gesture", "glove-mode-supported"], keys)
        self.assertEqual(len(keys), len({" ".join(k) for k in keys}))


@unittest.skipUnless(os.environ.get("GSETTINGS_BACKEND") == "memory",
                     "only with the memory backend")
class AgentCommands(unittest.TestCase):
    def test_get_set_reset(self):
        got = agent.cmd_gs_get(None, {"keys": [["org.gnome.desktop.interface", "color-scheme"],
                                               ["no.such", "key"]]})
        self.assertIsNotNone(got["org.gnome.desktop.interface color-scheme"])
        self.assertIsNone(got["no.such key"])
        out = agent.cmd_gs_set(None, {"schema": "org.gnome.desktop.interface",
                                      "key": "color-scheme", "value": "prefer-dark"})
        self.assertEqual(out["value"], "prefer-dark")
        with self.assertRaises(RuntimeError):
            agent.cmd_gs_set(None, {"schema": "org.gnome.desktop.interface",
                                    "key": "color-scheme", "value": "purple"})
        out = agent.cmd_gs_reset(None, {"schema": "org.gnome.desktop.interface",
                                        "key": "color-scheme"})
        self.assertTrue(out["default"])

    def test_relocatable_needs_a_path(self):
        schema = "org.gnome.desktop.notifications.application"
        if agent._schema_key(schema, "enable")[1] is None:
            self.skipTest("no GNOME notification schemas here")
        path = "/org/gnome/desktop/notifications/application/phonebridge-test/"
        self.assertIsNone(agent._describe(schema, "enable"))
        got = agent.cmd_gs_get(None, {"keys": [[schema, "enable", path]]})
        self.assertTrue(got["%s enable %s" % (schema, path)]["value"])
        out = agent.cmd_gs_set(None, {"schema": schema, "key": "enable", "path": path,
                                      "value": False})
        self.assertFalse(out["value"])

    def test_numbers_are_converted(self):
        out = agent.cmd_gs_set(None, {"schema": "org.gnome.desktop.session",
                                      "key": "idle-delay", "value": 300.0})
        self.assertEqual(out["value"], 300)


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class Rows(unittest.TestCase):
    def row(self, *spec):
        from phonebridge.phone_settings import SettingRow
        page = mock.Mock(updating=False)
        return SettingRow(page, *spec), page

    def test_invert(self):
        row, page = self.row("switch", "org.gnome.desktop.privacy", "disable-camera",
                             "Apps may use the camera", None, {"invert": True})
        page.updating = True
        row.show({"value": True, "range": ["type", []]})
        self.assertFalse(row.widget.get_active())
        page.updating = False
        row.widget.set_active(True)
        page.set_value.assert_called_with(row)
        self.assertFalse(row.value(None))

    def test_flag_adds_and_removes(self):
        row, page = self.row("flag", "sm.puri.phosh.plugins", "lock-screen", "Upcoming",
                             "upcoming-events", {})
        info = {"value": ["ticket-box"], "range": ["type", []]}
        page.updating = True
        row.show(info)
        self.assertFalse(row.widget.get_active())
        row.widget.set_active(True)
        self.assertEqual(row.value(info), ["ticket-box", "upcoming-events"])
        info["value"] = ["ticket-box", "upcoming-events"]
        row.widget.set_active(False)
        self.assertEqual(row.value(info), ["ticket-box"])

    def test_choice_keeps_a_foreign_value(self):
        row, page = self.row("choice", "org.gnome.desktop.session", "idle-delay", "Off after",
                             ((60, "1 min"), (0, "Never")), {})
        page.updating = True
        row.show({"value": 420, "range": ["type", []]})
        self.assertEqual(row.value(None), 420)
        self.assertEqual(row.values, [60, 420, 0])

    def test_enum_from_the_schema(self):
        row, page = self.row("enum", "org.sigxcpu.feedbackd", "profile", "Profile",
                             {"full": "Loud"}, {})
        page.updating = True
        row.show({"value": "quiet", "range": ["enum", ["full", "quiet", "silent"]]})
        self.assertEqual(row.values, ["full", "quiet", "silent"])
        self.assertEqual(row.value(None), "quiet")

    def test_missing_key_or_feature_hides(self):
        row, page = self.row("switch", "io.furios.gesture", "glove-mode-enabled", "Glove",
                             None, {"requires": ("io.furios.gesture", "glove-mode-supported")})
        row.show(None)
        self.assertFalse(row.widget.get_visible())
        row.show({"value": False, "range": ["type", []]}, required=False)
        self.assertFalse(row.widget.get_visible())
        row.show({"value": False, "range": ["type", []]}, required=True)
        self.assertTrue(row.widget.get_visible())


if __name__ == "__main__":
    unittest.main()
