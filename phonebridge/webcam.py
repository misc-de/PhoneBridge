# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The phone's camera as a webcam of the PC.

The phone sends H.264 (webcam_phone.py, over an ssh of its own); here it
is decoded and handed on, as fast as it comes - the decoder works on
slices, never on frames in parallel (that held a dozen pictures back,
1.6 s on an 8-core PC; now the way phone to picture takes ~50 ms):

  - to a v4l2loopback device named "PhoneBridge camera" - then every
    program sees it (browsers, Zoom, Teams, OBS ...). The device is made
    once, with root's rights asked for (pkexec), and on request at every
    start of the PC;
  - else to PipeWire as a camera node - programs that take PipeWire
    cameras see it (OBS, GNOME Snapshot, Firefox and Chromium with that
    switched on), without anything set up."""

import glob
import os
import platform
import re
import shlex
import subprocess
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst  # noqa: E402

from .connection import BOOTSTRAP, ssh_argv  # noqa: E402

LABEL = "PhoneBridge camera"
PHONE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webcam_phone.py")
QUALITIES = {"480p": (854, 480, 1200), "720p": (1280, 720, 2500), "1080p": (1920, 1080, 4500)}
PREVIEW = (640, 360)            # the test's picture: smaller, cheap to show
PREVIEW_EVERY = 0.06            # s between pictures handed to the window (~15 a second)
# the test's branch: what goes out, scaled down - never holding up the webcam
PREVIEW_BRANCH = ("t. ! queue leaky=downstream max-size-buffers=1 ! videoscale ! "
                  "video/x-raw,width=%d,height=%d,pixel-aspect-ratio=1/1 ! videoconvert ! "
                  "video/x-raw,format=RGBA ! appsink name=preview emit-signals=true "
                  "drop=true max-buffers=1 sync=false" % PREVIEW)
MODPROBE_OPTIONS = 'devices=1 exclusive_caps=1 card_label="%s"' % LABEL


# -- where the picture goes --------------------------------------------------------
def loopback_device(sysfs="/sys/class/video4linux"):
    """/dev/videoN of the "PhoneBridge camera" loopback device, or None."""
    for d in sorted(glob.glob(os.path.join(sysfs, "video*"))):
        try:
            with open(os.path.join(d, "name"), encoding="utf-8") as f:
                if f.read().strip() == LABEL:
                    return "/dev/" + os.path.basename(d)
        except OSError:
            continue
    return None


def loopback_installed():
    try:
        return subprocess.run(["modinfo", "-n", "v4l2loopback"], capture_output=True,
                              timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_hint(release="/etc/os-release"):
    """The command that installs v4l2loopback here - or None."""
    from . import deps
    d = deps.distro(release)
    try:
        with open(release, encoding="utf-8") as f:
            manjaro = "manjaro" in f.read().lower()
    except OSError:
        manjaro = False
    # the module is built by DKMS, for the running kernel's headers
    if manjaro:
        m = re.match(r"(\d+)\.(\d+)", platform.release())
        if m:
            return ("sudo pacman -S v4l2loopback-dkms v4l2loopback-utils linux%s%s-headers"
                    % m.groups())
    return {"arch": "sudo pacman -S v4l2loopback-dkms v4l2loopback-utils linux-headers",
            "debian": "sudo apt install v4l2loopback-dkms v4l2loopback-utils",
            "fedora": "sudo dnf install v4l2loopback   # RPM Fusion",
            "suse": "sudo zypper install v4l2loopback-kmp-default"}.get(d)


def setup_command(persistent):
    """What pkexec runs: the device now (a second one when the module is
    loaded already) - and, if asked, at every start of the PC."""
    now = ("if lsmod | grep -q '^v4l2loopback '; then "
           "v4l2loopback-ctl add -x 1 -n %s; else modprobe v4l2loopback %s; fi"
           % (shlex.quote(LABEL), MODPROBE_OPTIONS))
    if not persistent:
        return now
    return now + ("; echo v4l2loopback > /etc/modules-load.d/phonebridge-camera.conf"
                  "; echo %s > /etc/modprobe.d/phonebridge-camera.conf"
                  % shlex.quote("options v4l2loopback " + MODPROBE_OPTIONS))


def set_up(persistent, done):
    """Asks for root's rights (pkexec) and makes the device. done(error)."""
    def work():
        try:
            p = subprocess.run(["pkexec", "sh", "-c", setup_command(persistent)],
                               capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as e:
            error = str(e)
        else:
            error = None if p.returncode == 0 else (
                (p.stderr or p.stdout).strip().splitlines() or ["pkexec: %d" % p.returncode])[-1]
        GLib.idle_add(lambda: done(error) and False)

    threading.Thread(target=work, daemon=True).start()


def sink_description(device):
    if device:
        return "videoconvert ! video/x-raw,format=YUY2 ! v4l2sink device=%s sync=false" % device
    return "videoconvert ! pipewiresink name=pw mode=provide sync=false"


class Webcam(GObject.Object):
    """One stream, phone to PC. start() once; "stopped" carries why (None
    when stop() was asked for)."""

    __gsignals__ = {"stopped": (GObject.SignalFlags.RUN_FIRST, None, (object,))}

    def __init__(self, device, camera=0, quality="720p", mirror=False, phone_params=None,
                 sink=None, preview=None):
        super().__init__()
        self.info, self.password = dict(device.info), device.password
        width, height, kbps = QUALITIES.get(quality, QUALITIES["720p"])
        self.params = dict(camera=int(camera), width=width, height=height, kbps=kbps)
        self.params.update(phone_params or {})
        self.mirror = mirror
        self.preview = preview          # preview(rgba bytes, width, height) - the test
        self.frames = 0                 # pictures that went out
        self._shown = 0.0
        self.sink = sink                # a sink description of its own (the tests)
        self.target = None              # "/dev/videoN", or "pipewire"
        self.ssh = None
        self.pipe = None
        self.running = False
        self._stderr = []

    def start(self):
        device = None if self.sink else loopback_device()
        self.target = device or ("pipewire" if not self.sink else "test")
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
            self.ssh.stdin.flush()          # stays open: closing it ends the phone's side
        except OSError as e:
            self.stop(str(e))
            return False
        flip = " ! videoflip method=horizontal-flip" if self.mirror else ""
        desc = ("fdsrc fd=%d ! h264parse ! %s%s ! tee name=t ! "
                "queue leaky=downstream max-size-buffers=2 ! identity name=sent ! %s %s"
                % (self.ssh.stdout.fileno(), decoder(), flip,
                   self.sink or sink_description(device), PREVIEW_BRANCH))
        try:
            self.pipe = Gst.parse_launch(desc)
        except GLib.Error as e:
            self.stop(e.message)
            return False
        sent = self.pipe.get_by_name("sent")
        sent.set_property("signal-handoffs", True)
        sent.connect("handoff", lambda *a: setattr(self, "frames", self.frames + 1))
        self.pipe.get_by_name("preview").connect("new-sample", self._on_preview)
        pw = self.pipe.get_by_name("pw")
        if pw is not None:
            pw.set_property("stream-properties", Gst.Structure.new_from_string(
                'props,media.class=(string)Video/Source,media.role=(string)Camera,'
                'node.name=(string)phonebridge_camera,node.description=(string)"%s"' % LABEL))
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        self.pipe.set_state(Gst.State.PLAYING)
        self.running = True
        threading.Thread(target=self._errors, daemon=True).start()
        return True

    def _on_preview(self, sink):
        """A picture for the test window (in GStreamer's thread)."""
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        callback = self.preview
        now = time.monotonic()
        if callback is None or now - self._shown < PREVIEW_EVERY:
            return Gst.FlowReturn.OK
        self._shown = now
        s = sample.get_caps().get_structure(0)
        width, height = s.get_value("width"), s.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(info.data)
        buf.unmap(info)
        GLib.idle_add(lambda: self.preview is callback and callback(data, width, height) and False)
        return Gst.FlowReturn.OK

    def _errors(self):
        """The phone's own words ("camera: ..."); and its end - which a
        stream without a single picture never tells."""
        ssh = self.ssh
        for line in ssh.stderr:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("camera: "):
                self._stderr = (self._stderr + [line[8:]])[-5:]
            elif line.startswith("ssh:") or "Permission denied" in line:
                self._stderr = (self._stderr + [line])[-5:]
        ssh.wait()
        GLib.timeout_add(500, lambda: self._ssh_ended(ssh) and False)

    def _ssh_ended(self, ssh):
        if self.running and self.ssh is ssh:
            self.stop(self._stderr[-1] if self._stderr else "the phone ended the camera")

    def _on_message(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            self.stop(self._stderr[-1] if self._stderr else msg.parse_error()[0].message)
        elif msg.type == Gst.MessageType.EOS:      # the phone's side ended
            self.stop(self._stderr[-1] if self._stderr else "the phone ended the camera")

    def stop(self, reason=None):
        if self.pipe is not None:
            self.pipe.get_bus().remove_signal_watch()
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None
        if self.ssh is not None:
            ssh, self.ssh = self.ssh, None
            try:
                ssh.stdin.close()           # the phone frees the camera
            except OSError:
                pass
            try:
                ssh.wait(3)
            except subprocess.TimeoutExpired:
                ssh.kill()
        was = self.running
        self.running = False
        if was or reason is not None:
            self.emit("stopped", reason)


def decoder():
    """avdec_h264 (gst-libav) on slices - its default, frames in parallel,
    holds pictures back - else openh264dec."""
    if Gst.ElementFactory.find("avdec_h264"):
        return "avdec_h264 thread-type=slice max-threads=4"
    return "openh264dec"


Gst.init(None)
