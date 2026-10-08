# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Runs on the phone, sent over ssh as the agent is (nothing is installed
there): the phone's screen as an H.264 byte stream on stdout - and taps,
swipes and keys from the PC on stdin, one per line. It ends when stdin
closes - the PC went or closed the page.

The picture comes from wf-recorder (wlr-screencopy, x264), in FLV: every
picture there says how long it is, so the PC shows it at once - raw H.264
is only known to have ended when the next picture begins, and wf-recorder
only sends a picture when something changed on the screen (a still
screen costs nothing). While the screen is off there is nothing to copy:
the recorder is stopped. A second recorder would begin a second FLV
stream, which the PC cannot take in the middle of the first - this ends
with "screen: again" instead, and the PC connects anew.

Touch goes through a virtual touchscreen (uinput - the phone's user may
write it, group "system"), so phoc takes it for a finger: Phosh's swipes
from the edges work as well. Keys go through a virtual keyboard of their
own; they are typed with the phone's keyboard layout.

While this runs the screen does not go dark by itself (an idle inhibitor
at the session manager).

Turning the screen (portrait, landscape) goes through wlr-randr - Phosh
follows it. The picture changes its size then: a new stream ("again").

Lines on stdin, coordinates 0..10000 over the screen as shown:
  down X Y | move X Y | up | swipe X Y DX DY MS | key CODE | press CODE
  | release CODE | rotate normal|90|180|270|toggle (portrait <-> landscape)
On stderr: "screen: on", "screen: turned T" (how the screen is turned),
"screen: off", "screen: again", "screen: error ...".

PARAMS comes before this code: width (of the picture sent: the shorter
side), crf; in the tests "recorder" (an argv instead of wf-recorder),
"enabled" (an argv whose output stands for wlr-randr's), "rotate" (an argv
the transform is added to), "input_log" (a file the input goes to instead
of uinput) and "inhibit" (False: none)."""

import os
import signal
import subprocess
import sys
import threading
import time

PARAMS = globals().get("PARAMS", {})
RANGE = 10000
KEYS = range(1, 249)                # every key a keyboard has, power and volume too
STEP = 0.012                        # s between the points of a swipe
TRANSFORMS = ("normal", "90", "180", "270")


def session_env():
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    run = env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    env["XDG_RUNTIME_DIR"] = run
    if "WAYLAND_DISPLAY" not in env:
        for n in range(10):
            if os.path.exists(os.path.join(run, "wayland-%d" % n)):
                env["WAYLAND_DISPLAY"] = "wayland-%d" % n
                break
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=%s/bus" % run)
    return env


def say(line):
    sys.stderr.write("screen: %s\n" % line)
    sys.stderr.flush()


def recorder_argv(p):
    if p.get("recorder"):
        return list(p["recorder"])
    width = int(p.get("width", 540)) // 2 * 2
    return ["wf-recorder", "-y", "-m", "flv", "-f", "/dev/stdout", "-c", "libx264",
            "-x", "yuv420p", "-p", "preset=ultrafast", "-p", "tune=zerolatency",
            "-p", "crf=%d" % int(p.get("crf", 26)), "-p", "g=120",
            # the phone's pictures come upright, as the panel is built; wf-recorder
            # turns them after this filter - so in landscape too "width" stays
            # the shorter side
            "-F", "scale=%d:-2" % width]


def output(p, env):
    """The phone's screen as wlr-randr tells it: (name, on, transform)."""
    argv = p.get("enabled") or ["wlr-randr"]
    try:
        out = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, True, "normal"     # cannot tell: try the recorder
    name, on, transform = None, True, "normal"
    for line in out.splitlines():
        key, _colon, value = line.strip().partition(":")
        if key == "Enabled":
            on = value.strip() != "no"
        elif key == "Transform":
            transform = value.strip()
        elif line and not line[0].isspace() and name is None:
            name = line.split()[0]
    return name, on, transform


def screen_on(p, env):
    return output(p, env)[1]


