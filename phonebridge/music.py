# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The phone's music on the PC's speakers.

A second SSH connection carries it - raw 48 kHz stereo 16 bit, 192 KB/s
while something plays, nothing while it is paused:

  player --target.object--> pw-record (a sink) --ssh stdout--> PhoneBridge --> pw-play

music_phone.py does the phone's side; ringing, notifications and calls stay
on the phone. Switched back - or the connection gone - the phone plays the
music itself again.

pw-play takes 100 ms at a time and stutters when they are not all there:
PhoneBridge keeps PREBUFFER ahead of it in the pipe - gathered before it
starts, and again after the Wi-Fi made it run dry. It reads the phone all
the time and lets the pipe run no further ahead than MAX_AHEAD: the phone's
clock and the PC's never run quite alike, and a phone kept waiting would
stall its whole sound."""

import collections
import fcntl
import os
import struct
import subprocess
import termios
import threading

from gi.repository import GLib, GObject

from .connection import BOOTSTRAP, ssh_argv

RATE = 48000
CHANNELS = 2
FRAME = CHANNELS * 2
SECOND = RATE * FRAME
MAX_BEHIND = SECOND // 2        # more waiting than this: the oldest goes
KEEP = SECOND // 5              # ... down to this
PREBUFFER = SECOND * 2 // 5     # ahead of pw-play: what the Wi-Fi may hold back
LOW = SECOND // 10              # less ahead than this: gather PREBUFFER again
MAX_AHEAD = SECOND              # further ahead (the clocks): the newest goes
PIPE_SIZE = 1 << 20
PHONE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music_phone.py")


def play_command(name):
    return ["pw-play", "--raw", "--rate", str(RATE), "--channels", str(CHANNELS),
            "--format", "s16", "--latency", "100ms",
            "-P", "{ media.role=Music application.name=PhoneBridge "
                  "node.name=phonebridge_phone_music media.name=\"%s\" }"
                  % name.replace('"', "'"), "-"]


class Backlog:
    """Bytes from the phone, waiting for pw-play; never more than
    MAX_BEHIND, whole frames only."""

    def __init__(self):
        self.chunks = collections.deque()
        self.size = 0
        self.dropped = 0
        self.closed = False
        self.cond = threading.Condition()

    def put(self, data):
        with self.cond:
            self.chunks.append(data)
            self.size += len(data)
            if self.size > MAX_BEHIND:
                self._drop(self.size - KEEP)
            self.cond.notify()

    def _drop(self, n):
        n -= n % FRAME
        while n > 0 and self.chunks:
            first = self.chunks[0]
            if len(first) <= n:
                self.chunks.popleft()
                cut = len(first)
            else:
                cut = n
                self.chunks[0] = first[cut:]
            self.size -= cut
            self.dropped += cut
            n -= cut

    def get(self, at_least=0):
        """All that waits in whole frames, once there are at least
        `at_least` bytes (or any); b"" once closed."""
        with self.cond:
            while not self.closed and (self.size < max(at_least, FRAME)):
                self.cond.wait()
            data = b"".join(self.chunks)
            self.chunks.clear()
            whole = len(data) - len(data) % FRAME
            if whole < len(data):
                self.chunks.append(data[whole:])
            self.size = len(data) - whole
            return data[:whole]

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify()


def pipe_fill(fd):
    """Bytes in the pipe that pw-play has not read yet."""
    try:
        return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
    except OSError:
        return 0


_ending = set()


def ending():
    """A stream still handing the phone's players back: the next waits."""
    return any(t.is_alive() for t in list(_ending))


def finish(procs):
    try:
        _finish(procs)
    finally:
        _ending.discard(threading.current_thread())


def _finish(procs):
    for p in procs:
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()


class MusicOnPC(GObject.Object):
    """One stream, phone to PC. start() once; "stopped" carries why (None
    when stop() was asked for)."""

    __gsignals__ = {"stopped": (GObject.SignalFlags.RUN_FIRST, None, (object,))}

    def __init__(self, device, phone_params=None, play=None):
        super().__init__()
        self.info, self.password = dict(device.info), device.password
        self.name = device.name
        self.params = {"rate": RATE, "channels": CHANNELS}
        self.params.update(phone_params or {})
        self.play = play                # a player command of its own (the tests)
        self.routed = []                # the apps the phone sent here
        self.running = False
        self.ssh = self.player = None
        self.backlog = Backlog()
        self._stderr = []
        self._lock = threading.Lock()

    def start(self):
        code = ("PARAMS = %r\n" % self.params).encode() + open(PHONE_SCRIPT, "rb").read()
        env = None
        if self.password is not None:
            from .secrets import ssh_env
            env = ssh_env(self.password)
        try:
            self.player = subprocess.Popen(self.play or play_command(self.name),
                                           stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.ssh = subprocess.Popen(
                ssh_argv(self.info, BOOTSTRAP, password=self.password is not None),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            self.ssh.stdin.write(b"%d\n" % len(code) + code)
            self.ssh.stdin.flush()          # stays open: closing it ends the phone's side
        except OSError as e:
            self.stop(str(e))
            return False
        self.running = True
        for target, proc in ((self._receive, self.ssh), (self._feed, self.player),
                             (self._errors, self.ssh)):
            threading.Thread(target=target, args=(proc,), daemon=True).start()
        return True

    def _receive(self, ssh):
        try:
            while True:
                data = ssh.stdout.read1(16384)
                if not data:
                    break
                self.backlog.put(data)
        except (OSError, ValueError):
            pass
        self.backlog.close()

    def _feed(self, player):
        fd = player.stdin.fileno()
        try:
            fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, PIPE_SIZE)
        except (OSError, AttributeError):
            pass
        try:
            while True:
                ahead = pipe_fill(fd)
                data = self.backlog.get(PREBUFFER if ahead < LOW else 0)
                if not data:
                    break
                if ahead > MAX_AHEAD:
                    self.backlog.dropped += len(data)
                    continue
                player.stdin.write(data)
                player.stdin.flush()
        except (OSError, ValueError):
            pass

    def _errors(self, ssh):
        for line in ssh.stderr:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("routed: "):
                self.routed.append(line[8:])
            elif line.startswith("music: "):
                self._stderr = (self._stderr + [line[7:]])[-5:]
            elif line.startswith("ssh:") or "Permission denied" in line or "Error" in line:
                self._stderr = (self._stderr + [line])[-5:]
        ssh.wait()
        GLib.timeout_add(300, lambda: self._ssh_ended(ssh) and False)

    def _ssh_ended(self, ssh):
        if self.running and self.ssh is ssh:
            self.stop(self._stderr[-1] if self._stderr else "the phone ended the music")

    def stop(self, reason=None):
        with self._lock:
            was = self.running or self.ssh is not None
            self.running = False
            ssh, player, self.ssh, self.player = self.ssh, self.player, None, None
        self.backlog.close()
        procs = [p for p in (ssh, player) if p is not None]
        for p in procs:
            try:
                p.stdin.close()     # the phone's side ends with its stdin
            except OSError:
                pass
        if procs:
            # the phone hands the streams back before it goes: a moment for
            # that, without holding up the window
            t = threading.Thread(target=finish, args=(procs,), daemon=True)
            _ending.add(t)
            t.start()
        if was:
            self.emit("stopped", reason)
