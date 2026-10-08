# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Runs on the phone, sent over ssh as the agent is (nothing is installed
there): the camera, as an H.264 byte stream on stdout (no container: it
would only add delay). It ends when
stdin closes - the PC went or switched it off.

PARAMS comes before this code: camera (0 back, 1 front), width, height,
kbps; "source" and "encoder" replace the camera and x264 in the tests.

The camera comes straight from Android (droidcamsrc) - FuriOS's V4L2
devices (droidcam2v4l2) deliver no frames - and is encoded with x264: the
phone's hardware encoder (v4l2h264enc) puts out nothing."""

import os
import signal
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

PARAMS = globals().get("PARAMS", {})


def pipeline(p):
    source = p.get("source") or "droidcamsrc camera-device=%d mode=2" % int(p.get("camera", 0))
    encoder = p.get("encoder") or (
        "x264enc tune=zerolatency speed-preset=ultrafast bitrate=%d key-int-max=30"
        % int(p.get("kbps", 2500)))
    return ("%s ! video/x-raw,width=%d,height=%d ! queue leaky=downstream max-size-buffers=1 ! "
            "videoconvert ! video/x-raw,format=I420 ! %s ! h264parse config-interval=-1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! fdsink fd=1 sync=false"
            % (source, int(p.get("width", 1280)), int(p.get("height", 720)), encoder))


def main():
    Gst.init(None)
    try:
        pipe = Gst.parse_launch(pipeline(PARAMS))
    except GLib.Error as e:
        sys.stderr.write("camera: %s\n" % e.message)
        sys.stderr.flush()
        os._exit(1)
    loop = GLib.MainLoop()

    def watch_stdin():
        while sys.stdin.buffer.read(4096):
            pass
        GLib.idle_add(loop.quit)

    def on_message(bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            sys.stderr.write("camera: %s\n" % msg.parse_error()[0].message)
            sys.stderr.flush()
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            loop.quit()

    bus = pipe.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)
    threading.Thread(target=watch_stdin, daemon=True).start()
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda: loop.quit() or False)
    pipe.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        pipe.set_state(Gst.State.NULL)       # the camera free again at once
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)         # no clean-up after GStreamer's threads: it can crash there


main()
