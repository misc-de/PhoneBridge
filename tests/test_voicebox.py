# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""VoiceBox's messages: read like VoiceBox's store, matched to the calls it
answered, marked as heard and deleted the way VoiceBox does it."""

import json
import os
import time
import unittest
from unittest import mock

from phonebridge import agent
from phonebridge.connection import Device

from .support import Home, run_loop_until

NUMBER = "+4915550000001"


def write_message(root, mid, number=NUMBER, when=None, duration=12.5, new=True,
                  missed=False, box="global", audio=b"RIFF....WAVEfake"):
    d = os.path.join(root, "messages")
    os.makedirs(d, exist_ok=True)
    if not missed:
        with open(os.path.join(d, mid + ".wav"), "wb") as f:
            f.write(audio)
    with open(os.path.join(d, mid + ".json"), "w", encoding="utf-8") as f:
        json.dump({"number": number, "name": "", "time": when or time.time(),
                   "duration": 0.0 if missed else duration, "new": new, "box": box,
                   "line": "sim0", "missed": missed}, f)


class Store(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.root = os.path.join(self.home.dir, "voicebox")
        p = mock.patch.object(agent, "VOICEBOX_DATA", self.root)
        p.start()
        self.addCleanup(p.stop)
        now = time.time()
        write_message(self.root, "20261007-080000", when=now - 600)
        write_message(self.root, "20261007-090000", when=now - 60, missed=True)
        with open(os.path.join(self.root, "messages", ".rec-1.wav"), "wb") as f:
            f.write(b"in progress")

    def test_list(self):
        msgs = agent.voicebox_messages(book=None)
        self.assertEqual([m["id"] for m in msgs], ["20261007-090000", "20261007-080000"])
        self.assertEqual([m["audio"] for m in msgs], [False, True])
        self.assertTrue(msgs[0]["missed"])

    def test_read_keeps_everything_else(self):
        agent.voicebox_mark_read("20261007-080000")
        with open(os.path.join(self.root, "messages", "20261007-080000.json")) as f:
            meta = json.load(f)
        self.assertFalse(meta["new"])
        self.assertEqual(meta["line"], "sim0")
        self.assertFalse(os.path.exists(os.path.join(self.root, "messages",
                                                     "20261007-080000.json.tmp")))

    def test_delete(self):
        agent.voicebox_delete("20261007-080000")
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "messages"))),
                         [".rec-1.wav", "20261007-090000.json"])

    def test_no_paths(self):
        for bad in ("../config", "a/b", "", ".rec-1"):
            with self.assertRaises(RuntimeError):
                agent.voicebox_delete(bad)

    def test_match_calls(self):
        msgs = agent.voicebox_messages(book=None)
        now = time.time()
        calls = [{"number": "0155 50000001", "start": now - 90, "duration": 25},
                 {"number": NUMBER, "start": now - 640, "duration": 30},
                 {"number": NUMBER, "start": now - 86400, "duration": 30},
                 {"number": "+4915550000009", "start": now - 60, "duration": 5}]
        agent.match_voicebox(calls, msgs)
        self.assertEqual(calls[0]["voicebox"]["id"], "20261007-090000")
        self.assertTrue(calls[0]["voicebox"]["missed"])
        self.assertEqual(calls[1]["voicebox"]["id"], "20261007-080000")
        self.assertNotIn("voicebox", calls[2])
        self.assertNotIn("voicebox", calls[3])

    def test_not_installed(self):
        with mock.patch.object(agent, "VOICEBOX_DATA", os.path.join(self.home.dir, "none")), \
                mock.patch.object(agent, "VOICEBOX_CONFIG", os.path.join(self.home.dir, "x")):
            self.assertFalse(agent.voicebox_installed())
            self.assertEqual(agent.voicebox_messages(), [])


class OverTheWire(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.root = os.path.join(self.home.dir, "voicebox")
        os.makedirs(os.path.join(self.root, "messages"))
        cfg = os.path.join(self.home.dir, "vb-config.json")
        with open(cfg, "w") as f:
            json.dump({"boxes": [{"id": "global", "name": "General", "active": True},
                                 {"id": "b1", "name": "Familie", "active": True}]}, f)
        env = dict(self.home.env(), PHONEBRIDGE_VOICEBOX=self.root,
                   PHONEBRIDGE_VOICEBOX_CONFIG=cfg)
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.dev = Device({"id": "t", "host": "phone", "user": "me"})
        self.addCleanup(self.dev.stop)
        self.events = []
        self.dev.connect("voicebox", lambda d: self.events.append(1))
        self.dev.start()
        self.assertTrue(run_loop_until(lambda: self.dev.online, 15))

    def ask(self, cmd, args=None):
        box = {}
        self.dev.request(cmd, args, lambda r, e: box.update(r=r, e=e))
        self.assertTrue(run_loop_until(lambda: box, 15), cmd)
        return box["r"], box["e"]

    def test_new_message_list_audio_read(self):
        self.assertTrue(self.dev.hello["has"]["voicebox"])
        write_message(self.root, "20261007-100000", box="b1", audio=b"RIFFxxxxWAVE")
        self.assertTrue(run_loop_until(lambda: self.events, 40), "no voicebox event")
        result, error = self.ask("voicebox.list")
        self.assertEqual([b["name"] for b in result["boxes"]], ["General", "Familie"])
        self.assertEqual(result["messages"][0]["box"], "b1")
        audio, error = self.ask("voicebox.audio", {"id": "20261007-100000"})
        import base64
        self.assertEqual(base64.b64decode(audio["data"]), b"RIFFxxxxWAVE")
        self.assertEqual(self.ask("voicebox.read", {"id": "20261007-100000"}), (True, None))
        self.assertFalse(self.ask("voicebox.list")[0]["messages"][0]["new"])
        self.assertEqual(self.ask("voicebox.audio", {"id": "../vb-config"})[1],
                         "no such message")


if __name__ == "__main__":
    unittest.main()


class SortedIntoCalls(unittest.TestCase):
    """The phone page: messages in the call history, not in a block of their own."""

    def test_entries(self):
        import types
        from phonebridge.phone import PhonePage
        now = time.time()
        msgs = [
            {"id": "m1", "number": NUMBER, "name": "", "time": now - 100, "duration": 9.0,
             "new": True, "box": "global", "missed": False, "audio": True},
            {"id": "m2", "number": "+4915550000002", "name": "Bernd", "time": now - 50,
             "duration": 4.0, "new": False, "box": "b1", "missed": False, "audio": True},
            {"id": "m3", "number": NUMBER, "name": "", "time": now - 900, "duration": 0.0,
             "new": True, "box": "global", "missed": True, "audio": False},
        ]
        app = types.SimpleNamespace(
            voicebox={"t": {"messages": msgs, "boxes": [{"id": "global"}, {"id": "b1"}]}},
            voicebox_box_name=lambda dev_id, box: {"global": "General", "b1": "Familie"}[box])
        page = types.SimpleNamespace(dev=types.SimpleNamespace(id="t"), app=app, calls=[
            {"number": NUMBER, "name": "", "inbound": True, "answered": True,
             "start": now - 130, "duration": 20, "voicebox": {"id": "m1", "missed": False,
                                                            "audio": True, "duration": 9.0,
                                                            "new": True}},
            {"number": "+4915550000009", "name": "", "inbound": False, "answered": True,
             "start": now - 20, "duration": 60},
            {"number": NUMBER, "name": "", "inbound": True, "answered": True,
             "start": now - 3600, "duration": 5, "voicebox": {"id": "gone", "missed": False,
                                                            "audio": True, "duration": 3.0,
                                                            "new": False}}])
        page._vb = lambda dev, m: PhonePage._vb(page, dev, m)
        entries = PhonePage.entries(page)
        # newest first; m2 has no call of its own and comes in on its own
        self.assertEqual([e.get("voicebox", {}).get("id") for e in entries],
                         [None, "m2", "m1", None])
        self.assertEqual(entries[1]["voicebox"]["box_name"], "Familie")
        self.assertTrue(entries[2]["voicebox"]["new"])
        self.assertNotIn("voicebox", entries[3])        # deleted meanwhile