# -- touch and keys --------------------------------------------------------------
class Uinput:
    """A virtual touchscreen and keyboard."""

    def __init__(self):
        from evdev import AbsInfo, UInput, ecodes as e
        self.e = e
        axis = AbsInfo(0, 0, RANGE, 0, 0, 0)
        self.touch = UInput({
            e.EV_KEY: [e.BTN_TOUCH],
            e.EV_ABS: [(e.ABS_X, axis), (e.ABS_Y, axis),
                       (e.ABS_MT_SLOT, AbsInfo(0, 0, 9, 0, 0, 0)),
                       (e.ABS_MT_POSITION_X, axis), (e.ABS_MT_POSITION_Y, axis),
                       (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0))]},
            name="PhoneBridge touch", input_props=[e.INPUT_PROP_DIRECT])
        self.keys = UInput({e.EV_KEY: list(KEYS)}, name="PhoneBridge keyboard")
        self.tracking = 0
        time.sleep(0.3)             # phoc has to see them before the first event

    def _at(self, x, y):
        e = self.e
        for code in (e.ABS_MT_POSITION_X, e.ABS_X):
            self.touch.write(e.EV_ABS, code, x)
        for code in (e.ABS_MT_POSITION_Y, e.ABS_Y):
            self.touch.write(e.EV_ABS, code, y)

    def down(self, x, y):
        e = self.e
        self.tracking = (self.tracking + 1) % 65536
        self.touch.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
        self.touch.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, self.tracking)
        self._at(x, y)
        self.touch.write(e.EV_KEY, e.BTN_TOUCH, 1)
        self.touch.syn()

    def move(self, x, y):
        self._at(x, y)
        self.touch.syn()

    def up(self):
        e = self.e
        self.touch.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        self.touch.write(e.EV_KEY, e.BTN_TOUCH, 0)
        self.touch.syn()

    def key(self, code, value):
        self.keys.write(self.e.EV_KEY, code, value)
        self.keys.syn()

    def close(self):
        for dev in (self.touch, self.keys):
            try:
                dev.close()
            except OSError:
                pass


class InputLog:
    """The tests' stand-in for uinput: what would be done, as lines."""

    def __init__(self, path):
        self.f = open(path, "a", buffering=1, encoding="utf-8")

    def down(self, x, y):
        self.f.write("down %d %d\n" % (x, y))

    def move(self, x, y):
        self.f.write("move %d %d\n" % (x, y))

    def up(self):
        self.f.write("up\n")

    def key(self, code, value):
        self.f.write("key %d %d\n" % (code, value))

    def close(self):
        self.f.close()


def clamp(v):
    return max(0, min(RANGE, int(v)))


def handle(dev, line, rotate=None):
    """One line from the PC. Wrong lines are dropped: never a crash."""
    words = line.split()
    if not words:
        return
    try:
        if words[0] == "rotate":
            if rotate is not None and words[1:2] and words[1] in TRANSFORMS + ("toggle",):
                rotate(words[1])
            return
        if dev is None:
            return
        cmd, args = words[0], [int(float(w)) for w in words[1:]]
        if cmd == "down":
            dev.down(clamp(args[0]), clamp(args[1]))
        elif cmd == "move":
            dev.move(clamp(args[0]), clamp(args[1]))
        elif cmd == "up":
            dev.up()
        elif cmd == "swipe":
            x, y, dx, dy, ms = args[:5]
            steps = max(2, min(60, round(ms / 1000 / STEP)))
            dev.down(clamp(x), clamp(y))
            for i in range(1, steps + 1):
                time.sleep(STEP)
                dev.move(clamp(x + dx * i / steps), clamp(y + dy * i / steps))
            time.sleep(STEP)
            dev.up()
        elif cmd in ("key", "press", "release") and args[0] in KEYS:
            if cmd != "release":
                dev.key(args[0], 1)
            if cmd != "press":
                dev.key(args[0], 0)
    except (IndexError, ValueError, OSError):
        pass


