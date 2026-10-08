# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The phone's music on the PC: the players' streams sent to PhoneBridge's
sink and back (stand-in pw-dump and pw-metadata), the stream through the
stand-in ssh to the PC's player, the backlog that never grows, and the
switch in the window."""

import json
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, Gtk  # noqa: E402

from phonebridge import config, music, music_phone  # noqa: E402

from .fake_mpris import FakePlayer  # noqa: E402
from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def session():
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception:  # noqa: BLE001
        return None


def node(node_id, **props):
    props.setdefault("media.class", "Stream/Output/Audio")
    return {"id": node_id, "type": "PipeWire:Interface:Node", "info": {"props": props}}


class Pieces(unittest.TestCase):
    def test_backlog_drops_the_oldest(self):
        b = music.Backlog()
        b.put(b"\x01\x00\x02\x00" * (music.MAX_BEHIND // 4))
        self.assertEqual(b.dropped, 0)
        b.put(b"\x03\x00\x04\x00" * 10)
        # too much waiting: down to KEEP, whole frames, the newest kept
        self.assertEqual(b.size, music.KEEP)
        self.assertEqual(b.size % music.FRAME, 0)
        data = b.get()
        self.assertTrue(data.endswith(b"\x03\x00\x04\x00" * 10))
        self.assertEqual(b.size, 0)
        # whole frames only - the rest waits for the next bytes
        b.put(b"\x05" * 6)
        self.assertEqual(b.get(), b"\x05" * 4)
        self.assertEqual(b.size, 2)
        b.close()
        self.assertEqual(b.get(at_least=music.PREBUFFER), b"")

    def test_backlog_gathers_before_it_gives(self):
        import threading
        b = music.Backlog()
        got = []
        t = threading.Thread(target=lambda: got.append(b.get(at_least=8)))
        t.start()
        b.put(b"\x01" * 4)
        t.join(0.2)
        self.assertEqual(got, [])           # not enough yet
        b.put(b"\x02" * 4)
        t.join(2)
        self.assertEqual(got, [b"\x01" * 4 + b"\x02" * 4])

    def test_pipe_fill(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        self.assertEqual(music.pipe_fill(w), 0)
        os.write(w, b"x" * 1000)
        self.assertEqual(music.pipe_fill(w), 1000)

    def test_links_and_metadata(self):
        objects = [node(77), node(9, **{"media.class": "Audio/Sink", "node.name": "ours"}),
                   {"id": 100, "type": "PipeWire:Interface:Link",
                    "info": {"output-node-id": 77, "input-node-id": 9}},
                   {"id": 101, "type": "PipeWire:Interface:Link",
                    "info": {"output-node-id": 78, "input-node-id": 5}}]
        self.assertEqual(music_phone.linked_to(objects, "ours"), {77})
        self.assertEqual(music_phone.linked_to(objects, "droid-sink"), set())
        out = ("update: id:0 key:'default.audio.sink' value:'{\"name\":\"droid-sink\"}' "
               "type:'Spa:String:JSON'\nupdate: id:77 key:'target.object' value:'ours' "
               "type:'(null)'\n")
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout=out)):
            self.assertEqual(music_phone.metadata(), ({77: "ours"}, "droid-sink"))

    def test_which_stream_is_a_player(self):
        pids, names = {1802922}, {"emilia", "firefox"}
        self.assertTrue(music_phone.is_player({"application.process.id": "1802922"},
                                              pids, names))
        self.assertTrue(music_phone.is_player({"application.name": "Firefox"}, pids, names))
        self.assertTrue(music_phone.is_player({"application.id": "org.example.Emilia"},
                                              pids, names))
        # ringing and the like stay on the phone
        self.assertFalse(music_phone.is_player({"application.name": "feedbackd",
                                                "application.process.id": "7"}, pids, names))
        self.assertFalse(music_phone.is_player({"application.process.id": "x"}, set(), set()))

    def test_record_command_is_a_sink(self):
        cmd = music_phone.record_command({})
        self.assertEqual(cmd[0], "pw-record")
        self.assertIn("media.class=Audio/Sink", " ".join(cmd))
        self.assertIn("node.name=%s" % music_phone.SINK, " ".join(cmd))
        self.assertTrue(music_phone.SINK.startswith("phonebridge_music_"))   # one per run


@unittest.skipUnless(session() is not None, "no session bus")
class Stream(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phonebridge-music-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.dict(os.environ, {"FAKE_AUDIO_DIR": self.dir})
        p.start()
        self.addCleanup(p.stop)
        self.player = FakePlayer("tunes")
        self.addCleanup(self.player.close)
        with open(os.path.join(self.dir, "pw-dump.json"), "w") as f:
            json.dump([node(77, **{"application.name": "Tunes", "node.name": "tunes"}),
                       node(78, **{"application.name": "feedbackd"}),
                       node(5, **{"media.class": "Audio/Sink", "node.name": "droid-sink"})], f)
        self.out = os.path.join(self.dir, "played")
        dev = mock.Mock(info={"id": "t", "host": "phone", "user": "me"}, password=None)
        dev.name = "Testphone"
        self.stream = music.MusicOnPC(dev, play=["sh", "-c", "cat > " + self.out])
        self.stopped = []
        self.stream.connect("stopped", lambda s, why: self.stopped.append(why))
        self.addCleanup(self.stream.stop)

    def metadata(self):
        try:
            with open(os.path.join(self.dir, "pw-metadata.log")) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def test_the_player_comes_to_the_pc_and_goes_back(self):
        self.assertTrue(self.stream.start())
        self.assertTrue(run_loop_until(lambda: os.path.exists(self.out)
                                       and os.path.getsize(self.out) > 20000, 15))
        # only the player's stream went to PhoneBridge's sink (one of this run's own)
        self.assertEqual(len(self.metadata()), 1)
        self.assertRegex(self.metadata()[0], r"^77 target\.object phonebridge_music_\d+$")
        self.assertEqual(self.stream.routed, ["Tunes"])
        with open(self.out, "rb") as f:
            data = f.read()
        self.assertEqual(set(struct.unpack("<%dh" % (len(data) // 2), data[:len(data) // 2 * 2])),
                         {2000})
        with open(os.path.join(self.dir, "record-default")) as f:
            self.assertIn("media.class=Audio/Sink", f.read())
        self.stream.stop()
        self.assertEqual(self.stopped, [None])
        # switched back: the stream to the phone's speaker before the sink goes
        # (else WirePlumber pauses the player), then without a target again
        self.assertTrue(run_loop_until(lambda: len(self.metadata()) == 3, 10))
        self.assertEqual(self.metadata()[1:], ["77 target.object droid-sink",
                                               "-d 77 target.object"])
        self.assertTrue(run_loop_until(lambda: not music.ending(), 10))

    def test_a_phone_that_cannot_be_reached(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_FAIL": "1"}):
            self.assertTrue(self.stream.start())
            self.assertTrue(run_loop_until(lambda: self.stopped, 10))
        self.assertIn("No route to host", self.stopped[0])
        self.assertFalse(self.stream.running)


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class TheSwitch(unittest.TestCase):
    def test_switch_starts_and_stops_the_stream(self):
        from phonebridge.app import PhoneBridgeApp
        from phonebridge.window import MainWindow
        home = Home()
        self.addCleanup(home.cleanup)
        audio = tempfile.mkdtemp(prefix="phonebridge-music-")
        self.addCleanup(shutil.rmtree, audio, True)
        for p in (mock.patch.dict(os.environ, dict(home.env(), FAKE_AUDIO_DIR=audio)),
                  mock.patch.object(config, "CONFIG_DIR", home.config),
                  mock.patch.object(PhoneBridgeApp, "_music_extra",
                                    {"play": ["sh", "-c", "cat > /dev/null"]})):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        player = FakePlayer("switch")
        self.addCleanup(player.close)
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestMusic")
        app.send_notification = lambda *a: None
        app.register(None)
        self.addCleanup(app.do_shutdown)
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online and app.player("test"), 20))
        app.window = win = MainWindow(app)
        self.addCleanup(win.destroy)
        win.overview.set_device(dev)
        self.assertTrue(win.overview.on_phone.get_active())
        self.assertNotIn("test", app.music_pc)

        win.overview.on_pc.set_active(True)
        self.assertIn("test", app.music_pc)
        self.assertEqual(config.load()["music_on_pc"], ["test"])
        self.assertIn("Play on the phone", [i.get("label") for i in app.menu_items()])

        win.overview.on_phone.set_active(True)
        self.assertNotIn("test", app.music_pc)
        self.assertEqual(config.load()["music_on_pc"], [])

        # chosen once, it comes back with the player
        playing = app.player("test")
        app.set_music_on_pc(dev, True)
        app._on_media(dev, [])
        self.assertNotIn("test", app.music_pc)
        app._on_media(dev, [playing])
        # once the last stream has handed the player back on the phone
        self.assertTrue(run_loop_until(lambda: "test" in app.music_pc, 10))
