# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The phone's screen on the PC, live - and taps, swipes and keys back.

The phone sends H.264 in FLV (screen_phone.py, over an ssh of its own),
only when something changed on its screen; here it is decoded on slices
(as the webcam is, see webcam.py) and handed to the page as pictures.
What the page sends back goes as lines on the same ssh's stdin."""

import os
import subprocess
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst  # noqa: E402

from .connection import BOOTSTRAP, ssh_argv  # noqa: E402
from .webcam import decoder  # noqa: E402

PHONE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen_phone.py")
QUALITIES = {"fast": 360, "normal": 540, "sharp": 720}     # width of the picture sent
RANGE = 10000                   # the phone's coordinates: 0..RANGE over the screen


class Mirror(GObject.Object):
    """One stream, phone to PC. start() once; "state" tells "on", "off"
    (the phone's screen is dark) or "error ..."; "stopped" carries why it
    ended (None when stop() was asked for, "again" when the phone wants a
    new connection for a new stream)."""

    __gsignals__ = {"stopped": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
                    "state": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, device, quality="normal", picture=None, phone_params=None):
        super().__init__()
        self.info, self.password = dict(device.info), device.password
        self.params = dict(width=QUALITIES.get(quality, QUALITIES["normal"]))
        self.params.update(phone_params or {})
        self.picture = picture          # picture(rgba bytes, width, height)
        self.frames = 0
        self.size = None                # (width, height) of the last picture
        self.state = None
        self.ssh = None
        self.pipe = None
        self.running = False
        self._pending = False           # a picture waits for the main loop
        self._lock = threading.Lock()
        self._errors = []

    def start(self):
        code = ("PARAMS = %r\n" % self.params).encode() + open(PHONE_SCRIPT, "rb").read()
        env = None
        if self.password is not None:
            from .secrets import ssh_env
            env = ssh_env(self.password)
        try:
            self.ssh = subprocess.Popen(
                ssh_argv(self.info, BOOTSTRAP, low_delay=True,
                         password=self.password is not None),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            self.ssh.stdin.write(b"%d\n" % len(code) + code)
            self.ssh.stdin.flush()          # stays open: it carries the taps
        except OSError as e:
            self.stop(str(e))
            return False
        desc = ("fdsrc fd=%d ! flvdemux ! h264parse ! %s ! videoconvert ! video/x-raw,format=RGBA ! "
                "appsink name=out emit-signals=true drop=true max-buffers=1 sync=false"
                % (self.ssh.stdout.fileno(), decoder()))
        try:
            self.pipe = Gst.parse_launch(desc)
        except GLib.Error as e:
            self.stop(e.message)
            return False
        self.pipe.get_by_name("out").connect("new-sample", self._on_sample)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        self.pipe.set_state(Gst.State.PLAYING)
        self.running = True
        threading.Thread(target=self._read_errors, daemon=True).start()
        return True

    # -- what the page sends ---------------------------------------------------------
    def send(self, line):
        """One line to the phone (see screen_phone.py). False when it is gone."""
        ssh = self.ssh
        if ssh is None:
            return False
        try:
            with self._lock:
                ssh.stdin.write(line.encode("ascii") + b"\n")
                ssh.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    def down(self, x, y):
        return self.send("down %d %d" % (x, y))

    def move(self, x, y):
        return self.send("move %d %d" % (x, y))

    def up(self):
        return self.send("up")

    def swipe(self, x, y, dx, dy, ms=250):
        return self.send("swipe %d %d %d %d %d" % (x, y, dx, dy, ms))

    def key(self, code, how="key"):
        """how: "key" (press and release), "press" or "release"."""
        return self.send("%s %d" % (how, code))

    # -- the picture -----------------------------------------------------------------
    def _on_sample(self, sink):
        """A picture (in GStreamer's thread). Only the newest goes to the
        page: one waiting in the main loop is enough."""
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        self.frames += 1
        if self._pending:
            return Gst.FlowReturn.OK
        s = sample.get_caps().get_structure(0)
        width, height = s.get_value("width"), s.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(info.data)
        buf.unmap(info)
        self._pending = True
        GLib.idle_add(self._hand_on, data, width, height)
        return Gst.FlowReturn.OK

    def _hand_on(self, data, width, height):
        self._pending = False
        self.size = (width, height)
        if self.running and self.picture is not None:
            self.picture(data, width, height)
        return False

    def _read_errors(self):
        """The phone's word on its screen ("screen: on/off/error ..."), and
        the end of the ssh - a dark screen sends no picture to end on."""
        ssh = self.ssh
        for line in ssh.stderr:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("screen: "):
                GLib.idle_add(self._set_state, ssh, line[8:])
            elif line.startswith("ssh:") or "Permission denied" in line:
                self._errors = (self._errors + [line])[-5:]
        ssh.wait()
        GLib.timeout_add(500, lambda: self._ssh_ended(ssh) and False)

    def _set_state(self, ssh, state):
        if self.ssh is ssh:
            if state.startswith("error "):
                self._errors = (self._errors + [state[6:]])[-5:]
            if state == "again":
                self.stop("again")
                return False
            self.state = state
            self.emit("state", state)
        return False

    def _ssh_ended(self, ssh):
        if self.running and self.ssh is ssh:
            self.stop(self._errors[-1] if self._errors else "the phone ended the connection")

    def _on_message(self, bus, msg):
        """The stream broke or ended: the phone's side ended, most likely -
        why ("again" ...) comes on stderr, which may still be on its way.
        A moment's wait for it; at EOS the end of the ssh tells anyway."""
        if msg.type == Gst.MessageType.ERROR:
            message, pipe = msg.parse_error()[0].message, self.pipe
            GLib.timeout_add(700, lambda: self.pipe is pipe and self.stop(
                self._errors[-1] if self._errors else message) and False)

    def stop(self, reason=None):
        if self.pipe is not None:
            self.pipe.get_bus().remove_signal_watch()
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None
        if self.ssh is not None:
            ssh, self.ssh = self.ssh, None
            try:
                with self._lock:
                    ssh.stdin.close()       # the phone stops its recorder
            except OSError:
                pass
            threading.Thread(target=end_ssh, args=(ssh,), daemon=True).start()
        was = self.running
        self.running = False
        if was or reason is not None:
            self.emit("stopped", reason)


def end_ssh(ssh):
    """Waits for the phone's side to end - not in the main loop: the page
    goes at once."""
    try:
        ssh.wait(3)
    except subprocess.TimeoutExpired:
        ssh.kill()
        ssh.wait()


def to_phone(x, y, area, picture, clamp=False):
    """A point in the widget (x, y) to the phone's 0..RANGE - the picture
    of size picture=(w, h) shown in area=(W, H), as large as fits and
    centred. None when outside the picture - or, with clamp, its edge
    (a finger dragged out of the picture stays at its edge)."""
    (aw, ah), (pw, ph) = area, picture
    if pw <= 0 or ph <= 0 or aw <= 0 or ah <= 0:
        return None
    scale = min(aw / pw, ah / ph)
    dw, dh = pw * scale, ph * scale
    fx, fy = (x - (aw - dw) / 2) / dw, (y - (ah - dh) / 2) / dh
    if clamp:
        fx, fy = min(1.0, max(0.0, fx)), min(1.0, max(0.0, fy))
    elif not (0 <= fx <= 1 and 0 <= fy <= 1):
        return None
    return round(fx * RANGE), round(fy * RANGE)


Gst.init(None)
