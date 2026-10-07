# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The call's sound on the PC: the caller from the PC's speakers, the PC's
microphone to the caller.

A second SSH connection carries raw audio both ways - 16 kHz mono 16 bit,
32 KB/s each way:

  PC microphone --pw-record--> PhoneBridge --ssh stdin--> pw-play  -> droid-call-sink
  PC speakers   <--pw-play---- PhoneBridge <-ssh stdout-- pw-record <- droid-call-source

droid-call-sink/-source come from the patched spa-droid plugin (the same
nodes VoiceBox uses); the agent mutes the phone's own microphone in the
modem meanwhile (callaudio.mute). PhoneBridge sits in the middle so it can
raise the caller's voice - the call source is quiet, about -23 dBFS at its
peaks - and show both levels.

Echo cancellation: with speakers instead of a headset the caller would hear
themselves; PipeWire's echo-cancel module (loaded for the call, unloaded
after) takes the speakers out of the microphone.

The test mode uses the phone's ordinary speaker and microphone (droid-sink,
droid-source) instead - to try the way without a call."""

import array
import os
import shlex
import shutil
import subprocess
import sys
import threading

from gi.repository import GLib, GObject

from .connection import ssh_argv

RATE = 16000
CHUNK = RATE // 50 * 2          # 20 ms of s16 mono
FORMAT = ["--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16"]
EC_SOURCE = "phonebridge_ec_source"
EC_SINK = "phonebridge_ec_sink"


def remote_command(sink, source, latency="40ms"):
    """What runs on the phone: play stdin into `sink`, record `source` to
    stdout. The player gets stdin explicitly - a background job of a
    non-interactive shell would otherwise read /dev/null - and goes when
    the recorder does (the connection closed)."""
    fmt = " ".join(FORMAT)
    return ("export XDG_RUNTIME_DIR=/run/user/$(id -u); exec 3<&0; "
            "pw-play %s --latency %s --target %s - <&3 & p=$!; "
            "trap 'kill $p 2>/dev/null' EXIT INT TERM HUP; "
            "pw-record %s --latency %s --target %s -"
            % (fmt, latency, shlex.quote(sink), fmt, latency, shlex.quote(source)))


def amplify(data, gain):
    """s16 samples times gain, clipped; and the peak (0..1) afterwards."""
    samples = array.array("h")
    samples.frombytes(data[:len(data) // 2 * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    if gain != 1.0:
        samples = array.array("h", (32767 if v > 32767 else -32768 if v < -32768 else v
                                    for v in (int(x * gain) for x in samples)))
    peak = max((abs(x) for x in samples), default=0) / 32768
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes(), peak


def load_echo_cancel():
    """Loads PipeWire's echo canceller between the default microphone and
    speakers; returns the module id, or None."""
    try:
        out = subprocess.run(
            ["pactl", "load-module", "module-echo-cancel", "aec_method=webrtc",
             "source_name=" + EC_SOURCE, "sink_name=" + EC_SINK,
             "source_properties=device.description=PhoneBridge-Mikrofon",
             "sink_properties=device.description=PhoneBridge-Lautsprecher"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip().isdigit() else None


def unload_leftovers():
    """Unloads echo cancellers of ours that a crash left loaded."""
    try:
        out = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) > 2 and parts[1] == "module-echo-cancel" and EC_SOURCE in parts[2]:
            unload_module(parts[0])


def unload_module(module_id):
    if module_id:
        try:
            subprocess.run(["pactl", "unload-module", module_id],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


class CallAudio(GObject.Object):
    """One stream; start() once, stop() any number of times. "stopped"
    carries the reason (None when stop() was asked for)."""

    __gsignals__ = {"stopped": (GObject.SignalFlags.RUN_FIRST, None, (object,))}

    def __init__(self, device_info, gain=2.0, echo_cancel=True, test=False, password=None):
        super().__init__()
        self.info = device_info
        self.password = password
        self.gain = gain
        self.echo_cancel = echo_cancel
        self.test = test
        self.level_in = 0.0       # the caller, after the gain
        self.level_out = 0.0      # the PC's microphone
        self.running = False
        self._procs = []
        self._module = None
        self._stderr = []
        self._lock = threading.Lock()

    def start(self):
        sink, source = (("droid-sink", "droid-source") if self.test
                        else ("droid-call-sink", "droid-call-source"))
        mic_target, speaker_target = [], []
        if self.echo_cancel:
            self._module = load_echo_cancel()
            if self._module:
                mic_target = ["--target", EC_SOURCE]
                speaker_target = ["--target", EC_SINK]
        try:
            self.mic = subprocess.Popen(
                ["pw-record"] + FORMAT + ["--latency", "20ms"] + mic_target + ["-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.speaker = subprocess.Popen(
                ["pw-play"] + FORMAT + ["--latency", "40ms"] + speaker_target + ["-"],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            env = None
            if self.password is not None:
                from .secrets import ssh_env
                env = ssh_env(self.password)
            self.ssh = subprocess.Popen(
                ssh_argv(self.info, remote_command(sink, source), low_delay=True,
                         password=self.password is not None),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        except OSError as e:
            self._procs = [p for p in (getattr(self, n, None) for n in ("mic", "speaker", "ssh"))
                           if p is not None]
            self.stop(str(e))
            return False
        self._procs = [self.mic, self.speaker, self.ssh]
        self.running = True
        for target in (self._uplink, self._downlink, self._errors):
            threading.Thread(target=target, daemon=True).start()
        return True

    def _uplink(self):
        try:
            while self.running:
                data = self.mic.stdout.read1(CHUNK)
                if not data:
                    break
                _d, self.level_out = amplify(data, 1.0)
                self.ssh.stdin.write(data)
                self.ssh.stdin.flush()
        except (OSError, ValueError):
            pass
        self._ended("microphone")

    def _downlink(self):
        try:
            while self.running:
                data = self.ssh.stdout.read1(CHUNK)
                if not data:
                    break
                data, self.level_in = amplify(data, self.gain)
                self.speaker.stdin.write(data)
                self.speaker.stdin.flush()
        except (OSError, ValueError):
            pass
        self._ended("phone")

    def _errors(self):
        for line in self.ssh.stderr:
            line = line.decode("utf-8", "replace").strip()
            if line:
                self._stderr = (self._stderr + [line])[-10:]

    def _ended(self, which):
        if self.running:
            reason = self._stderr[-1] if self._stderr else "%s stream ended" % which
            GLib.idle_add(lambda: self.stop(reason) and False)

    def stop(self, reason=None):
        with self._lock:
            was = self.running or self._procs or self._module
            self.running = False
            procs, self._procs = self._procs, []
            module, self._module = self._module, None
        for p in procs:
            for stream in (p.stdin, p.stdout):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
            try:
                p.terminate()
                p.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    p.kill()
                except OSError:
                    pass
        unload_module(module)
        self.level_in = self.level_out = 0.0
        if was:
            self.emit("stopped", reason)


def available():
    """pw-record and pw-play on the PC."""
    return all(shutil.which(prog) for prog in ("pw-record", "pw-play"))
