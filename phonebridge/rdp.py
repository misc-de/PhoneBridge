# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""A GNOME desktop of the phone's own, shown on the PC over RDP.

rdp_phone.py starts it on the phone (over an ssh of its own; it ends
with it) and says on which port GNOME Remote Desktop listens and which
certificate it has. The phone's sshd forwards no ports, so here a port
on 127.0.0.1 takes the RDP client's connections, and each goes on as an
ssh of its own (rdp_phone.RELAY). The client is FreeRDP 3; its
arguments - the session's password among them - go in on stdin, never on
its command line, and it trusts only the session's certificate."""

import os
import secrets
import shlex
import shutil
import socket
import string
import subprocess
import threading

from gi.repository import GLib, GObject

from .connection import BOOTSTRAP, ssh_argv

PHONE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rdp_phone.py")
CLIENTS = ("xfreerdp3", "sdl-freerdp3")
# what the desktop may be: 720p upwards (its pixels cost the phone's CPU)
SIZES = ((1280, 720), (1280, 800), (1366, 768), (1600, 900), (1680, 1050), (1920, 1080),
         (1920, 1200), (2560, 1440), (2560, 1600), (3840, 2160))
NAMES = {(1280, 720): "720p", (1920, 1080): "1080p", (2560, 1440): "1440p", (3840, 2160): "4K"}
USER = "phonebridge"


def client():
    """The RDP client here (FreeRDP 3 - FreeRDP 2 has no /args-from), or None."""
    return next((c for c in CLIENTS if shutil.which(c)), None)


def install_hint(release="/etc/os-release"):
    from . import deps
    return {"arch": "sudo pacman -S freerdp",
            "debian": "sudo apt install freerdp3-x11",
            "fedora": "sudo dnf install freerdp",
            "suse": "sudo zypper install freerdp"}.get(deps.distro(release))


def password():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))


def client_args(port, secret, fingerprint, size, title):
    """FreeRDP's arguments, one a line, for /args-from:stdin."""
    return "\n".join(("/v:127.0.0.1:%d" % port, "/u:" + USER, "/p:" + secret,
                      "/cert:deny,fingerprint:sha256:" + fingerprint,
                      "/size:%dx%d" % size, "/smart-sizing", "/sound", "+clipboard",
                      "/title:" + title)) + "\n"


