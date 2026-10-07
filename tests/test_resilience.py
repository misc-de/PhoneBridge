# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""When things go wrong: a phone that never answers, one that takes long to
fail, a PC that vanished, big recordings, flaky pictures, a double switch,
and what must never pass as markup, option or property."""

import base64
import json
import os
import time
import unittest
from unittest import mock

from gi.repository import GLib

from phonebridge import agent, config, connection, text
from phonebridge.connection import Device

from .support import Home, run_loop_until


class Connection(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        p = mock.patch.dict(os.environ, self.home.env())
        p.start()
        self.addCleanup(p.stop)
        self.dev = Device({"id": "t", "host": "phone", "user": "me"})
        self.addCleanup(self.dev.stop)

    def test_a_slow_failure_does_not_block_the_window(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_SLOW": "2"}):
            t = time.monotonic()
            self.dev.start()
            self.assertLess(time.monotonic() - t, 0.5)     # was 2 s and more
            ticks = []
            GLib.timeout_add(50, lambda: ticks.append(1) or True)
            self.assertTrue(run_loop_until(lambda: self.dev.error, 10))
        self.assertGreater(len(ticks), 20)                  # the loop kept going
        hit = []
        GLib.idle_add(lambda: hit.append(1) and False)
        self.assertTrue(run_loop_until(lambda: hit, 2))     # and idles still run

    def test_no_answer_times_out_and_reconnects(self):
        with mock.patch.object(connection, "BACKOFF", (60,)), \
                mock.patch.dict(os.environ, {"FAKE_SSH_HANG": "1"}):
            self.dev.start()
            box = {}
            self.dev.request("ping", {}, lambda r, e: box.update(e=e), timeout=1)
            self.assertTrue(run_loop_until(lambda: box, 5))
            self.assertEqual(box["e"], "timeout")
            self.assertEqual(self.dev.state, "connecting")
        # hello gets no answer either: the connection is ended to start anew
        with mock.patch.object(connection, "BACKOFF", (60,)):
            self.dev._pending.clear()
            self.dev._on_hello(None, "timeout")
            self.assertTrue(run_loop_until(lambda: self.dev.state == "offline", 5))

    def test_dead_line_is_noticed_by_the_ping(self):
        with mock.patch.object(connection, "PING_EVERY", 1), \
                mock.patch.object(connection, "PING_TIMEOUT", 1):
            self.dev.start()
            self.assertTrue(run_loop_until(lambda: self.dev.online, 15))
            os.kill(self.dev._proc.get_identifier() and int(self.dev._proc.get_identifier()),
                    19)                                     # SIGSTOP: hangs, stays alive
            self.assertTrue(run_loop_until(lambda: not self.dev.online, 10))
        self.assertIn("does not answer", " ".join(self.dev._stderr))

    def test_reconnect_ends_a_hanging_connection(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_HANG": "1"}):
            self.dev.start()
            first = self.dev._proc
            self.assertTrue(run_loop_until(lambda: first.get_identifier(), 5))
        self.dev.reconnect()
        self.assertTrue(run_loop_until(lambda: self.dev._proc not in (None, first), 10))
        self.assertTrue(run_loop_until(lambda: self.dev.online, 15))

    def test_stop_lets_the_agent_end_by_itself(self):
        self.dev.start()
        self.assertTrue(run_loop_until(lambda: self.dev.online, 15))
        proc = self.dev._proc
        self.dev.stop()
        self.assertTrue(run_loop_until(lambda: proc.get_identifier() is None, 5))
        self.assertTrue(proc.get_if_exited())               # ended, not killed


class Agent(unittest.TestCase):
    def test_watchdog_lets_go_of_the_microphone(self):
        a = mock.Mock()
        a.last_rx = time.monotonic() - 100
        with mock.patch.object(agent, "pc_audio_release") as release:
            self.assertFalse(agent.Agent._watchdog(a))
        release.assert_called_once_with(a)
        a.loop.quit.assert_called_once()
        a.last_rx = time.monotonic()
        self.assertTrue(agent.Agent._watchdog(a))

    def test_guarded_keeps_the_timer(self):
        def boom():
            raise ValueError("x")
        with mock.patch("traceback.print_exc"):
            self.assertTrue(agent.guarded(boom)(keep=True))

    def test_only_numbers_are_dialled(self):
        for bad in ("--provider=evil", "Vodafone", "1 2; rm"):
            with self.assertRaises(RuntimeError):
                agent.cmd_call(mock.Mock(), {"number": bad})

    def test_one_line_and_escape(self):
        c = {"given": "A", "family": "B", "org": "", "note": "x\ry",
             "phones": [{"type": "mobile", "value": "0155\nX-EVIL:1"}],
             "emails": ["a@b\r\nX-EVIL:2"], "birthday": "1990-01-01\nX-EVIL:3"}
        card = agent.vcard_from_contact(c)
        self.assertNotIn("\nX-EVIL", card)
        self.assertIn("NOTE:x\\ny", card)

    def test_sent_log_is_private_and_trimmed(self):
        home = Home()
        self.addCleanup(home.cleanup)
        old = {"id": "s0", "to": "+491", "body": "x" * 3000, "time": 1}
        for i in range(100):
            agent.append_sent(old, path=home.sent)
        agent.append_sent({"id": "s1", "to": "+492", "body": "neu", "time": time.time()},
                          path=home.sent)
        self.assertEqual(os.stat(home.sent).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(home.data).st_mode & 0o777, 0o700)
        ids = [e["id"] for e in agent.read_sent(home.sent)]
        self.assertLess(ids.count("s0"), 100)          # old ones went at 256 KB
        self.assertEqual(ids[-1], "s1")
        self.assertLess(os.path.getsize(home.sent), 256 * 1024)

    def test_pim_does_not_create_twice(self):
        pim = agent.Pim(None)
        pim._handle = lambda kind, uid: ("n", "/p", "i", True)
        calls = []

        def fail(*args, **kw):
            calls.append(args[4])
            raise agent.DBusFailure("timeout", "org.freedesktop.DBus.Error.NoReply")
        with mock.patch.object(agent, "call", fail):
            with self.assertRaises(agent.DBusFailure):
                pim.call("contacts", "b", "CreateContacts", None)
        self.assertEqual(calls, ["CreateContacts"])
        calls.clear()

        def gone(*args, **kw):
            calls.append(args[4])
            raise agent.DBusFailure("gone", "org.freedesktop.DBus.Error.UnknownObject")
        with mock.patch.object(agent, "call", gone):
            with self.assertRaises(agent.DBusFailure):
                pim.call("contacts", "b", "GetContactList", None)
        self.assertEqual(calls, ["GetContactList", "GetContactList"])   # reopened once


class OverTheWire(unittest.TestCase):
    def test_big_recording_in_pieces(self):
        home = Home()
        self.addCleanup(home.cleanup)
        vb = os.path.join(home.dir, "vb")
        os.makedirs(os.path.join(vb, "messages"))
        audio = os.urandom(agent.VOICEBOX_CHUNK * 2 + 1234)
        with open(os.path.join(vb, "messages", "m1.wav"), "wb") as f:
            f.write(audio)
        with open(os.path.join(vb, "messages", "m1.json"), "w") as f:
            json.dump({"number": "1", "time": 1, "duration": 1, "new": True}, f)
        cache = os.path.join(home.dir, "cache")
        with mock.patch.dict(os.environ, dict(home.env(), PHONEBRIDGE_VOICEBOX=vb,
                                              XDG_CACHE_HOME=cache)):
            dev = Device({"id": "t", "host": "phone", "user": "me"})
            self.addCleanup(dev.stop)
            dev.start()
            self.assertTrue(run_loop_until(lambda: dev.online, 15))
            from phonebridge.app import PhoneBridgeApp
            sizes = []
            real = dev.request

            def spy(cmd, args=None, cb=None, timeout=None):
                if cmd == "voicebox.audio":
                    sizes.append(args.get("offset", 0))
                return real(cmd, args, cb, timeout)
            dev.request = spy
            got = {}
            PhoneBridgeApp.voicebox_audio(None, dev, "m1", lambda p, e: got.update(p=p, e=e))
            self.assertTrue(run_loop_until(lambda: got, 20))
        self.assertIsNone(got["e"])
        with open(got["p"], "rb") as f:
            self.assertEqual(f.read(), audio)
        self.assertEqual(len(sizes), 3)
        self.assertEqual(os.stat(got["p"]).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(got["p"])).st_mode & 0o777, 0o700)


class PC(unittest.TestCase):
    def test_ssh_destination_is_no_option(self):
        argv = connection.ssh_argv({"user": "furios", "host": "10.0.0.5"})
        self.assertEqual(argv[argv.index("--") + 1], "furios@10.0.0.5")
        for user, host in (("-oProxyCommand=x", "h"), ("u", "-oX"), ("a b", "h"),
                           ("u", "h h"), ("u@x", "h")):
            self.assertFalse(config.valid_device({"id": "a", "user": user, "host": host}),
                             (user, host))
        self.assertTrue(config.valid_device({"id": "a", "user": "furios",
                                             "host": "fe80::1", "port": 22}))
        self.assertFalse(config.valid_port("70000"))

    def test_notification_text(self):
        from phonebridge.app import notification_text
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "XFCE"}):
            self.assertEqual(notification_text('<a href="x">Paket</a> & Co'),
                             '&lt;a href=&quot;x&quot;&gt;Paket&lt;/a&gt; &amp; Co')
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            self.assertEqual(notification_text("<b>"), "<b>")

    def test_avatar_errors_are_not_remembered(self):
        from phonebridge import avatars
        home = Home()
        self.addCleanup(home.cleanup)
        with mock.patch.object(avatars, "CACHE_DIR", os.path.join(home.dir, "a")):
            cache = avatars.Avatars()
            asked = []
            dev = mock.Mock()
            dev.request = lambda cmd, args, cb: asked.append(cb)
            cache.get(dev, "a" * 20, lambda t: None)
            asked[0](None, "connection lost")
            self.assertNotIn("a" * 20, cache.textures)       # asked again next time
            cache.get(dev, "a" * 20, lambda t: None)
            self.assertEqual(len(asked), 2)
            asked[1]({"data": base64.b64encode(b"no picture").decode()}, None)
            self.assertIsNone(cache.textures["a" * 20])        # truly unusable: kept

    def test_avatar_memory_is_bounded(self):
        from phonebridge import avatars
        cache = avatars.Avatars()
        for i in range(avatars.LIMIT + 50):
            cache._keep("%040x" % i, None)
        self.assertEqual(len(cache.textures), avatars.LIMIT)
        self.assertNotIn("%040x" % 0, cache.textures)

    def test_leftover_echo_cancel_is_unloaded(self):
        from phonebridge import callaudio
        out = ("12\tmodule-echo-cancel\tsource_name=phonebridge_ec_source sink_name=x\t\n"
               "13\tmodule-echo-cancel\tsource_name=other\t\n")
        runs = []

        def fake(argv, **kw):
            runs.append(argv)
            return mock.Mock(stdout=out, returncode=0)
        with mock.patch.object(callaudio.subprocess, "run", fake):
            callaudio.unload_leftovers()
        self.assertIn(["pactl", "unload-module", "12"], runs)
        self.assertNotIn(["pactl", "unload-module", "13"], runs)

    def test_notification_pictures_are_files(self):
        from phonebridge import app, icon
        home = Home()
        self.addCleanup(home.cleanup)
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": home.dir}):
            a = app.notification_picture(icon.avatar_png("Anna", 32))
            b = app.notification_picture(icon.avatar_png("Anna", 32))
            self.assertEqual(a, b)                         # named by content
            self.assertEqual(os.stat(a).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.dirname(a)).st_mode & 0o777, 0o700)
            for i in range(8):
                app.notification_picture(icon.avatar_png("Name %d" % i, 32), keep=5)
            self.assertEqual(len(os.listdir(os.path.dirname(a))), 5)

    def test_error_words(self):
        self.assertEqual(text.error("timeout"), "timeout")       # en in the tests


if __name__ == "__main__":
    unittest.main()


class Battery(unittest.TestCase):
    """Told once when full, once below 5 % - again after the next charge cycle."""

    def run_states(self, states):
        from phonebridge.app import PhoneBridgeApp
        app = PhoneBridgeApp.__new__(PhoneBridgeApp)
        app._battery = {}
        told = []
        app._battery_note = lambda dev, kind, *a: told.append(kind)
        dev = mock.Mock()
        dev.id, dev.name = "t", "Phone"
        for percent, state in states:
            dev.status = {"battery": {"percent": percent, "state": state}}
            PhoneBridgeApp._battery_check(app, dev)
        return told

    def test_full_once_per_charge(self):
        self.assertEqual(self.run_states([
            (90, "charging"), (99, "charging"), (100, "charging"),   # full: told
            (100, "full"), (100, "charging"),                          # still full: quiet
            (98, "discharging"), (97, "discharging"),                  # unplugged
            (99, "charging"), (100, "full")]),                         # full again: told
            ["full", "full"])

    def test_already_full_when_met(self):
        self.assertEqual(self.run_states([(100, "full"), (100, "charging")]), [])

    def test_low_once(self):
        self.assertEqual(self.run_states([
            (6, "discharging"), (4, "discharging"), (3, "discharging"),  # told once
            (3, "charging"), (5, "charging"),                            # charging: reset
            (4, "discharging")]),                                         # low again: told
            ["low", "low"])

    def test_low_when_met(self):
        self.assertEqual(self.run_states([(2, "discharging")]), ["low"])
