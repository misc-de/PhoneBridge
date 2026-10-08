# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Other desktops than Xfce: which one it is, how its notifications take a
click, light or dark, whether a panel shows the icon (a stand-in watcher
on the private bus), install hints for more distributions, and "Send to"
in the file managers that are there."""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from gi.repository import Gio, GLib

from phonebridge import deps, desktop
from phonebridge.tray import Tray

from .support import run_loop_until

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Which(unittest.TestCase):
    def test_current(self):
        for env, key in (({"XDG_CURRENT_DESKTOP": "XFCE"}, "xfce"),
                         ({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, "gnome"),
                         ({"XDG_CURRENT_DESKTOP": "KDE"}, "kde"),
                         ({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, "cinnamon"),
                         ({"XDG_CURRENT_DESKTOP": "Budgie:GNOME"}, "budgie"),
                         ({"XDG_CURRENT_DESKTOP": "", "DESKTOP_SESSION": "plasmawayland"}, "kde"),
                         ({"XDG_CURRENT_DESKTOP": "sway"}, "sway"),
                         ({}, "")):
            self.assertEqual(desktop.current(env), key, env)
        self.assertEqual(desktop.name("kde"), "KDE Plasma")
        self.assertEqual(desktop.name(""), "?")

    def test_click_on_notifications(self):
        self.assertTrue(desktop.click_on_notification("gnome", ""))
        self.assertTrue(desktop.click_on_notification("kde", "plasma"))
        self.assertTrue(desktop.click_on_notification("sway", "mako"))
        self.assertFalse(desktop.click_on_notification("xfce", "xfce notify daemon"))
        self.assertFalse(desktop.click_on_notification("mate", "notification daemon"))
        self.assertFalse(desktop.click_on_notification("lxde", ""))      # nobody answers

    def test_dark(self):
        # where color-scheme is spoken, it decides
        self.assertIsNone(desktop.wants_dark("gnome", "default", ("Adwaita-dark", False)))
        # set on purpose (as Manjaro does on Xfce): it decides as well
        self.assertIsNone(desktop.wants_dark("xfce", "prefer-dark", ("Matcha-sea", False)))
        # else the theme
        self.assertTrue(desktop.wants_dark("xfce", "default", ("Matcha-dark-sea", False)))
        self.assertFalse(desktop.wants_dark("mate", "default", ("Ambiant-MATE", False)))
        self.assertTrue(desktop.wants_dark("lxqt", "default", ("Breeze", True)))

    def test_theme_from_settings_ini(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "gtk-3.0"))
        with open(os.path.join(d, "gtk-3.0", "settings.ini"), "w") as f:
            f.write("[Settings]\ngtk-theme-name=Nordic-darker\n"
                    "gtk-application-prefer-dark-theme=1\n")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            self.assertEqual(desktop.theme("lxde"), ("Nordic-darker", True))

    def test_tray_hints(self):
        self.assertIn("AppIndicator", desktop.tray_hint("gnome"))
        self.assertIn("Status Tray", desktop.tray_hint("xfce"))
        self.assertEqual(desktop.tray_hint("hyprland"), desktop.GENERIC_TRAY_HINT)


class Distributions(unittest.TestCase):
    def release(self, text):
        f = tempfile.NamedTemporaryFile("w", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_which(self):
        for text, key in (("ID=fedora\n", "fedora"),
                          ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "suse"),
                          ("ID=manjaro\nID_LIKE=arch\n", "arch"),
                          ("ID=linuxmint\nID_LIKE=\"ubuntu debian\"\n", "debian"),
                          ("ID=nixos\n", None)):
            self.assertEqual(deps.distro(self.release(text)), key, text)

    def test_hints(self):
        missing = [e for e in deps.REQUIRED if e[0] in ("libadwaita", "OpenSSH (ssh)")]
        for key, command in (("fedora", "sudo dnf install libadwaita openssh-clients"),
                             ("suse", "sudo zypper install typelib-1_0-Adw-1 openssh-clients"),
                             ("arch", "sudo pacman -S libadwaita openssh")):
            with mock.patch.object(deps, "distro", lambda k=key: k):
                self.assertEqual(deps.install_hint(missing), command)


WATCHER_XML = """
<node><interface name="org.kde.StatusNotifierWatcher">
 <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
 <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
 <signal name="StatusNotifierHostRegistered"/>
 <signal name="StatusNotifierHostUnregistered"/>
