# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""One phone: an SSH connection that carries the agent and its answers.

ssh starts `python3` on the phone, which first reads the length of the
agent's code and the code itself from stdin and runs it; from then on the
same stdin/stdout carry the JSON lines of agent.py. Nothing is stored on
the phone, and no port is opened there - SSH with your key is all it takes.

When the connection drops, it comes back on its own: after 3, 5, 10, 30 and
then every 60 seconds, or right away with reconnect()."""

import json
import os
import queue
import shlex
import threading

from gi.repository import Gio, GLib, GObject

AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.py")
BOOTSTRAP = ("python3 -u -c 'import sys;n=int(sys.stdin.buffer.readline());"
             "exec(compile(sys.stdin.buffer.read(n),\"phonebridge-agent\",\"exec\"))'")
BACKOFF = (3, 5, 10, 30, 60)
# seconds a request may take before it fails with "timeout"
TIMEOUT = 45
TIMEOUTS = {"sms.send": 120, "pim.sources": 90, "contacts.list": 150,
            "contacts.save": 150, "contacts.delete": 150, "calendar.events": 150,
            "calendar.save": 150, "calendar.delete": 150, "voicebox.audio": 120,
            "sms.delete_thread": 60, "sip.save": 60, "sip.delete": 60,
            "files.list": 60, "files.thumbs": 90, "files.delete": 300,
            "screen.shot": 60, "desktop.check": 60, "hotspot.set": 60}
# a ping this often; no answer in PING_TIMEOUT and the connection is dead
PING_EVERY = 20
PING_TIMEOUT = 20


def ssh_argv(device, command=BOOTSTRAP, low_delay=False, password=False):
    """ssh to the phone. With password=True it may ask for one - and gets it
    from SSH_ASKPASS (secrets.ssh_env), once; else only the key counts.
    An unknown phone is learnt on first contact, a changed host key is
    still refused."""
    from .hotspot import reach
    device = reach(device)          # in the phone's hotspot: its gateway address
    ssh = shlex.split(os.environ.get("PHONEBRIDGE_SSH", "ssh"))
    extra = ["-o", "IPQoS=lowdelay", "-o", "Compression=no"] if low_delay else []
    auth = (["-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1",
             "-o", "PreferredAuthentications=publickey,keyboard-interactive,password"]
            if password else ["-o", "BatchMode=yes"])
    return ssh + ["-T"] + auth + [
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"] + extra + [
        "-p", str(device.get("port") or 22),
        "--", "%s@%s" % (device["user"], device["host"]), command]


def agent_payload():
    with open(AGENT, "rb") as f:
        code = f.read()
    return b"%d\n" % len(code) + code


class Device(GObject.Object):
    """States: "offline" (not started or waiting to reconnect),
    "connecting", "online". `error` tells why the last attempt failed."""

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "sms": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "calls": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # the key is refused (and no password, or a wrong one): bool wrong
        "auth-needed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "voicebox": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "media": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # a notification of the phone's apps; and one closed there (its id)
        "notification": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "notification-closed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "clipboard": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, info):
        super().__init__()
        self.info = dict(info)
        self.state = "offline"
        self.error = None
        self.status = None
        self.hello = None
        self.password = None        # from the keyring, or typed in; never stored here
        self.needs_password = False
        self._proc = None
        self._cancel = None
        self._stdin = None
        self._next_id = 1
        self._pending = {}
        self._stderr = []
        self._queue = None
        self._retry = 0
        self._retry_source = 0
        self._ping_source = 0
        self._running = False

    @property
    def id(self):
        return self.info["id"]

    @property
    def name(self):
        return self.info.get("name") or self.info["host"]

    @property
    def online(self):
        return self.state == "online"

    # -- lifecycle --------------------------------------------------------
    def start(self):
        self._running = True
        if self._proc is None:
            self._connect()

    def stop(self):
        self._running = False
        self._clear_retry()
        self._stop_ping()
        proc, q = self._proc, self._queue
        self._proc, self._stdin, self._queue = None, None, None
        if proc is not None:
            # end of input first (the writer closes the pipe once what is
            # queued is out): the agent ends by itself and cleans up - gives
            # the phone's microphone back, ...; killed only if it lingers
            self._cancel.cancel()
            q.put(None)
            GLib.timeout_add(2000, lambda: proc.get_identifier() and proc.force_exit()
                             and False)
        self._fail_pending("not connected")
        self._set_state("offline")

    def reconnect(self):
        """Connect now - ending a connection that hangs, if there is one."""
        self._clear_retry()
        self._retry = 0
        self._running = True
        if self._proc is None:
            self._connect()
        else:
            self._now = True
            self._proc.force_exit()

    def _clear_retry(self):
        if self._retry_source:
            GLib.source_remove(self._retry_source)
            self._retry_source = 0

    def _set_state(self, state, error=None):
        self.state = state
        if error is not None or state == "online":
            self.error = error
        self.emit("changed")

    def _connect(self):
        self._stderr = []
        self._cancel = Gio.Cancellable()
        try:
            launcher = Gio.SubprocessLauncher.new(
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
                | Gio.SubprocessFlags.STDERR_PIPE)
            if self.password is not None:
                from .secrets import ssh_env
                launcher.set_environ([
                    "%s=%s" % kv for kv in ssh_env(self.password).items()])
            proc = launcher.spawnv(ssh_argv(self.info, password=self.password is not None))
        except GLib.Error as e:
            self._set_state("offline", e.message)
            self._schedule_retry()
            return
        self._proc = proc
        self._set_state("connecting")
        self._stdin = proc.get_stdin_pipe()
        # a thread of its own writes: the agent is larger than a pipe holds,
        # ssh reads it only once it is connected, and a GIO pipe write would
        # block the window meanwhile
        self._queue = queue.Queue()
        threading.Thread(target=_writer, args=(self._stdin, self._queue),
                         daemon=True).start()
        self._queue.put(agent_payload())
        out = Gio.DataInputStream.new(proc.get_stdout_pipe())
        err = Gio.DataInputStream.new(proc.get_stderr_pipe())
        out.read_line_async(GLib.PRIORITY_DEFAULT, self._cancel, self._on_line, proc)
        err.read_line_async(GLib.PRIORITY_DEFAULT, self._cancel, self._on_err, proc)
        proc.wait_async(None, self._on_exit)
        self.request("hello", {}, self._on_hello, timeout=30)

    def _schedule_retry(self):
        if not self._running:
            return
        delay = BACKOFF[min(self._retry, len(BACKOFF) - 1)]
        if getattr(self, "_now", False):
            self._now, delay = False, 0
        self._retry += 1
        self._clear_retry()
        self._retry_source = GLib.timeout_add_seconds(delay, self._retry_now)

    def _retry_now(self):
        self._retry_source = 0
        if self._proc is None and self._running:
            self._connect()
        return False

    def _on_hello(self, result, error):
        if error is not None:
            if error == "timeout" and self._proc is not None:
                self._proc.force_exit()         # nothing answers: anew
            return
        self.hello = result
        self._retry = 0
        self.needs_password = False
        self._set_state("online")
        self._stop_ping()
        self._ping_source = GLib.timeout_add_seconds(PING_EVERY, self._ping)

    def _ping(self):
        proc = self._proc
        if proc is None or not self.online:
            self._ping_source = 0
            return False

        def answered(result, error):
            if error == "timeout" and proc is self._proc:
                self._stderr.append("the phone does not answer")
                proc.force_exit()               # a dead line: reconnect

        self.request("ping", {}, answered, timeout=PING_TIMEOUT)
        return True

    def _stop_ping(self):
        if self._ping_source:
            GLib.source_remove(self._ping_source)
            self._ping_source = 0

    def _fail_pending(self, reason):
        pending, self._pending = self._pending, {}
        for cb, source in pending.values():
            GLib.source_remove(source)
            cb(None, reason)

    def _on_exit(self, proc, res):
        try:
            proc.wait_finish(res)
        except GLib.Error:
            pass
        if proc is not self._proc:
            return
        self._proc = None
        self._stdin = None
        if self._queue is not None:
            self._queue.put(None)
            self._queue = None
        self._stop_ping()
        self._fail_pending("connection lost")
        reason = self._stderr[-1] if self._stderr else None
        if reason is None and proc.get_if_exited() and proc.get_exit_status() != 0:
            reason = "ssh exited with %d" % proc.get_exit_status()
        if self.state != "online" and any("Permission denied" in l for l in self._stderr):
            # the key is not taken - a password is needed (or was wrong):
            # no retries until there is one, they would only be refused
            wrong = self.password is not None
            self.password = None
            self.needs_password = True
            self._set_state("offline", "login refused")
            self.emit("auth-needed", wrong)
            return
        self._set_state("offline", reason or "connection closed")
        self._schedule_retry()

    def set_password(self, password):
        """Log in with this password from now on (None: the key only)."""
        self.password = password
        self.needs_password = False
        self.reconnect()

    # -- reading ----------------------------------------------------------
    def _on_line(self, stream, res, proc):
        try:
            # the text variant: at the end it says None (the bytes one b"",
            # the same as an empty line)
            line, _len = stream.read_line_finish_utf8(res)
        except GLib.Error as e:
            if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED) \
                    and proc is self._proc:
                proc.force_exit()           # unreadable: rather anew than deaf
            return
        if line is None:
            return
        stream.read_line_async(GLib.PRIORITY_DEFAULT, self._cancel, self._on_line, proc)
        if proc is self._proc:
            self._handle(line)

    def _on_err(self, stream, res, proc):
        try:
            line, _len = stream.read_line_finish_utf8(res)
        except GLib.Error:
            return
        if line is None:
            return
        line = line.strip()
        if line:
            self._stderr = (self._stderr + [line])[-20:]
        stream.read_line_async(GLib.PRIORITY_DEFAULT, self._cancel, self._on_err, proc)

    def _handle(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if "event" in msg:
            if msg["event"] == "status":
                self.status = msg["data"]
                self.emit("changed")
            elif msg["event"] == "sms":
                self.emit("sms", msg.get("new") or [])
            elif msg["event"] == "calls":
                self.emit("calls", msg.get("calls") or [])
            elif msg["event"] == "voicebox":
                self.emit("voicebox")
            elif msg["event"] == "media":
                self.emit("media", msg.get("players") or [])
            elif msg["event"] == "notification":
                self.emit("notification", msg)
            elif msg["event"] == "notification-closed":
                self.emit("notification-closed", int(msg.get("id") or 0))
            elif msg["event"] == "clipboard":
                self.emit("clipboard", str(msg.get("text") or ""))
            return
        if not isinstance(msg, dict):
            return
        entry = self._pending.pop(msg.get("id"), None)
        if entry is not None:
            cb, source = entry
            GLib.source_remove(source)
            if msg.get("ok"):
                cb(msg.get("result"), None)
            else:
                cb(None, msg.get("error") or "failed")

    # -- requests ---------------------------------------------------------
    def request(self, cmd, args=None, callback=None, timeout=None):
        """callback(result, error) - exactly one of them is None; error is
        "timeout" when no answer comes in time (TIMEOUTS)."""
        callback = callback or (lambda r, e: None)
        if self._stdin is None or self._queue is None:
            GLib.idle_add(lambda: callback(None, "not connected") and False)
            return
        rid = self._next_id
        self._next_id += 1
        seconds = timeout or TIMEOUTS.get(cmd, TIMEOUT)
        source = GLib.timeout_add_seconds(seconds, self._expire, rid)
        self._pending[rid] = (callback, source)
        data = json.dumps({"id": rid, "cmd": cmd, "args": args or {}}) + "\n"
        self._queue.put(data.encode("utf-8"))

    def _expire(self, rid):
        entry = self._pending.pop(rid, None)
        if entry is not None:
            entry[0](None, "timeout")
        return False


def _writer(stream, q):
    """Writes what is queued to the pipe, in order; None ends it and closes
    the pipe (the agent then sees the end of its input)."""
    fd = stream.get_fd()
    while True:
        data = q.get()
        if data is None:
            break
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        except OSError:
            break                       # the other side is gone; _on_exit follows
    try:
        stream.close(None)
    except GLib.Error:
        pass
