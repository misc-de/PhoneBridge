# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The phone as webcam. The phone's side runs here through the stand-in
ssh with a test picture instead of the camera; the PC's side counts the
pictures instead of handing them to v4l2loopback or PipeWire. pkexec is
never run."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gst, Gtk  # noqa: E402

from phonebridge import config, webcam  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
Gst.init(None)
HAVE_GST = all(Gst.ElementFactory.find(e) for e in
               ("videotestsrc", "x264enc", "mpegtsmux", "tsdemux", "h264parse"))
TEST_PICTURE = {"source": "videotestsrc is-live=true num-buffers=45", "width": 320,
                "height": 240}
COUNT = "fakesink name=count signal-handoffs=true sync=false"


class Pieces(unittest.TestCase):
    def test_loopback_device(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for n, name in ((0, "Integrated Camera"), (9, webcam.LABEL), (10, "OBS Virtual Camera")):
            os.makedirs(os.path.join(d, "video%d" % n))
            with open(os.path.join(d, "video%d" % n, "name"), "w") as f:
                f.write(name + "\n")
        self.assertEqual(webcam.loopback_device(d), "/dev/video9")
        self.assertIsNone(webcam.loopback_device(os.path.join(d, "none")))

    def test_install_hints(self):
        def release(text):
            f = tempfile.NamedTemporaryFile("w", delete=False)
            f.write(text)
            f.close()
            self.addCleanup(os.unlink, f.name)
            return f.name
        with mock.patch("platform.release", lambda: "7.2.3-2-MANJARO"):
            self.assertEqual(webcam.install_hint(release("ID=manjaro\nID_LIKE=arch\n")),
                             "sudo pacman -S linux72-v4l2loopback")
        self.assertEqual(webcam.install_hint(release("ID=arch\n")),
                         "sudo pacman -S v4l2loopback-dkms")
        self.assertEqual(webcam.install_hint(release("ID=ubuntu\nID_LIKE=debian\n")),
                         "sudo apt install v4l2loopback-dkms")
        self.assertIsNone(webcam.install_hint(release("ID=nixos\n")))

    def test_setup_command(self):
        now = webcam.setup_command(False)
        self.assertIn("modprobe v4l2loopback", now)
        self.assertIn("v4l2loopback-ctl add", now)          # when it is loaded already
        self.assertIn('card_label="PhoneBridge camera"', now)
        self.assertNotIn("/etc/", now)
        always = webcam.setup_command(True)
        self.assertIn("/etc/modules-load.d/phonebridge-camera.conf", always)
        self.assertIn("/etc/modprobe.d/phonebridge-camera.conf", always)

    def test_sinks(self):
        self.assertIn("v4l2sink device=/dev/video9", webcam.sink_description("/dev/video9"))
        self.assertIn("pipewiresink", webcam.sink_description(None))

    def test_phone_pipeline(self):
        import runpy
        src = open(webcam.PHONE_SCRIPT).read().replace("\nmain()\n", "\n")
        ns = runpy._run_code(compile(src, "webcam_phone", "exec"), {"PARAMS": {}})
        desc = ns["pipeline"]({"camera": 1, "width": 1280, "height": 720, "kbps": 2500})
        self.assertTrue(desc.startswith("droidcamsrc camera-device=1 mode=2 ! video/x-raw,"
                                        "width=1280,height=720"))
        self.assertIn("x264enc tune=zerolatency speed-preset=ultrafast bitrate=2500", desc)
        self.assertTrue(desc.endswith("fdsink fd=1 sync=false"))


@unittest.skipUnless(HAVE_GST, "no GStreamer elements for the test picture")
class Streaming(unittest.TestCase):
    def dev(self):
        return mock.Mock(info={"id": "t", "host": "phone", "user": "me"}, password=None)

    def test_pictures_come_and_the_end_is_told(self):
        cam = webcam.Webcam(self.dev(), phone_params=TEST_PICTURE, sink=COUNT)
        stopped, frames = [], []
        cam.connect("stopped", lambda c, r: stopped.append(r))
        self.assertTrue(cam.start())
        cam.pipe.get_by_name("count").connect("handoff", lambda *a: frames.append(1))
        self.assertTrue(run_loop_until(lambda: stopped, 30))
        self.assertGreater(len(frames), 20)
        self.assertEqual(stopped, ["the phone ended the camera"])    # 45 pictures, then EOS
        self.assertIsNone(cam.ssh)

    def test_stop_frees_the_camera(self):
        cam = webcam.Webcam(self.dev(), phone_params=dict(TEST_PICTURE, source=(
            "videotestsrc is-live=true")), sink=COUNT, mirror=True)
        stopped, frames = [], []
        cam.connect("stopped", lambda c, r: stopped.append(r))
        cam.start()
        cam.pipe.get_by_name("count").connect("handoff", lambda *a: frames.append(1))
        self.assertTrue(run_loop_until(lambda: len(frames) > 5, 30))
        ssh = cam.ssh
        cam.stop()
        self.assertEqual(stopped, [None])
        self.assertIsNotNone(ssh.poll())                 # the phone's side has ended

    def test_a_phone_without_camera(self):
        cam = webcam.Webcam(self.dev(), phone_params=dict(TEST_PICTURE, source="nosuchsrc"),
                            sink=COUNT)
        stopped = []
        cam.connect("stopped", lambda c, r: stopped.append(r))
        cam.start()
        self.assertTrue(run_loop_until(lambda: stopped, 30))
        self.assertTrue(stopped[0])                      # said why


@unittest.skipUnless(HAVE_DISPLAY and HAVE_GST and Gtk.init_check(), "no display")
class InTheApp(unittest.TestCase):
    def test_switch_settings_and_setup(self):
        from phonebridge.app import PhoneBridgeApp
        from phonebridge.webcam_ui import WebcamGroup
        home = Home()
        self.addCleanup(home.cleanup)
        for p in (mock.patch.dict(os.environ, home.env()),
                  mock.patch.object(config, "CONFIG_DIR", home.config),
                  mock.patch.object(webcam, "loopback_device", lambda *a: None)):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestWebcam")
        app.send_notification = lambda *a: None
        told = []
        app.tell = told.append
        app.toast = told.append
        app._webcam_extra = {"phone_params": dict(TEST_PICTURE, source="videotestsrc is-live=true"),
                             "sink": COUNT}
        app.register(None)
        self.addCleanup(app.do_shutdown)
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online, 20))
        group = WebcamGroup(app)
        group.set_device(dev)
        self.assertFalse(group.switch.get_active())
        self.assertTrue(group.setup_button.get_visible())       # no loopback device here

        group.switch.set_active(True)
        self.assertIn("test", app.webcams)
        self.assertTrue(told[-1].startswith("Testphone is the webcam now"))
        self.assertIn("Webcam off", [i.get("label") for i in app.menu_items()])
        first = app.webcams["test"]
        group.camera.set_selected(0)                             # another camera: again
        self.assertIsNot(app.webcams["test"], first)
        self.assertEqual(app.cfg["webcam"]["camera"], 0)
        app.on_tray_item("webcam")                               # off from the panel
        self.assertNotIn("test", app.webcams)
        self.assertFalse(group.switch.get_active())

        dialogs, ran = [], []
        with mock.patch.object(Adw.AlertDialog, "present", lambda d, p=None: dialogs.append(d)), \
                mock.patch.object(webcam, "loopback_installed", lambda: False), \
                mock.patch.object(webcam, "install_hint", lambda: "sudo pacman -S x"):
            group.ask_setup()
        self.assertEqual(dialogs[-1].get_extra_child().get_label(), "sudo pacman -S x")
        with mock.patch.object(Adw.AlertDialog, "present", lambda d, p=None: dialogs.append(d)), \
                mock.patch.object(webcam, "loopback_installed", lambda: True), \
                mock.patch.object(webcam, "set_up",
                                  lambda persistent, done: ran.append(persistent) or done(None)):
            group.ask_setup()
            dialogs[-1].emit("response", "go")
        self.assertEqual(ran, [True])
        self.assertEqual(told[-1], "“PhoneBridge camera” is there now")


if __name__ == "__main__":
    unittest.main()