</interface></node>"""


@unittest.skipUnless(os.environ.get("DBUS_SESSION_BUS_ADDRESS"), "no session bus")
class PanelShowsIt(unittest.TestCase):
    def test_hosted_or_not(self):
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        tray = Tray(lambda: None, lambda: None, lambda i: None)
        self.addCleanup(tray.close)
        changes = []
        tray.on_hosted = changes.append
        run_loop_until(lambda: False, 0.3)
        self.assertFalse(tray.hosted)                       # no watcher: no panel

        state = {"host": True}
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]
        reg = conn.register_object(
            "/StatusNotifierWatcher", info,
            lambda c, s, p, i, m, params, inv: inv.return_value(None),
            lambda c, s, p, i, prop: GLib.Variant("b", state["host"]), None)
        own = Gio.bus_own_name_on_connection(conn, "org.kde.StatusNotifierWatcher",
                                            Gio.BusNameOwnerFlags.NONE, None, None)
        self.assertTrue(run_loop_until(lambda: tray.hosted, 5))
        state["host"] = False                               # the panel's tray goes
        conn.emit_signal(None, "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                         "StatusNotifierHostUnregistered", None)
        self.assertTrue(run_loop_until(lambda: not tray.hosted, 5))
        state["host"] = True
        conn.emit_signal(None, "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                         "StatusNotifierHostRegistered", None)
        self.assertTrue(run_loop_until(lambda: tray.hosted, 5))
        Gio.bus_unown_name(own)                             # the watcher goes
        conn.unregister_object(reg)
        self.assertTrue(run_loop_until(lambda: not tray.hosted, 5))
        self.assertEqual(changes, [True, False, True, False])


class FileManagers(unittest.TestCase):
    """install.sh with Nautilus and Dolphin "installed" (stand-ins): their
    "Send to" entries with the full path; none for absent ones."""

    def test_send_to_entries(self):
        home = tempfile.mkdtemp(prefix="phonebridge-test-home-")
        self.addCleanup(shutil.rmtree, home, True)
        fake = os.path.join(home, "fakebin")
        os.makedirs(fake)
        for program in ("nautilus", "dolphin"):
            with open(os.path.join(fake, program), "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(os.path.join(fake, program), 0o755)
        env = dict(os.environ, HOME=home, PHONEBRIDGE_VERSION="a" * 40, NO_AUTOSTART="1",
                   PHONEBRIDGE_NO_RESTART="1", LANG="de_DE.UTF-8",
                   XDG_CONFIG_HOME=os.path.join(home, ".config"),
                   XDG_DATA_HOME=os.path.join(home, ".local", "share"),
                   PATH=fake + os.pathsep + os.environ["PATH"])
        p = subprocess.run(["bash", os.path.join(ROOT, "install.sh")], env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        share = os.path.join(home, ".local", "share")
        binary = os.path.join(home, ".local", "bin", "phonebridge")
        script = os.path.join(share, "nautilus", "scripts", "An Phone senden (PhoneBridge)")
        self.assertTrue(os.access(script, os.X_OK))
        self.assertIn('exec "%s" --send' % binary, open(script).read())
        menu = os.path.join(share, "kio", "servicemenus", "io.github.miscde.PhoneBridge-send.desktop")
        self.assertTrue(os.access(menu, os.X_OK))                 # KDE wants it executable
        self.assertIn("Exec=%s --send %%F" % binary, open(menu).read())
        self.assertFalse(os.path.exists(os.path.join(share, "nemo")))      # no Nemo here
        launcher = open(os.path.join(share, "applications", "io.github.miscde.PhoneBridge.desktop")).read()
        self.assertIn("Exec=%s %%u" % binary, launcher)

        # its "phonebridge --quit" must reach no app: the bus is none
        subprocess.run(["bash", os.path.join(ROOT, "uninstall.sh")],
                       env=dict(env, DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent"),
                       capture_output=True, timeout=60)
        self.assertFalse(os.path.exists(script))
        self.assertFalse(os.path.exists(menu))


if __name__ == "__main__":
    unittest.main()
