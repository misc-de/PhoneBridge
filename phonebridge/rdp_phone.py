# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Runs on the phone, sent over ssh as the agent is (nothing is installed
there): a GNOME desktop of its own, for the PC only - gnome-shell without
a screen (--headless) and GNOME Remote Desktop, which shows it over RDP.
It ends, and clears up behind itself, when stdin closes.

Phosh goes on as it is. The desktop runs on a D-Bus of its own, on which
only what it needs may start (no second address book, calendar, online
accounts or keyring on the same data), with a dconf database of its own
(~/.config/dconf/phonebridge_desktop - Phosh's settings stay untouched;
the desktop's own stay from one session to the next). Files, apps and
their data are the user's as always.

gnome-shell draws without a GPU (the phone's goes through Android, which
only Phosh can use), so the environment Phosh's session hands on to its
services (libhybris' EGL, a preloaded shader cache) is cleared for it.

GNOME Remote Desktop listens on every interface. While the desktop runs a
firewall rule lets only the phone itself reach its port (sudo without a
password needed; "open" in PARAMS: without the rule, after the user has
agreed). The PC comes in through ssh: the phone's sshd forwards no ports,
so every RDP connection is an ssh of its own that runs RELAY here.

PARAMS comes before this code: password (for this session only); "open";
in the tests "fake" (an echo server stands for GNOME) and "firewall"
(False: no rule).

On stderr: "desktop: starting", "desktop: ready PORT FINGERPRINT",
"desktop: open" (no firewall rule), "desktop: error ..."."""

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

PARAMS = globals().get("PARAMS", {})
PORTS = range(3389, 3400)
USER = "phonebridge"
DCONF_DB = "phonebridge_desktop"     # dconf makes a D-Bus path of it: no "-"
SERVICES = ("ca.desrt.dconf", "org.a11y.Bus", "org.gnome.Shell.Notifications",
            "org.gnome.Shell.Screencast", "org.gnome.Shell.Extensions",
            "org.freedesktop.portal.Desktop", "org.freedesktop.impl.portal.desktop.gtk",
            "org.freedesktop.impl.portal.desktop.gnome",
            "org.freedesktop.impl.portal.PermissionStore")
# Phosh's session environment that would make gnome-shell look for the GPU
PHOSH_ONLY = ("__EGL_VENDOR_LIBRARY_FILENAMES", "LD_PRELOAD", "EGL_PLATFORM",
              "COGL_DISABLE_MAPBUFFERRANGE", "GDK_GL", "GSK_RENDERER", "GST_GL_API",
              "WLR_BACKENDS", "WLR_HWC_SKIP_VERSION_CHECK", "QT_QPA_PLATFORM",
              "QT_QUICK_CONTROLS_MOBILE", "QT_SCALE_FACTOR_ROUNDING_POLICY",
              "QT_WAYLAND_DISABLE_WINDOWDECORATION", "QTWEBENGINE_CHROMIUM_FLAGS",
              "PLYMOUTH_FORCE_SCALE", "XDG_MENU_PREFIX", "DISPLAY", "WAYLAND_DISPLAY",
              "DBUS_SESSION_BUS_ADDRESS", "LC_ALL")
GRD = "/usr/libexec/gnome-remote-desktop-daemon"

# One RDP connection: stdin/stdout of its ssh to the port, here.
RELAY = r"""
import os, socket, sys, threading
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
def back():
    while True:
        d = s.recv(65536)
        if not d:
            break
        while d:
            d = d[os.write(1, d):]
    os._exit(0)
threading.Thread(target=back, daemon=True).start()
while True:
    d = os.read(0, 65536)
    if not d:
        break
    s.sendall(d)
s.shutdown(socket.SHUT_WR)
threading.Event().wait(5)
"""


def say(line):
    sys.stderr.write("desktop: %s\n" % line)
    sys.stderr.flush()


def free_port(ports=PORTS):
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            continue
        finally:
            s.close()
        return port
    raise RuntimeError("no free port for RDP (%d-%d)" % (ports[0], ports[-1]))


def listening(port, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def certificate(folder):
    """A certificate for this session: (cert, key, sha256 fingerprint as
    "ab:cd:..." - the PC trusts exactly this one)."""
    cert, key = os.path.join(folder, "tls.crt"), os.path.join(folder, "tls.key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                    "-out", cert, "-days", "2", "-subj", "/CN=PhoneBridge desktop"],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    out = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-fingerprint", "-sha256"],
                         check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                         timeout=30).stdout
    return cert, key, out.strip().split("=", 1)[1].lower()


def bus_config(folder, sources="/usr/share/dbus-1/services"):
    """The desktop's own bus: only SERVICES may start on it, and each as a
    plain process (not as a unit of the user's systemd, which belongs to
    Phosh's session)."""
    services = os.path.join(folder, "services")
    os.makedirs(services, exist_ok=True)
    for name in SERVICES:
        try:
            with open(os.path.join(sources, name + ".service"), encoding="utf-8") as f:
                lines = [ln for ln in f if not ln.startswith("SystemdService=")]
        except OSError:
            continue
        with open(os.path.join(services, name + ".service"), "w", encoding="utf-8") as f:
            f.writelines(lines)
    path = os.path.join(folder, "bus.conf")
    with open(path, "w", encoding="utf-8") as f:
        f.write('<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"'
                ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n'
                "<busconfig><type>session</type><keep_umask/>"
                "<listen>unix:dir=%s</listen><auth>EXTERNAL</auth>"
                "<servicedir>%s</servicedir><policy context=\"default\">"
                "<allow send_destination=\"*\" eavesdrop=\"true\"/><allow eavesdrop=\"true\"/>"
                "<allow own=\"*\"/></policy></busconfig>\n" % (folder, services))
    return path


def desktop_env(folder, bus):
    env = {k: v for k, v in os.environ.items() if k not in PHOSH_ONLY}
    profile = os.path.join(folder, "dconf-profile")
    with open(profile, "w", encoding="utf-8") as f:
        f.write("user-db:%s\n" % DCONF_DB)
    env.update(DBUS_SESSION_BUS_ADDRESS=bus, DCONF_PROFILE=profile, GSETTINGS_BACKEND="dconf",
               XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="GNOME",
               XDG_SESSION_DESKTOP="gnome")
    return env


def quiet(argv, env=None, given=None, timeout=10):
    """Runs a helper - stdin never the PC's line (grdctl waits on it), and
    never an exception: (returncode or None, stderr)."""
    try:
        p = subprocess.run(argv, env=env, input=given if given is not None else "",
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    return p.returncode, p.stderr or ""


def firewall_argv(port, add, tool="iptables"):
    return ["sudo", "-n", tool, "-I" if add else "-D", "INPUT", "!", "-i", "lo", "-p", "tcp",
            "--dport", str(port), "-j", "DROP"]


class Session:
    def __init__(self, params):
        self.params = params
        self.procs = []
        self.rules = []
        self.folder = None
        self.env = None

    def run(self, argv, **kw):
        """A part of the desktop: in a process group of its own (ended as a
        whole), stdin never the PC's line."""
        p = subprocess.Popen(argv, env=self.env, stdin=subprocess.DEVNULL,
                             start_new_session=True, **kw)
        self.procs.append(p)
        return p

    def start(self):
        say("starting")
        base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        self.folder = tempfile.mkdtemp(prefix="phonebridge-desktop-", dir=base)
        port = free_port()
        if self.params.get("firewall", True):
            for tool in ("iptables", "ip6tables"):
                if shutil.which(tool) or os.path.exists("/usr/sbin/" + tool):
                    if quiet(firewall_argv(port, True, tool), timeout=20)[0] == 0:
                        self.rules.append((port, tool))
            if not self.rules:
                if not self.params.get("open"):
                    raise RuntimeError("no firewall rule (sudo asks for a password)")
                say("open")
        if self.params.get("fake"):
            fingerprint = self._fake(port)
        else:
            fingerprint = self._gnome(port)
        if not listening(port, 60):
            raise RuntimeError("GNOME Remote Desktop does not answer")
        say("ready %d %s" % (port, fingerprint))

    def _gnome(self, port):
        log = open(os.path.join(self.folder, "log"), "ab")
        daemon = self.run(["dbus-daemon", "--config-file", bus_config(self.folder), "--nofork",
                           "--print-address"], stdout=subprocess.PIPE, stderr=log)
        bus = daemon.stdout.readline().decode().strip()
        if not bus:
            raise RuntimeError("no D-Bus for the desktop")
        self.env = desktop_env(self.folder, bus)
        cert, key, fingerprint = certificate(self.folder)
        self.run(["gnome-shell", "--headless", "--wayland-display",
                  "wayland-phonebridge-%d" % os.getpid()], stdout=log, stderr=log)
        if not self._wait_name("org.gnome.Mutter.RemoteDesktop", 60):
            raise RuntimeError("gnome-shell did not start")
        schema = "org.gnome.desktop.remote-desktop.rdp.headless"
        for argv, given in ((["grdctl", "--headless", "rdp", "set-tls-cert", cert], None),
                            (["grdctl", "--headless", "rdp", "set-tls-key", key], None),
                            (["grdctl", "--headless", "rdp", "set-credentials", USER],
                             self.params["password"] + "\n"),
                            (["gsettings", "set", schema, "port", str(port)], None),
                            (["gsettings", "set", schema, "negotiate-port", "false"], None),
                            (["gsettings", "set", schema, "enable", "true"], None)):
            code, err = quiet(argv, self.env, given, timeout=30)
            if code != 0:
                raise RuntimeError("%s: %s" % (" ".join(argv[:4]), err.strip()[-200:]))
        self.run([GRD, "--headless"], stdout=log, stderr=log)
        return fingerprint

    def _wait_name(self, name, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                out = subprocess.run(["gdbus", "call", "--session", "-d", "org.freedesktop.DBus",
                                      "-o", "/org/freedesktop/DBus", "-m",
                                      "org.freedesktop.DBus.NameHasOwner", name],
                                     env=self.env, stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=10).stdout
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            if "true" in out:
                return True
            if any(proc.poll() is not None for proc in self.procs):
                return False
            time.sleep(0.5)
        return False

    def _fake(self, port):
        """The tests: an echo server where GNOME Remote Desktop would be."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(5)

        def serve():
            while True:
                conn, _a = server.accept()
                threading.Thread(target=echo, args=(conn,), daemon=True).start()

        def echo(conn):
            while True:
                d = conn.recv(65536)
                if not d:
                    break
                conn.sendall(d)
            conn.close()

        threading.Thread(target=serve, daemon=True).start()
        return "00:11:22"

    def stop(self):
        """Everything this started ends; the rule, the session's password
        and its folder go. The password and the switch first - they need
        the desktop's bus still."""
        if self.env is not None:
            quiet(["grdctl", "--headless", "rdp", "clear-credentials"], self.env)
            quiet(["gsettings", "set", "org.gnome.desktop.remote-desktop.rdp.headless",
                   "enable", "false"], self.env)
        for p in reversed(self.procs):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
        for port, tool in self.rules:
            quiet(firewall_argv(port, False, tool), timeout=20)
        self.rules = []
        if self.folder:
            shutil.rmtree(self.folder, ignore_errors=True)


def main():
    session = Session(PARAMS)
    done = threading.Event()

    def end(*args):
        if not done.is_set():
            done.set()
            try:
                session.stop()
            except Exception as e:  # noqa: BLE001 - never leave without the rest
                say("error while clearing up: %s" % e)
        sys.stderr.flush()
        os._exit(0)

    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, end)

    def work():
        try:
            session.start()
        except Exception as e:  # noqa: BLE001 - told to the PC, then cleared up
            say("error %s" % e)
            end()

    threading.Thread(target=work, daemon=True).start()
    while sys.stdin.buffer.read(4096):
        pass
    end()


main()