class Desktop(GObject.Object):
    """One desktop session. start() once; "state" tells "starting", "open"
    (no firewall rule on the phone), "ready" and "client" (the RDP window is
    up); "stopped" carries why it ended (None when stop() was asked for or
    the RDP window was closed)."""

    __gsignals__ = {"state": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
                    "stopped": (GObject.SignalFlags.RUN_FIRST, None, (object,))}

    def __init__(self, device, size=(1280, 800), allow_open=False, phone_params=None,
                 client_argv=None):
        super().__init__()
        self.info, self.password, self.name = dict(device.info), device.password, device.name
        self.size = tuple(size)
        self.secret = password()
        self.params = dict(password=self.secret, open=bool(allow_open))
        self.params.update(phone_params or {})
        self.client_argv = client_argv          # the tests' client
        self.ssh = None
        self.client = None
        self.listener = None
        self.port = None                        # the phone's RDP port
        self.local_port = None
        self.fingerprint = None
        self.relays = []
        self.running = False
        self._error = None

    def _env(self):
        if self.password is None:
            return None
        from .secrets import ssh_env
        return ssh_env(self.password)

    def start(self):
        code = ("PARAMS = %r\n" % self.params).encode() + open(PHONE_SCRIPT, "rb").read()
        try:
            self.ssh = subprocess.Popen(
                ssh_argv(self.info, BOOTSTRAP, password=self.password is not None),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env=self._env())
            self.ssh.stdin.write(b"%d\n" % len(code) + code)
            self.ssh.stdin.flush()          # stays open: closing it ends the desktop
        except OSError as e:
            self.stop(str(e))
            return False
        self.running = True
        threading.Thread(target=self._read, args=(self.ssh,), daemon=True).start()
        return True

    def _read(self, ssh):
        for line in ssh.stderr:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("desktop: "):
                GLib.idle_add(self._said, ssh, line[9:])
            elif line.startswith("ssh:") or "Permission denied" in line:
                self._error = line
        ssh.wait()
        GLib.idle_add(self._ssh_ended, ssh)

    def _said(self, ssh, what):
        if ssh is not self.ssh:
            return False
        if what.startswith("ready "):
            port, fingerprint = what.split()[1:3]
            self.port, self.fingerprint = int(port), fingerprint
            self.emit("state", "ready")
            self._open_client()
        elif what.startswith("error "):
            self._error = what[6:]
        else:
            self.emit("state", what)
        return False

    def _ssh_ended(self, ssh):
        if self.running and self.ssh is ssh:
            self.stop(self._error or "the phone ended the desktop")
        return False

    # -- the way in ----------------------------------------------------------------------
    def _open_client(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.local_port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, args=(self.listener,), daemon=True).start()
        argv = self.client_argv or [client() or CLIENTS[0], "/args-from:stdin"]
        title = "%s - %s" % (self.name, "Desktop")
        try:
            self.client = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.client.stdin.write(client_args(self.local_port, self.secret, self.fingerprint,
                                                self.size, title).encode())
            self.client.stdin.close()
        except OSError as e:
            self.stop(str(e))
            return
        self.emit("state", "client")
        threading.Thread(target=self._client_ended, args=(self.client,), daemon=True).start()

    def _accept(self, listener):
        """Every connection of the client: an ssh to the phone's port."""
        command = "python3 -c %s %d" % (shlex.quote(_relay()), self.port)
        while True:
            try:
                conn, _addr = listener.accept()
            except OSError:
                return              # closed: the session ended
            try:
                relay = subprocess.Popen(
                    ssh_argv(self.info, command, low_delay=True,
                             password=self.password is not None),
                    stdin=conn.fileno(), stdout=conn.fileno(), stderr=subprocess.DEVNULL,
                    env=self._env())
            except OSError:
                conn.close()
                continue
            conn.close()            # ssh has it now
            self.relays.append(relay)

    def _client_ended(self, proc):
        err = proc.stderr.read().decode("utf-8", "replace")
        code = proc.wait()
        GLib.idle_add(self._client_gone, proc, code, err)

    def _client_gone(self, proc, code, err):
        if proc is not self.client or not self.running:
            return False
        reason = None
        if code not in (0, 12, 13):     # closed by the user / logged off
            lines = [ln for ln in err.splitlines() if "ERROR" in ln]
            reason = (lines[-1].split("]: ", 1)[-1] if lines else
                      "the RDP client ended (%d)" % code)
        self.stop(reason)
        return False

    def stop(self, reason=None):
        if self.client is not None and self.client.poll() is None:
            self.client.terminate()
        self.client = None
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
            self.listener = None
        for relay in self.relays:
            if relay.poll() is None:
                relay.terminate()
        self.relays = []
        if self.ssh is not None:
            ssh, self.ssh = self.ssh, None
            try:
                ssh.stdin.close()           # the phone clears up the desktop
            except OSError:
                pass
            threading.Thread(target=_end_ssh, args=(ssh,), daemon=True).start()
        was = self.running
        self.running = False
        if was or reason is not None:
            self.emit("stopped", reason)


def _relay():
    import runpy
    src = open(PHONE_SCRIPT, encoding="utf-8").read().replace("\nmain()\n", "\n")
    return runpy._run_code(compile(src, "rdp_phone", "exec"), {"PARAMS": {}})["RELAY"]


def _end_ssh(ssh):
    try:
        ssh.wait(30)            # the phone clears up first (a few seconds)
    except subprocess.TimeoutExpired:
        ssh.kill()
        ssh.wait()
