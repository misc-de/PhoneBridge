# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The phone's screen on the PC. The phone's side runs here through the
stand-in ssh: a test picture from gst-launch instead of wf-recorder, and
what would go to uinput is written to a file."""

import os
import runpy
import shutil
import tempfile
import time
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, Gst, Gtk  # noqa: E402

from phonebridge import config, screen  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
Gst.init(None)
HAVE_GST = bool(shutil.which("gst-launch-1.0")) and all(
    Gst.ElementFactory.find(e) for e in ("videotestsrc", "x264enc", "h264parse", "flvmux", "flvdemux"))
RECORDER = ["gst-launch-1.0", "-q", "videotestsrc", "is-live=true", "num-buffers=%d", "!",
            "video/x-raw,width=180,height=400", "!", "x264enc", "tune=zerolatency", "!",
            "h264parse", "!", "flvmux", "streamable=true", "!", "fdsink", "fd=1"]


def recorder(frames=20):
    return [w % frames if "%d" in w else w for w in RECORDER]


def phone_side():
    src = open(screen.PHONE_SCRIPT).read().replace("\nmain()\n", "\n")
    return runpy._run_code(compile(src, "screen_phone", "exec"), {"PARAMS": {}})


class Pieces(unittest.TestCase):
    def test_to_phone(self):
        # a 100x200 picture in a 400x200 area: 100 wide, centred (150..250)
        self.assertEqual(screen.to_phone(150, 0, (400, 200), (100, 200)), (0, 0))
        self.assertEqual(screen.to_phone(200, 100, (400, 200), (100, 200)), (5000, 5000))
        self.assertEqual(screen.to_phone(250, 200, (400, 200), (100, 200)), (10000, 10000))
        self.assertIsNone(screen.to_phone(100, 100, (400, 200), (100, 200)))
        self.assertEqual(screen.to_phone(100, 300, (400, 200), (100, 200), clamp=True),
                         (0, 10000))
        self.assertIsNone(screen.to_phone(1, 1, (0, 0), (100, 200)))

    def test_recorder(self):
        ns = phone_side()
        argv = ns["recorder_argv"]({"width": 541})
        self.assertEqual(argv[0], "wf-recorder")
        self.assertIn("scale=540:-2", argv)              # even, for yuv420p
        self.assertIn("tune=zerolatency", argv)
        self.assertEqual(ns["recorder_argv"]({"recorder": ["x"]}), ["x"])

    def test_output(self):
        ns = phone_side()
        randr = ('HWCOMPOSER-1 "Unknown"\n  Make: hwcomposer\n  Enabled: yes\n  Modes:\n'
                 '    720x1600 px, 90.000000 Hz (current)\n  Transform: 90\n  Scale: 1.5\n')
        self.assertEqual(ns["output"]({"enabled": ["printf", randr]}, None),
                         ("HWCOMPOSER-1", True, "90"))

    def test_turned_back_to_the_panel(self):
        ns = phone_side()
        panel = ns["panel"]
        self.assertEqual(panel(1000, 2000, "normal"), (1000, 2000))
        # landscape ("90", measured): the top edge shown is the panel's left one
        self.assertEqual(panel(5000, 0, "90"), (0, 5000))
        self.assertEqual(panel(10000, 5000, "90"), (5000, 0))
        self.assertEqual(panel(1000, 2000, "180"), (9000, 8000))
        self.assertEqual(panel(1000, 2000, "270"), (8000, 1000))
        self.assertEqual(panel(-5, 20000, "normal"), (0, 10000))
        log = tempfile.NamedTemporaryFile(delete=False)
        log.close()
        self.addCleanup(os.unlink, log.name)
        dev = ns["InputLog"](log.name)
        with mock.patch.object(time, "sleep", lambda s: None):
            for line in ("down 5000 0", "move 5000 6000", "up", "swipe 5000 0 0 6000 24"):
                ns["handle"](dev, line, None, "90")
        dev.close()
        self.assertEqual(open(log.name).read().splitlines(),
                         ["down 0 5000", "move 6000 5000", "up",
                          "down 0 5000", "move 3000 5000", "move 6000 5000", "up"])

    def test_screen_on(self):
        ns = phone_side()
        self.assertTrue(ns["screen_on"]({"enabled": ["echo", "  Enabled: yes"]}, None))
        self.assertFalse(ns["screen_on"]({"enabled": ["echo", "  Enabled: no"]}, None))
        self.assertTrue(ns["screen_on"]({"enabled": ["/nonexistent"]}, None))

    def test_lines_to_touch_and_keys(self):
        ns = phone_side()
        log = tempfile.NamedTemporaryFile(delete=False)
        log.close()
        self.addCleanup(os.unlink, log.name)
        dev = ns["InputLog"](log.name)
        with mock.patch.object(time, "sleep", lambda s: None):
            for line in ("down 100 200", "move 20000 -5", "up", "swipe 5000 9000 0 -4000 36",
                         "key 30", "press 42", "release 42", "key 999", "down x", "nonsense",
                         "", "up 1"):
                ns["handle"](dev, line)
            turned = []
            for line in ("rotate 90", "rotate sideways", "rotate", "rotate toggle"):
                ns["handle"](dev, line, turned.append)
            ns["handle"](None, "rotate normal", turned.append)
            ns["handle"](None, "down 1 1")               # no touch: nothing, no crash
        dev.close()
        lines = open(log.name).read().splitlines()
        self.assertEqual(lines[:3], ["down 100 200", "move 10000 0", "up"])
        swipe = lines[3:lines.index("up", 3) + 1]
        self.assertEqual(swipe[0], "down 5000 9000")
        self.assertEqual(swipe[-2:], ["move 5000 5000", "up"])
        self.assertEqual(len(swipe), 5)                  # 36 ms: down, three points, up
        rest = lines[3 + len(swipe):]
        self.assertEqual(rest, ["key 30 1", "key 30 0", "key 42 1", "key 42 0", "up"])
        self.assertEqual(turned, ["90", "toggle", "normal"])


@unittest.skipUnless(HAVE_GST, "no gst-launch or GStreamer elements for the test picture")
class Streaming(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.log = os.path.join(d, "input")

    def mirror(self, **params):
        p = dict(recorder=recorder(), enabled=["echo", "Enabled: yes"], input_log=self.log,
                 inhibit=False)
        p.update(params)
        dev = mock.Mock(info={"id": "t", "host": "phone", "user": "me"}, password=None)
        pictures = []
        m = screen.Mirror(dev, "fast", picture=lambda d, w, h: pictures.append((len(d), w, h)),
                          phone_params=p)
        self.addCleanup(m.stop)
        return m, pictures

    def test_pictures_and_taps(self):
        m, pictures = self.mirror()
        states, stopped = [], []
        m.connect("state", lambda _m, s: states.append(s))
        m.connect("stopped", lambda _m, r: stopped.append(r))
        self.assertEqual(m.params["width"], 360)
        self.assertTrue(m.start())
        self.assertTrue(run_loop_until(lambda: pictures, 30))
        self.assertEqual(pictures[-1], (180 * 400 * 4, 180, 400))
        self.assertEqual(states[0], "on")
        self.assertTrue(m.down(10, 20) and m.move(30, 40) and m.up() and m.key(28))
        self.assertTrue(run_loop_until(
            lambda: os.path.exists(self.log) and "key 28 0" in open(self.log).read(), 10))
        self.assertEqual(open(self.log).read().splitlines(),
                         ["down 10 20", "move 30 40", "up", "key 28 1", "key 28 0"])
        ssh = m.ssh
        m.stop()
        self.assertEqual(stopped, [None])
        self.assertTrue(run_loop_until(lambda: ssh.poll() is not None, 5))  # the phone side ended
        self.assertFalse(m.up())

    def test_a_dark_screen(self):
        m, pictures = self.mirror(enabled=["echo", "Enabled: no"])
        states = []
        m.connect("state", lambda _m, s: states.append(s))
        m.start()
        self.assertTrue(run_loop_until(lambda: states, 20))
        self.assertEqual(states, ["off"])
        self.assertEqual(pictures, [])

    def test_a_second_stream_is_a_new_connection(self):
        m, pictures = self.mirror(recorder=["sh", "-c", "echo broken >&2; exit 3"])
        states, stopped = [], []
        m.connect("state", lambda _m, s: states.append(s))
        m.connect("stopped", lambda _m, r: stopped.append(r))
        m.start()
        self.assertTrue(run_loop_until(lambda: stopped, 20))
        self.assertEqual(states, ["on", "turned normal", "error broken"])
        self.assertEqual(stopped, ["again"])
        self.assertIsNone(m.ssh)

    def test_turning_is_a_new_stream(self):
        turned = os.path.join(os.path.dirname(self.log), "turned")
        m, pictures = self.mirror(recorder=recorder(100000),
                                  rotate=["sh", "-c", 'echo "$0" > ' + turned])
        states, stopped = [], []
        m.connect("state", lambda _m, s: states.append(s))
        m.connect("stopped", lambda _m, r: stopped.append(r))
        m.start()
        self.assertTrue(run_loop_until(lambda: pictures, 30))
        self.assertEqual(m.transform, "normal")
        self.assertEqual(states[:2], ["on", "turned normal"])
        self.assertTrue(m.rotate("toggle"))
        self.assertTrue(run_loop_until(lambda: stopped, 20))
        self.assertEqual(stopped, ["again"])
        self.assertEqual(open(turned).read().strip(), "90")
        self.assertFalse([s for s in states if s.startswith("error")])

    def test_the_phone_goes(self):
        m, pictures = self.mirror(recorder=["sh", "-c", "kill -TERM $PPID"])
        stopped = []
        m.connect("stopped", lambda _m, r: stopped.append(r))
        m.start()
        self.assertTrue(run_loop_until(lambda: stopped, 20))
        self.assertTrue(stopped[0])                      # said why
        self.assertIsNone(m.ssh)


@unittest.skipUnless(HAVE_DISPLAY and HAVE_GST and Gtk.init_check(), "no display")
class InTheApp(unittest.TestCase):
    def test_page(self):
        from phonebridge.app import PhoneBridgeApp
        from phonebridge.screen_page import KEY_POWER, ScreenPage
        home = Home()
        self.addCleanup(home.cleanup)
        log = os.path.join(home.dir, "input")
        for p in (mock.patch.dict(os.environ, home.env()),
                  mock.patch.object(config, "CONFIG_DIR", home.config)):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestScreen")
        app.send_notification = lambda *a: None
        app.tell = app.toast = lambda *a: None
        app._screen_extra = {"phone_params": dict(
            recorder=recorder(200), enabled=["echo", "Enabled: yes"], input_log=log,
            inhibit=False)}
        app.register(None)
        self.addCleanup(app.do_shutdown)
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online, 20))
        page = ScreenPage(app)
        mapped = [False]
        page.get_mapped = lambda: mapped[0]
        page.set_device(dev)
        self.assertIsNone(page.mirror)                   # not shown: no picture
        mapped[0] = True
        page.load()
        self.assertIsNotNone(page.mirror)
        self.assertTrue(run_loop_until(lambda: page.picture.get_paintable() is not None, 30))
        self.assertEqual(page.view.get_visible_child_name(), "picture")
        page.key(KEY_POWER)
        first = page.mirror
        page.quality.set_selected(2)                     # sharper: again
        self.assertEqual(app.cfg["screen"]["quality"], "sharp")
        self.assertIsNot(page.mirror, first)
        self.assertEqual(page.mirror.params["width"], 720)
        # full screen: only the picture; Esc goes back
        window = mock.Mock(fullscreened=False)
        window.is_fullscreen = lambda: window.fullscreened
        window.fullscreen = lambda: setattr(window, "fullscreened", True)
        window.unfullscreen = lambda: setattr(window, "fullscreened", False)
        page._window = window
        page.set_fullscreen(True)
        self.assertTrue(window.fullscreened)
        self.assertFalse(page.bar.get_visible())
        window.fullscreened = False                     # the window manager has not said so yet
        self.assertTrue(page.is_fullscreen())
        window.set_chrome.assert_called_with(False)
        self.assertFalse(page._on_escape(None, Gdk.KEY_a, 38, 0))      # a key for the phone
        self.assertTrue(page._on_escape(None, Gdk.KEY_Escape, 9, 0))
        self.assertFalse(window.fullscreened)
        self.assertTrue(page.bar.get_visible())
        window.set_chrome.assert_called_with(True)
        self.assertFalse(page._on_escape(None, Gdk.KEY_Escape, 9, 0))  # not full screen: the phone's
        page._window = None

        page._pressed.add(30)
        page.stop()                                      # the page goes: keys up, picture off
        self.assertIsNone(page.mirror)
        self.assertEqual(page._pressed, set())
        self.assertTrue(run_loop_until(
            lambda: os.path.exists(log) and "key 30 0" in open(log).read(), 10))
        self.assertIn("key 116 1", open(log).read())
