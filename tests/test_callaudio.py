# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The call's sound on the PC: the stream both ways (through the stand-in
ssh, pw-record and pw-play), the gain, echo cancellation, and the agent
muting the phone's microphone and handing it back."""

import os
import struct
import tempfile
import shutil
import unittest
from unittest import mock

from phonebridge import agent, callaudio

from .support import run_loop_until


def values(path):
    with open(path, "rb") as f:
        data = f.read()
    return set(struct.unpack("<%dh" % (len(data) // 2), data[:len(data) // 2 * 2]))


class Pieces(unittest.TestCase):
    def test_amplify_clips(self):
        data = struct.pack("<3h", 1000, -20000, 30000)
        out, peak = callaudio.amplify(data, 2.0)
        self.assertEqual(struct.unpack("<3h", out), (2000, -32768, 32767))
        self.assertAlmostEqual(peak, 32768 / 32768)
        same, peak = callaudio.amplify(data, 1.0)
        self.assertEqual(same, data)

    def test_remote_command(self):
        cmd = callaudio.remote_command("droid-call-sink", "droid-call-source")
        self.assertIn("pw-play --raw --rate 16000", cmd)
        self.assertIn("--target droid-call-sink - <&3 &", cmd)   # stdin given explicitly
        self.assertIn("--target droid-call-source -", cmd)
        self.assertIn("trap", cmd)


class Stream(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phonebridge-audio-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.dict(os.environ, {"FAKE_AUDIO_DIR": self.dir})
        p.start()
        self.addCleanup(p.stop)
        self.info = {"id": "t", "host": "phone", "user": "me"}

    def run_stream(self, **kw):
        audio = callaudio.CallAudio(self.info, **kw)
        stopped = []
        audio.connect("stopped", lambda a, why: stopped.append(why))
        self.assertTrue(audio.start())
        self.addCleanup(audio.stop)
        played = lambda name: os.path.exists(os.path.join(self.dir, name)) and \
            os.path.getsize(os.path.join(self.dir, name)) > 2000  # noqa: E731
        return audio, stopped, played

    def test_both_ways_with_gain_and_echo_cancel(self):
        audio, stopped, played = self.run_stream(gain=2.0, echo_cancel=True)
        ec_sink = "play-" + callaudio.EC_SINK
        self.assertTrue(run_loop_until(lambda: played("play-droid-call-sink")
                                       and played(ec_sink), 15))
        # the PC's microphone (the echo-cancelled one) reaches the call ...
        self.assertTrue(os.path.exists(os.path.join(self.dir,
                                                    "record-" + callaudio.EC_SOURCE)))
        self.assertEqual(values(os.path.join(self.dir, "play-droid-call-sink")), {2000})
        # ... and the caller comes out on the PC, twice as loud
        self.assertEqual(values(os.path.join(self.dir, ec_sink)), {2000})
        self.assertTrue(run_loop_until(lambda: audio.level_in > 0 and audio.level_out > 0, 5))
        audio.stop()
        self.assertEqual(stopped, [None])
        with open(os.path.join(self.dir, "pactl.log")) as f:
            log = f.read()
        self.assertIn("module-echo-cancel", log)
        self.assertIn("unload 42", log)
        self.assertFalse(audio.running)

    def test_test_mode_uses_the_phones_own_speaker_and_mic(self):
        audio, stopped, played = self.run_stream(echo_cancel=False, test=True, gain=1.0)
        self.assertTrue(run_loop_until(lambda: played("play-droid-sink")
                                       and played("play-default"), 15))
        self.assertEqual(values(os.path.join(self.dir, "play-default")), {1500})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "pactl.log")))

    def test_lost_connection_stops_with_a_reason(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_FAIL": "1"}):
            audio, stopped, played = self.run_stream(echo_cancel=False)
            self.assertTrue(run_loop_until(lambda: stopped, 10))
        self.assertIn("No route to host", stopped[0])
        self.assertFalse(audio.running)


class Mute(unittest.TestCase):
    """callaudio.mute against a stand-in modem."""

    def setUp(self):
        self.muted = False
        self.sets = []

        def fake_call(conn, name, path, iface, method, args=None, reply=None, timeout=5000):
            if method == "GetProperties":
                return ({"Muted": self.muted},)
            if method == "SetProperty":
                self.muted = bool(args.unpack()[1])
                self.sets.append(self.muted)
            return None

        p = mock.patch.object(agent, "call", fake_call)
        p.start()
        self.addCleanup(p.stop)
        self.agent = mock.Mock(spec=["modem", "system"])
        self.agent.modem = "/ril_0"
        self.agent.system = None

    def test_mute_guard_and_hand_back(self):
        self.assertTrue(agent.cmd_callaudio_mute(self.agent, {"on": True}))
        self.assertTrue(self.muted)
        self.muted = False                          # somebody unmutes
        self.assertTrue(agent.pc_audio_guard(self.agent))
        self.assertTrue(self.muted)
        self.assertFalse(agent.cmd_callaudio_mute(self.agent, {"on": False}))
        self.assertFalse(self.muted)                # as it was before
        self.assertFalse(agent.pc_audio_guard(self.agent))

    def test_was_muted_stays_muted(self):
        self.muted = True
        agent.cmd_callaudio_mute(self.agent, {"on": True})
        agent.pc_audio_release(self.agent)
        self.assertTrue(self.muted)

    def test_unconfirmed_mute_is_an_error(self):
        def stubborn(conn, name, path, iface, method, args=None, reply=None, timeout=5000):
            return ({"Muted": False},) if method == "GetProperties" else None
        with mock.patch.object(agent, "call", stubborn):
            with self.assertRaises(RuntimeError):
                agent.cmd_callaudio_mute(self.agent, {"on": True})


if __name__ == "__main__":
    unittest.main()
