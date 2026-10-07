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
import shlex

from gi.repository import Gio, GLib, GObject

AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.py")
BOOTSTRAP = ("python3 -u -c 'import sys;n=int(sys.stdin.buffer.readline());"
             "exec(compile(sys.stdin.buffer.read(n),\"phonebridge-agent\",\"exec\"))'")
BACKOFF = (3, 5, 10, 30, 60)


def ssh_argv(device):
    ssh = shlex.split(os.environ.get("PHONEBRIDGE_SSH", "ssh"))
    return ssh + [
        "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-p", str(device.get("port") or 22),
        "%s@%s" % (device["user"], device["host"]), BOOTSTRAP]


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
    }

    def __init__(self, info):
        super().__init__()
        self.info = dict(info)
        self.state = "offline"
        self.error = None
        self.status = None
        self.hello = None
        self._proc = None
        self._cancel = None
        self._stdin = None
        self._next_id = 1
        self._pending = {}
        self._stderr = []
        self._outq = []
        self._writing = False
        self._retry = 0
        self._retry_source = 0
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
        proc, self._proc, self._stdin = self._proc, None, None
        if proc is not None:
            self._cancel.cancel()
            proc.force_exit()
        pending, self._pending = self._pending, {}
        for cb in pending.values():
            cb(None, "not connected")
        self._set_state("offline")

    def reconnect(self):
        self._clear_retry()
        self._retry = 0
        if self._proc is None:
            self._running = True
            self._connect()

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
            proc = Gio.Subprocess.new(
                ssh_argv(self.info),
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
                | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as e:
            self._set_state("offline", e.message)
            self._schedule_retry()
            return
        self._proc = proc
        self._set_state("connecting")
        self._stdin = proc.get_stdin_pipe()
        self._outq = []
        self._writing = False
        # asynchronously: the agent is larger than a pipe holds, and ssh
        # reads it only once it is connected
        self._write(agent_payload())
        out = Gio.DataInputStream.new(proc.get_stdout_pipe())
        err = Gio.DataInputStream.new(proc.get_stderr_pipe())
        out.read_line_async(GLib.PRIORITY_DEFAULT, self._cancel, self._on_line, proc)
        err.read_line_async(GLib.PRIORITY_DEFAULT, self._cancel, self._on_err, proc)
        proc.wait_async(None, self._on_exit)
        self.request("hello", {}, self._on_hello)

    def _schedule_retry(self):
        if not self._running:
            return
        delay = BACKOFF[min(self._retry, len(BACKOFF) - 1)]
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
            return
        self.hello = result
        self._retry = 0
        self._set_state("online")

    def _on_exit(self, proc, res):
        try:
            proc.wait_finish(res)
        except GLib.Error:
            pass
        if proc is not self._proc:
            return
        self._proc = None
        self._stdin = None
        pending, self._pending = self._pending, {}
        for cb in pending.values():
            cb(None, "connection lost")
        reason = self._stderr[-1] if self._stderr else None
        if reason is None and proc.get_if_exited() and proc.get_exit_status() != 0:
            reason = "ssh exited with %d" % proc.get_exit_status()
        self._set_state("offline", reason or "connection closed")
        self._schedule_retry()

    # -- reading ----------------------------------------------------------
    def _on_line(self, stream, res, proc):
        try:
            line, _len = stream.read_line_finish_utf8(res)
        except GLib.Error:
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
            return
        cb = self._pending.pop(msg.get("id"), None)
        if cb is not None:
            if msg.get("ok"):
                cb(msg.get("result"), None)
            else:
                cb(None, msg.get("error") or "failed")

    # -- requests ---------------------------------------------------------
    def request(self, cmd, args=None, callback=None):
        """callback(result, error) - exactly one of them is None."""
        callback = callback or (lambda r, e: None)
        if self._stdin is None:
            GLib.idle_add(lambda: callback(None, "not connected") and False)
            return
        rid = self._next_id
        self._next_id += 1
        self._pending[rid] = callback
        data = json.dumps({"id": rid, "cmd": cmd, "args": args or {}}) + "\n"
        self._write(data.encode("utf-8"))

    def _write(self, data):
        self._outq.append(data)
        if not self._writing:
            self._write_next()

    def _write_next(self):
        if not self._outq or self._stdin is None:
            self._writing = False
            return
        self._writing = True
        stream = self._stdin
        stream.write_all_async(self._outq.pop(0), GLib.PRIORITY_DEFAULT, self._cancel,
                               self._written, None)

    def _written(self, stream, res, _data):
        try:
            stream.write_all_finish(res)
        except GLib.Error:
            # the connection is going away; _on_exit fails what is pending
            self._outq = []
            self._writing = False
            return
        if stream is self._stdin:
            self._write_next()
        else:
            self._writing = False
