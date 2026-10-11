# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""A GNOME desktop of the phone's own over RDP. The phone's side runs here
through the stand-in ssh with an echo server instead of GNOME; the RDP
client is a small script that sends through the relay and reads the echo.
No firewall rule is set (sudo fails here on purpose)."""

import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from phonebridge import agent, config, rdp  # noqa: E402

from .fake_eds import EDS, FakeEDS  # noqa: E402
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
        apps = os.path.join(d, "apps")
        os.makedirs(apps)
        for name, activated in (("org.gnome.Settings", True), ("sm.puri.Chatty", True),
                                ("org.gnome.Calculator", False)):
            with open(os.path.join(sources, name + ".service"), "w") as f:
                f.write("[D-BUS Service]\nName=%s\nExec=/bin/true\n" % name)
            with open(os.path.join(apps, name + ".desktop"), "w") as f:
                f.write("[Desktop Entry]\nName=x\n%s" % ("DBusActivatable=true\n" * activated))
        conf = ns["bus_config"](d, sources, ((apps, sources), ("/nonexistent", "/nonexistent")))
        self.assertIn("<servicedir>%s/services</servicedir>" % d, open(conf).read())
        made = sorted(os.listdir(os.path.join(d, "services")))
        # no address book on this bus, and no second Chatty - but the apps
        self.assertEqual(made, ["ca.desrt.dconf.service", "org.gnome.Settings.service"])
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
        for name in ("LD_PRELOAD", "WAYLAND_DISPLAY"):
            self.assertNotIn(name, env)
        self.assertNotEqual(env.get("__EGL_VENDOR_LIBRARY_FILENAMES"), "hybris.json")
        self.assertEqual(env["HOME"], "/home/x")             # the user's files, as always
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/tmp/bus")
        self.assertEqual(open(env["DCONF_PROFILE"]).read(), "user-db:phonebridge_desktop\n")
        with mock.patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/phosh"}):
            env = ns["desktop_env"](d)                      # for the bus: never Phosh's
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertEqual(env["XDG_CURRENT_DESKTOP"], "GNOME")

    def fake_cpus(self, d):
        """FLX1s with the screen off: only cpu7 online, in powersave."""
        for n in range(8):
            os.makedirs(os.path.join(d, "cpu%d" % n))
            with open(os.path.join(d, "cpu%d" % n, "online"), "w") as f:
                f.write("1\n" if n == 7 else "0\n")
        for policy, governor in (("policy0", "schedutil"), ("policy6", "powersave")):
            os.makedirs(os.path.join(d, "cpufreq", policy))
            with open(os.path.join(d, "cpufreq", policy, "scaling_governor"), "w") as f:
                f.write(governor + "\n")
            with open(os.path.join(d, "cpufreq", policy, "scaling_available_governors"), "w") as f:
                f.write("ondemand userspace powersave performance schedutil \n")
        os.makedirs(os.path.join(d, "cpufreq", "x"))       # not a policy

    def test_cores(self):
        ns = phone_side()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.fake_cpus(d)
        cores = ns["Cores"](d, sudo=False)
        found = cores.state()
        cores.boost()
        online, governors = cores.state()
        self.assertEqual(set(online.values()), {"1"})
        self.assertEqual(governors, {"policy0": "schedutil", "policy6": "schedutil"})
        cores.boost()                                       # once is enough
        cores.release()
        self.assertEqual(cores.state(), found)              # as it was
        cores.release()                                     # nothing to give back
        self.assertEqual(cores.state(), found)

    def test_cores_batman_wins(self):
        ns = phone_side()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.fake_cpus(d)
        cores = ns["Cores"](d, sudo=False)
        cores.boost()
        with open(os.path.join(d, "cpu3", "online"), "w") as f:
            f.write("0\n")                                  # batman, meanwhile
        cores.release()
        self.assertEqual(cores.state()[0]["cpu0"], "1")     # left as batman made it
        self.assertEqual(cores.state()[0]["cpu3"], "0")

    def test_cores_without_sudo(self):
        ns = phone_side()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.fake_cpus(d)
        cores = ns["Cores"](d)
        calls = []
        with mock.patch.dict(cores.boost.__globals__,
                             quiet=lambda argv, *a, **k: calls.append(argv) or (1, "")):
            cores.boost()
            cores.boost()
        self.assertEqual(len(calls), 1)                     # asked once only
        self.assertEqual(calls[0][:4], ["sudo", "-n", "sh", "-c"])
        self.assertIn("echo 1 > %s/cpu0/online" % d, calls[0][4])
        self.assertIsNone(cores.saved)

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


@unittest.skipUnless(shutil.which("dbus-daemon"), "no dbus-daemon")
class Bridge(unittest.TestCase):
    """The phone's EDS (here on the private test bus) on the desktop's bus."""

    def setUp(self):
        self.ns = phone_side()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        conf = self.ns["bus_config"](d, os.path.join(d, "none"), ())
        daemon = subprocess.Popen(["dbus-daemon", "--config-file", conf, "--nofork",
                                   "--print-address"], stdout=subprocess.PIPE)
        self.addCleanup(daemon.wait)
        self.addCleanup(daemon.terminate)
        address = daemon.stdout.readline().decode().strip()
        self.eds = FakeEDS()
        self.addCleanup(self.eds.close)
        self.eds.contacts["a"] = "BEGIN:VCARD\r\nUID:a\r\nFN:Anna\r\nEND:VCARD\r\n"
        session = self.ns["Session"]({})
        session.env = dict(os.environ)
        self.addCleanup(session.stop)
        log = open(os.devnull, "wb")
        self.addCleanup(log.close)
        self.bridge = session._bridge(address, log, os.environ["DBUS_SESSION_BUS_ADDRESS"])
        self.app = Gio.DBusConnection.new_for_address_sync(
            address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        self.addCleanup(self.app.close_sync, None)

    def call(self, name, path, iface, method, args=None):
        got = []
        self.app.call(name, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 5000,
                      None, lambda c, res: got.append(self._finish(c, res)))
        self.assertTrue(run_loop_until(lambda: got))
        return got[0]

    def _finish(self, conn, res):
        try:
            return conn.call_finish(res).unpack()
        except GLib.Error as e:
            return e

    def has(self, name):
        return self.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                         "org.freedesktop.DBus", "NameHasOwner", GLib.Variant("(s)", (name,)))[0]

    def test_address_book(self):
        self.assertIsNone(self.bridge.poll())
        path, name = self.call(EDS + "AddressBook10",
                               "/org/gnome/evolution/dataserver/AddressBookFactory",
                               EDS + "AddressBookFactory", "OpenAddressBook",
                               GLib.Variant("(s)", ("book-contacts",)))
        cards = self.call(name, path, EDS + "AddressBook", "GetContactList",
                          GLib.Variant("(s)", ("",)))[0]
        self.assertIn("FN:Anna", cards[0])
        missing = self.call(name, path, EDS + "AddressBook", "GetContact",
                            GLib.Variant("(s)", ("nobody",)))
        self.assertIn("ContactNotFound", missing.message)     # EDS's errors as they are
        self.assertTrue(self.has(EDS + "UserPrompter0"))       # started on Phosh's bus
        self.assertTrue(self.has("org.gnome.OnlineAccounts"))   # the accounts of the books

    def test_signals_and_backends(self):
        heard = []
        self.app.signal_subscribe(None, EDS + "AddressBook", "Changed", "/book/1", None,
                                  Gio.DBusSignalFlags.NONE, lambda *a: heard.append(a[5]))
        self.eds.conn.emit_signal(None, "/book/1", EDS + "AddressBook", "Changed",
                                  GLib.Variant("(s)", ("a",)))
        self.assertTrue(run_loop_until(lambda: heard))
        self.assertEqual(heard[0].unpack(), ("a",))
        backend = EDS + "Subprocess.Backend.AddressBookx1x2"   # as EDS starts one
        own = Gio.bus_own_name_on_connection(self.eds.conn, backend,
                                             Gio.BusNameOwnerFlags.NONE, None, None)
        self.assertTrue(run_loop_until(lambda: self.has(backend)))
        cards = self.call(backend, "/book/1", EDS + "AddressBook", "GetContactList",
                          GLib.Variant("(s)", ("",)))[0]
        self.assertEqual(len(cards), 1)
        Gio.bus_unown_name(own)
        self.assertTrue(run_loop_until(lambda: not self.has(backend)))
        self.assertTrue(self.has(EDS + "AddressBook10"))      # the factories stay


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
        # the overview: how a running one is doing
        from phonebridge.overview import OverviewPage
        overview = OverviewPage(app)
        overview.set_device(dev)
        self.assertFalse(overview.rdp_info.get_visible())
        app.set_rdp(dev, True, allow_open=True)
        self.assertIn("test", app.rdp_sessions)
        session = app.rdp_sessions["test"]
        self.assertTrue(overview.rdp_info.get_visible())
        self.assertEqual(overview.rdp_state.get_label(), "Starting …")
        session._said(session.ssh, "cores on")
        self.assertIn("all cores at work", overview.rdp_lines.get_label())
        self.assertTrue(session.state_text.startswith("Starting"))  # not the cores' news
        self.assertTrue(run_loop_until(lambda: session.since or "test" not in app.rdp_sessions,
                                       30))
        if "test" in app.rdp_sessions:
            self.assertEqual(overview.rdp_state.get_label(), "Running")
            self.assertIn("since ", overview.rdp_lines.get_label())
            self.assertEqual(overview.rdp_warning.get_visible(), bool(session.open))
        self.assertTrue(run_loop_until(lambda: "test" not in app.rdp_sessions, 30))
        self.assertFalse(overview.rdp_info.get_visible())
