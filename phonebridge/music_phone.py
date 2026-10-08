# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Runs on the phone, sent over ssh as the agent is (nothing is installed
there): the music of the phone's players on stdout, raw s16 stereo, for the
PC's speakers. It ends when stdin closes - the PC went or switched back.

pw-record itself is the way out: started as an Audio/Sink it is a sink of
its own ("PhoneBridge"), and whatever plays into it comes out on stdout.
The streams of the MPRIS players are sent there by their target.object
(pactl move-sink-input does nothing on FuriOS); ringing, notifications and
calls stay on the phone. When pw-record ends - this script, or the
connection, gone - the sink is gone, and PipeWire plays the streams on the
phone's speaker again: nothing is left behind.

PARAMS comes before this code: rate, channels, latency."""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

PARAMS = globals().get("PARAMS", {})
SINK = "phonebridge_music"
STREAM = "Stream/Output/Audio"
MPRIS = "org.mpris.MediaPlayer2."


def record_command(p):
    return ["pw-record", "--raw", "--rate", str(p.get("rate", 48000)),
            "--channels", str(p.get("channels", 2)), "--format", "s16",
            "--latency", p.get("latency", "100ms"),
            "-P", "{ media.class=Audio/Sink node.name=%s node.virtual=true "
                  "node.description=\"PhoneBridge\" }" % SINK, "-"]


def players():
    """The MPRIS players on the session bus: what tells their streams -
    process ids (the owner's, and the one in the name: a Flatpak's owner is
    its D-Bus proxy) and names, in small letters."""
    pids, names = set(), set()
    try:
        out = subprocess.run(["busctl", "--user", "list", "--no-legend", "--no-pager"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return pids, names
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith(MPRIS):
            continue
        if parts[1].isdigit():
            pids.add(int(parts[1]))
        for word in parts[0][len(MPRIS):].split("."):
            digits = re.fullmatch(r"instance_?(\d+)(?:_\d+)?", word)
            if digits:
                pids.add(int(digits.group(1)))
            elif word:
                names.add(word.lower())
    return pids, names


def streams():
    """The audio streams that play: [(node id, its properties)]."""
    try:
        objects = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True,
                                            timeout=5).stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    found = []
    for o in objects:
        props = ((o.get("info") or {}).get("props") or {}) if isinstance(o, dict) else {}
        if o.get("type", "").endswith(":Node") and props.get("media.class") == STREAM:
            found.append((o["id"], props))
    return found


def is_player(props, pids, names):
    try:
        if int(props.get("application.process.id", -1)) in pids:
            return True
    except (TypeError, ValueError):
        pass
    for key in ("application.name", "application.process.binary", "node.name",
                "application.id", "pipewire.access.portal.app_id"):
        value = str(props.get(key) or "").lower()
        if value and (value in names or value.rsplit(".", 1)[-1] in names):
            return True
    return False


class Router:
    """Sends the players' streams to our sink - each one once."""

    def __init__(self):
        self.sent = set()
        self.lock = threading.Lock()

    def route(self):
        with self.lock:
            pids, names = players()
            for node, props in streams():
                if node in self.sent or not is_player(props, pids, names):
                    continue
                if props.get("node.name") == SINK:
                    continue
                if set_target(node, SINK):
                    self.sent.add(node)
                    sys.stderr.write("routed: %s\n" % (props.get("application.name")
                                                       or props.get("node.name") or node))
                    sys.stderr.flush()

    def give_back(self):
        """The streams to the phone again, before our sink goes - so that
        the session manager does not keep our sink as their target."""
        with self.lock:
            for node in self.sent:
                set_target(node, None)
            self.sent.clear()


def set_target(node, target):
    argv = (["pw-metadata", str(node), "target.object", target] if target
            else ["pw-metadata", "-d", str(node), "target.object"])
    try:
        return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def watch(router, stop):
    """New streams (the next track can be one): routed when they come, and
    once more a moment later - the player's name may come after its stream."""
    try:
        sub = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True)
    except OSError:
        sub = None
    if sub is None:                 # without pactl: now and then
        while not stop.wait(3):
            router.route()
        return
    stop.callback = sub.terminate
    for line in sub.stdout:
        if stop.is_set():
            break
        if "'new' on sink-input" in line:
            router.route()
            threading.Timer(1.5, router.route).start()


class Stop(threading.Event):
    callback = None

    def set(self):
        super().set()
        if self.callback is not None:
            try:
                self.callback()
            except OSError:
                pass


def main():
    os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    stop = Stop()
    try:
        rec = subprocess.Popen(record_command(PARAMS), stdin=subprocess.DEVNULL)
    except OSError as e:
        sys.stderr.write("music: %s\n" % e)
        sys.stderr.flush()
        os._exit(1)
    router = Router()

    def end(*_a):
        stop.set()

    def watch_stdin():
        while sys.stdin.buffer.read(4096):
            pass
        end()

    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, end)
    threading.Thread(target=watch_stdin, daemon=True).start()
    time.sleep(0.3)                 # the sink there before the first stream goes to it
    router.route()
    threading.Thread(target=watch, args=(router, stop), daemon=True).start()
    while not stop.wait(0.5):
        if rec.poll() is not None:
            sys.stderr.write("music: pw-record ended (%d)\n" % rec.returncode)
            break
    stop.set()
    router.give_back()
    if rec.poll() is None:
        rec.terminate()
        try:
            rec.wait(timeout=2)
        except subprocess.TimeoutExpired:
            rec.kill()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":     # sent over ssh; imported only by the tests
    main()
