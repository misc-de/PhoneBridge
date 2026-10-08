# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""A GNOME desktop of the phone's own over RDP. The phone's side runs here
through the stand-in ssh with an echo server instead of GNOME; the RDP
client is a small script that sends through the relay and reads the echo.
No firewall rule is set (sudo fails here on purpose)."""

import os
import runpy
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from phonebridge import agent, config, rdp  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
FAKE = {"fake": True, "firewall": False}


def phone_side():
    src = open(rdp.PHONE_SCRIPT).read().replace("\nmain()\n", "\n")
    return runpy._run_code(compile(src, "rdp_phone", "exec"), {"PARAMS": {}})


def fake_client(folder):
    """A stand-in for xfreerdp3: reads its arguments from stdin, sends
    "ping" to /v: and writes what came back (and the arguments) to files."""
    path = os.path.join(folder, "client.py")
    with open(path, "w") as f:
        f.write(textwrap.dedent("""
            import socket, sys
            args = sys.stdin.read().splitlines()
            open(%r, "w").write("\\n".join(args))
            host, port = [a for a in args if a.startswith("/v:")][0][3:].rsplit(":", 1)
            s = socket.create_connection((host, int(port)), 10)
            s.sendall(b"ping")
            got = b""
            while len(got) < 4:
                got += s.recv(10)
            open(%r, "wb").write(got)
        """ % (os.path.join(folder, "args"), os.path.join(folder, "echo"))))
    return [sys.executable, path]


class Pieces(unittest.TestCase):
    def test_client_args(self):
        args = rdp.client_args(4567, "s3cret", "ab:cd", (1600, 900), "Phone - Desktop")
        lines = args.splitlines()
        self.assertIn("/v:127.0.0.1:4567", lines)
        self.assertIn("/p:s3cret", lines)                   # on stdin, never in argv
        self.assertIn("/cert:deny,fingerprint:sha256:ab:cd", lines)
        self.assertIn("/size:1600x900", lines)
        self.assertIn("/title:Phone - Desktop", lines)

    def test_password(self):
        a, b = rdp.password(), rdp.password()
        self.assertEqual(len(a), 24)
        self.assertNotEqual(a, b)

    def test_install_hint(self):
        f = tempfile.NamedTemporaryFile("w", delete=False)
        f.write("ID=manjaro\nID_LIKE=arch\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(rdp.install_hint(f.name), "sudo pacman -S freerdp")

    def test_bus_and_firewall(self):
        ns = phone_side()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        sources = os.path.join(d, "sources")
        os.makedirs(sources)
        for name in ("ca.desrt.dconf", "org.gnome.evolution.dataserver.Sources5"):
            with open(os.path.join(sources, name + ".service"), "w") as f:
                f.write("[D-BUS Service]\nName=%s\nExec=/bin/true\nSystemdService=x.service\n"
                        % name)
        conf = ns["bus_config"](d, sources)
        self.assertIn("<servicedir>%s/services</servicedir>" % d, open(conf).read())
        made = os.listdir(os.path.join(d, "services"))
        self.assertEqual(made, ["ca.desrt.dconf.service"])  # no address book on this bus
        self.assertNotIn("SystemdService", open(os.path.join(d, "services", made[0])).read())
        self.assertEqual(ns["firewall_argv"](3390, True),
                         ["sudo", "-n", "iptables", "-I", "INPUT", "!", "-i", "lo", "-p", "tcp",
                          "--dport", "3390", "-j", "DROP"])
        self.assertEqual(ns["firewall_argv"](3390, False, "ip6tables")[2:4], ["ip6tables", "-D"])

    def test_env_leaves_phosh_alone(self):
        ns = phone_side()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.dict(os.environ, {"LD_PRELOAD": "libglesshadercache.so",
                                          "__EGL_VENDOR_LIBRARY_FILENAMES": "hybris.json",
                                          "WAYLAND_DISPLAY": "wayland-0", "HOME": "/home/x"}):
            env = ns["desktop_env"](d, "unix:path=/tmp/bus")
        for name in ("LD_PRELOAD", "__EGL_VENDOR_LIBRARY_FILENAMES", "WAYLAND_DISPLAY"):
            self.assertNotIn(name, env)
        self.assertEqual(env["HOME"], "/home/x")             # the user's files, as always
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/tmp/bus")
        self.assertEqual(open(env["DCONF_PROFILE"]).read(), "user-db:phonebridge_desktop\n")

    @unittest.skipUnless(shutil.which("openssl"), "no openssl")
    def test_certificate(self):
        ns = phone_side()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        cert, key, fingerprint = ns["certificate"](d)
        self.assertTrue(os.path.exists(cert) and os.path.exists(key))
        self.assertRegex(fingerprint, r"^([0-9a-f]{2}:){31}[0-9a-f]{2}$")

    def test_check_on_the_phone(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        grd = os.path.join(d, "grd")
        open(grd, "w").close()
        with mock.patch.dict(os.environ, {"PHONEBRIDGE_GRD": grd,
                                          "PHONEBRIDGE_GNOME_SHELL": "/nonexistent"}):
            found = agent.desktop_check()
        self.assertFalse(found["gnome_shell"])
        self.assertEqual(found["remote_desktop"], bool(shutil.which("grdctl")))
        self.assertFalse(found["firewall"])                 # sudo fails here


class Session(unittest.TestCase):
    def dev(self):
        return mock.Mock(info={"id": "t", "host": "phone", "user": "me"}, password=None)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_through_the_relay_and_back(self):
        dev = self.dev()
        dev.name = "Testphone"
        session = rdp.Desktop(dev, size=(1600, 900), phone_params=FAKE,
                              client_argv=fake_client(self.dir))
        states, stopped = [], []
        session.connect("state", lambda s, st: states.append(st))
        session.connect("stopped", lambda s, r: stopped.append(r))
        self.assertTrue(session.start())
        self.assertTrue(run_loop_until(lambda: stopped, 30))
        self.assertEqual(stopped, [None])                    # the client ended: the session too
        self.assertEqual(states, ["starting", "ready", "client"])
        self.assertEqual(open(os.path.join(self.dir, "echo"), "rb").read(), b"ping")
        args = open(os.path.join(self.dir, "args")).read().splitlines()
        self.assertIn("/p:" + session.secret, args)
        self.assertIn("/size:1600x900", args)
        self.assertIn("/title:Testphone - Desktop", args)
        self.assertIsNone(session.ssh)

    def test_without_a_firewall_rule_it_asks(self):
        dev = self.dev()
        dev.name = "T"
        session = rdp.Desktop(dev, phone_params={"fake": True},
                              client_argv=fake_client(self.dir))
        stopped = []
        session.connect("stopped", lambda s, r: stopped.append(r))
        session.start()
        self.assertTrue(run_loop_until(lambda: stopped, 30))
        self.assertIn("firewall", stopped[0])               # sudo asks: not started
        opened = rdp.Desktop(dev, allow_open=True, phone_params={"fake": True},
                             client_argv=fake_client(self.dir))
        states, stopped = [], []
        opened.connect("state", lambda s, st: states.append(st))
        opened.connect("stopped", lambda s, r: stopped.append(r))
        opened.start()
        self.assertTrue(run_loop_until(lambda: stopped, 30))
        self.assertIn("open", states)                        # told: reachable in the network
        self.assertEqual(stopped, [None])

    def test_stop_ends_the_phone_side(self):
        dev = self.dev()
        dev.name = "T"
        session = rdp.Desktop(dev, phone_params=FAKE,
                              client_argv=[sys.executable, "-c", "import time; time.sleep(60)"])
        states = []
        session.connect("state", lambda s, st: states.append(st))
        session.start()
        self.assertTrue(run_loop_until(lambda: "client" in states, 30))
        ssh, client = session.ssh, session.client
        session.stop()
        self.assertTrue(run_loop_until(lambda: ssh.poll() is not None, 10))
        self.assertTrue(run_loop_until(lambda: client.poll() is not None, 10))


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class InTheApp(unittest.TestCase):
    def test_panel(self):
        from phonebridge.app import PhoneBridgeApp
        from phonebridge.rdp_ui import DesktopPanel, size_name, sizes
        home = Home()
        self.addCleanup(home.cleanup)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for p in (mock.patch.dict(os.environ, home.env()),
                  mock.patch.object(config, "CONFIG_DIR", home.config),
                  mock.patch.object(rdp, "client", lambda: "xfreerdp3")):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestRdp")
        app.send_notification = lambda *a: None
        told = []
        app.tell = app.toast = told.append
        app._rdp_extra = {"phone_params": dict(FAKE), "client_argv": fake_client(d)}
        app.register(None)
        self.addCleanup(app.do_shutdown)
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online, 20))

        self.assertEqual(sizes()[0], (1280, 720))               # 720p upwards
        self.assertEqual(size_name((1920, 1080)), "1920 × 1080 (1080p)")
        self.assertIn("slow", size_name((3840, 2160)))
        panel = DesktopPanel(app)
        panel.set_device(dev)
        self.assertTrue(run_loop_until(lambda: panel.check is not None, 20))
        self.assertFalse(panel.met()["firewall"])               # sudo fails here
        # GNOME is not on this "phone": the start waits, with a way to set it up
        if not panel.check["gnome_shell"]:
            self.assertFalse(panel.button.get_sensitive())
            heading, body, command = panel.instructions("gnome_shell")
            self.assertEqual(command, "sudo apt install gnome-shell")
            self.assertIn("ssh me@phone", body)
        heading, body, command = panel.instructions("firewall")
        self.assertIn("NOPASSWD: ", command)
        self.assertIn("/etc/sudoers.d/phonebridge-desktop", command)
        with mock.patch.object(Adw.AlertDialog, "present", lambda d, p=None: None):
            self.assertIsNotNone(panel.explain("remote_desktop"))

        # as if the phone had everything: without the rule it asks first
        panel.check = dict(panel.check, gnome_shell=True, remote_desktop=True, openssl=True,
                           dbus_daemon=True)
        panel.update()
        self.assertTrue(panel.button.get_sensitive())
        panel.size.set_selected(sizes().index((1920, 1080)))
        self.assertEqual(app.cfg["screen"]["desktop_size"], [1920, 1080])
        with mock.patch.object(Adw.AlertDialog, "present", lambda d, p=None: None):
            dialog = panel._on_button()
        self.assertNotIn("test", app.rdp_sessions)
        dialog.emit("response", "go")                           # start anyway
        self.assertIn("test", app.rdp_sessions)
        self.assertEqual(app.rdp_sessions["test"].size, (1920, 1080))
        self.assertEqual(panel.button.get_label(), "End")
        # the client is the test's: it sends, reads the echo and ends - so does the session
        self.assertTrue(run_loop_until(lambda: "test" not in app.rdp_sessions, 30))
        self.assertEqual(open(os.path.join(d, "echo"), "rb").read(), b"ping")
        self.assertEqual(panel.button.get_label(), "Start")