# -- the picture -----------------------------------------------------------------
class Picture:
    """The recorder, while the screen is on - once (see above)."""

    def __init__(self, params, env):
        self.params, self.env = params, env
        self.proc = None
        self.ending = False
        self.state = None
        self.started = False
        self.turned = False             # the recorder ended for a turn: no error

    def tell(self, state):
        if state != self.state:
            self.state = state
            say(state)

    def run(self):
        while not self.ending:
            name, on, transform = output(self.params, self.env)
            if not on:
                self.tell("off")
                time.sleep(1)
                continue
            if self.started:
                self.tell("again")
                return
            self.started = True
            try:
                self.proc = subprocess.Popen(
                    recorder_argv(self.params), env=self.env, stdin=subprocess.DEVNULL,
                    stdout=sys.stdout.fileno(), stderr=subprocess.PIPE)
            except OSError as e:
                self.tell("error %s" % e)
                return
            self.tell("on")
            say("turned %s" % transform)
            threading.Thread(target=self._watch, args=(self.proc,), daemon=True).start()
            last = b""
            for line in self.proc.stderr:
                if line.strip():
                    last = line.strip()
            code = self.proc.wait()
            self.proc = None
            if self.ending:
                return
            if self.turned:
                continue                    # "again", at once
            if code != 0 and screen_on(self.params, self.env):
                self.tell("error %s" % last.decode("utf-8", "replace")[-200:])
                time.sleep(2)
            elif self.params.get("recorder") and code == 0:
                return              # the tests' recorder: a few pictures, then the end

    def _watch(self, proc):
        """The screen went dark: the recorder waits forever for a picture."""
        while proc.poll() is None and not self.ending:
            time.sleep(1)
            if proc.poll() is None and not screen_on(self.params, self.env):
                self.tell("off")
                proc.terminate()

    def rotate(self, transform):
        """Turns the screen; the recorder ends, so the PC connects anew.
        "toggle" goes by how the screen is turned now - not by what the PC
        last heard, which may be on its way still."""
        name, _on, now = output(self.params, self.env)
        if transform == "toggle":
            transform = "normal" if now in ("90", "270") else "90"
        argv = (list(self.params["rotate"]) if self.params.get("rotate") else
                ["wlr-randr", "--output", name or "", "--transform"])
        try:
            p = subprocess.run(argv + [transform], env=self.env, capture_output=True,
                               text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            say("error %s" % e)
            return
        if p.returncode != 0:
            say("error %s" % ((p.stderr or "").strip()[-200:] or "wlr-randr failed"))
            return
        self.turned = True
        self._end_recorder()

    def stop(self):
        self.ending = True
        self._end_recorder()

    def _end_recorder(self):
        proc = self.proc
        if proc is not None and proc.poll() is None:
            # wf-recorder ends cleanly on SIGINT - but only with the next
            # picture, which a still screen never sends
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(0.3)
            except subprocess.TimeoutExpired:
                proc.kill()


def inhibit(env):
    """Keeps the screen from going dark while the PC shows it (as long as
    this process lives: the session manager drops it with the connection)."""
    try:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = env["DBUS_SESSION_BUS_ADDRESS"]
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync("org.gnome.SessionManager", "/org/gnome/SessionManager",
                      "org.gnome.SessionManager", "Inhibit",
                      GLib.Variant("(susu)", ("io.github.miscde.PhoneBridge", 0,
                                              "The screen is shown on the PC", 8)),
                      None, Gio.DBusCallFlags.NONE, 5000, None)
        return bus
    except Exception:       # noqa: BLE001 - no session manager: the screen may go dark
        return None


def main():
    env = session_env()
    try:
        dev = (InputLog(PARAMS["input_log"]) if PARAMS.get("input_log") else Uinput())
    except Exception as e:  # noqa: BLE001 - no uinput: the picture alone
        dev = None
        say("error no touch: %s" % e)
    keep = inhibit(env) if PARAMS.get("inhibit", True) else None
    picture = Picture(PARAMS, env)

    def end(*args):
        picture.stop()
        if dev is not None:
            dev.close()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)         # the reading thread may still wait on stdin

    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, end)
    def show():
        picture.run()
        if picture.state == "again":
            end()

    threading.Thread(target=show, daemon=True).start()
    for line in sys.stdin:
        handle(dev, line, picture.rotate)
    del keep
    end()


main()
