# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The phone's hotspot: found, switched, made (nmcli answers made up here),
kept on the PC (NetworkManager stood in for - never polkit) and the phone
reached through it."""

import os
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from phonebridge import agent, config, connection, hotspot  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
AP, HOME_WIFI = "11111111-2222-3333-4444-555555555555", "99999999-2222-3333-4444-555555555555"


class FakeNM:
    def __init__(self, with_ap=True, active=False):
        self.with_ap, self.active, self.ran = with_ap, active, []

    def __call__(self, *argv, timeout=10):
        self.ran.append(argv)
        if argv[:6] == ("nmcli", "-t", "-f", "UUID,TYPE,NAME", "connection", "show"):
            lines = ["%s:802-11-wireless:Home\\: upstairs" % HOME_WIFI, "x:gsm:Mobile"]
            if self.with_ap:
                lines.append("%s:802-11-wireless:Hotspot" % AP)
            return "\n".join(lines) + "\n"
        if argv[:2] == ("nmcli", "-g"):
            return "ap\nPhoneNet\n" if argv[-1] == AP else "infrastructure\nHome\n"
        if "--active" in argv:
            return (AP + "\n") if self.active else (HOME_WIFI + "\n")
        if argv[1:3] == ("device", "wifi"):
            self.with_ap = True
        return ""


class OnThePhone(unittest.TestCase):
    def test_fields(self):
        self.assertEqual(agent.nmcli_fields("a:b\\:c:d\\\\"), ["a", "b:c", "d\\"])

    def test_state(self):
        with mock.patch.object(agent, "run_checked", FakeNM(active=True)):
            self.assertEqual(agent.hotspot_state(), {"exists": True, "active": True,
                                                     "name": "Hotspot", "ssid": "PhoneNet"})
        with mock.patch.object(agent, "run_checked", FakeNM(with_ap=False)):
            self.assertFalse(agent.hotspot_state()["exists"])

    def test_switch_and_make(self):
        a = mock.Mock()
        nm = FakeNM()
        with mock.patch.object(agent, "run_checked", nm):
            agent.cmd_hotspot_set(a, {"on": True})
            agent.cmd_hotspot_set(a, {"on": False})
        self.assertIn(("nmcli", "connection", "up", AP), nm.ran)
        self.assertIn(("nmcli", "connection", "down", AP), nm.ran)
        nm = FakeNM(with_ap=False)
        with mock.patch.object(agent, "run_checked", nm):
            state = agent.cmd_hotspot_set(a, {"on": True})
        made = next(r for r in nm.ran if r[1:4] == ("device", "wifi", "hotspot"))
        self.assertEqual(made[-2], "password")
        self.assertEqual(state["password"], made[-1])          # told once to the PC
        self.assertGreaterEqual(len(state["password"]), 8)


class OnThePC(unittest.TestCase):
    def setUp(self):
        hotspot._cache.update(at=0.0)
        self.addCleanup(hotspot._cache.update, at=0.0)

    def test_reached_through_the_hotspot(self):
        info = {"id": "t", "host": "192.168.0.25", "user": "furios", "hotspot_ssid": "PhoneNet"}
        with mock.patch.object(hotspot, "pc_wifi_ssid", lambda: "Home"), \
                mock.patch.object(hotspot, "default_gateway", lambda: "192.168.0.1"):
            self.assertEqual(hotspot.reach(info)["host"], "192.168.0.25")
        hotspot._cache.update(at=0.0)
        with mock.patch.object(hotspot, "pc_wifi_ssid", lambda: "PhoneNet"), \
                mock.patch.object(hotspot, "default_gateway", lambda: "10.42.0.1"):
            self.assertEqual(hotspot.reach(info)["host"], "10.42.0.1")
            argv = connection.ssh_argv(info)
        self.assertEqual(argv[-2], "furios@10.42.0.1")
        plain = dict(info)
        del plain["hotspot_ssid"]
        self.assertIs(hotspot.reach(plain), plain)

    def test_gateway_and_ssid(self):
        with mock.patch.object(hotspot, "_run", lambda *a: "default via 10.42.0.1 dev wlp0 \n"):
            self.assertEqual(hotspot.default_gateway(), "10.42.0.1")
        with mock.patch.object(hotspot, "_run", lambda *a: "no:Other\nyes:Phone\\:Net\n"):
            self.assertEqual(hotspot.pc_wifi_ssid(), "Phone:Net")


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class Settings(unittest.TestCase):
    def test_join_from_this_pc(self):
        from phonebridge.app import PhoneBridgeApp
        from phonebridge.quick import QuickSettings
        home = Home()
        self.addCleanup(home.cleanup)
        for p in (mock.patch.dict(os.environ, home.env()),
                  mock.patch.object(config, "CONFIG_DIR", home.config)):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestHotspot")
        app.send_notification = lambda *a: None
        app.toast = lambda m: None
        app.register(None)
        self.addCleanup(app.do_shutdown)
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online, 20))
        quick = QuickSettings(app)
        quick.set_device(dev)
        quick._hotspot_for = dev
        quick._got_hotspot(dev, {"exists": True, "active": True, "name": "Hotspot",
                                 "ssid": "PhoneNet"}, None)
        self.assertTrue(quick.hotspot.get_visible() and quick.hotspot.get_active())
        self.assertEqual(quick.hotspot_join.get_label(), "Set up …")

        dialogs, added = [], []
        with mock.patch.object(Adw.AlertDialog, "present",
                               lambda d, parent=None: dialogs.append(d)), \
                mock.patch.object(hotspot, "pc_add_profile",
                                  lambda ssid, pw, done: added.append((ssid, pw)) or done(None)):
            quick.ask_join()
            d = dialogs[-1]
            self.assertFalse(d.get_response_enabled("save"))      # 8 characters at least
            d.get_extra_child().set_text("secret-password")
            d.emit("response", "save")
        self.assertEqual(added, [("PhoneNet", "secret-password")])
        self.assertEqual(app.cfg["devices"][0]["hotspot_ssid"], "PhoneNet")
        self.assertEqual(config.load()["devices"][0]["hotspot_ssid"], "PhoneNet")


if __name__ == "__main__":
    unittest.main()
