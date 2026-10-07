# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The agent: reading chatty's store, our log of sent messages, and the
protocol end to end - through connection.Device and the stand-in ssh."""

import os
import time
import unittest
from unittest import mock

from phonebridge import agent, text
from phonebridge.connection import Device, ssh_argv

from .support import ANNA, BERND, GROUP, Home, run_loop_until


class Numbers(unittest.TestCase):
    CASES = ["0171 123-45", "0049 171 12345", "+49 (171) 123/45", "+4917112345",
             "Vodafone", "22922", "", "030.1234"]

    def test_normalize(self):
        self.assertEqual(agent.normalize("0171 123-45"), "+4917112345")
        self.assertEqual(agent.normalize("0049171"), "+49171")
        self.assertEqual(agent.normalize("0171", country="43"), "+43171")
        self.assertEqual(agent.normalize("Vodafone"), "Vodafone")

    def test_pc_side_normalizes_the_same(self):
        for case in self.CASES:
            self.assertEqual(agent.normalize(case), text.normalize(case), case)


class StoreReading(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.s = self.home.store
        now = time.time()
        self.a1 = self.s.add(ANNA, "Hello!", at=now - 300, member_alias="Anna")
        self.a2 = self.s.add(ANNA, "See you later", incoming=False, at=now - 200)
        self.b1 = self.s.add(BERND, "Package is here", at=now - 100)
        self.g1 = self.s.add(GROUP, "Group", at=now - 400, kind=1)

    def tearDown(self):
        self.home.cleanup()

    def threads(self, **kw):
        return agent.list_threads(store=self.home.store_path, sent=self.home.sent, **kw)

    def test_threads_newest_first_with_names(self):
        threads = self.threads()
        self.assertEqual([t["thread"] for t in threads], [BERND, ANNA, GROUP])
        anna = threads[1]
        self.assertEqual(anna["title"], "Anna")
        self.assertEqual(anna["last"]["body"], "See you later")
        self.assertTrue(anna["last"]["out"])
        self.assertEqual(threads[0]["title"], BERND)
        self.assertTrue(threads[2]["group"])

    def test_unread_after_baseline_and_seen(self):
        by = {t["thread"]: t["unread"] for t in self.threads(baseline=0)}
        self.assertEqual(by, {ANNA: 1, BERND: 1, GROUP: 1})
        by = {t["thread"]: t["unread"] for t in self.threads(baseline=self.a1)}
        self.assertEqual(by[ANNA], 0)
        self.assertEqual(by[BERND], 1)
        by = {t["thread"]: t["unread"]
              for t in self.threads(baseline=0, seen={BERND: self.b1})}
        self.assertEqual(by[BERND], 0)
        self.assertEqual(by[ANNA], 1)

    def test_messages_oldest_first(self):
        msgs = agent.list_messages(ANNA, store=self.home.store_path, sent=self.home.sent)
        self.assertEqual([m["body"] for m in msgs], ["Hello!", "See you later"])
        self.assertEqual([m["out"] for m in msgs], [False, True])

    def test_sent_log_merges_into_thread(self):
        agent.append_sent({"id": "s1", "to": ANNA, "body": "From PhoneBridge",
                           "time": int(time.time()), "status": "sent"},
                          path=self.home.sent)
        msgs = agent.list_messages("0155 50000001", store=self.home.store_path,
                                   sent=self.home.sent)
        self.assertEqual(msgs[-1]["body"], "From PhoneBridge")
        threads = self.threads()
        self.assertEqual(threads[0]["thread"], ANNA)
        self.assertEqual(threads[0]["last"]["body"], "From PhoneBridge")

    def test_sent_log_is_dropped_when_chatty_has_it(self):
        now = int(time.time())
        self.s.add(ANNA, "Twice", incoming=False, at=now)
        agent.append_sent({"id": "s2", "to": ANNA, "body": "Twice", "time": now + 5},
                          path=self.home.sent)
        msgs = agent.list_messages(ANNA, store=self.home.store_path, sent=self.home.sent)
        self.assertEqual([m["body"] for m in msgs].count("Twice"), 1)

    def test_sent_to_new_number_is_its_own_thread(self):
        agent.append_sent({"id": "s3", "to": "+4915550000009", "body": "New",
                           "time": int(time.time())}, path=self.home.sent)
        threads = self.threads()
        self.assertEqual(threads[0]["thread"], "+4915550000009")
        self.assertEqual(threads[0]["unread"], 0)

    def test_new_incoming(self):
        new = agent.new_incoming(self.a1, store=self.home.store_path)
        self.assertEqual([m["body"] for m in new], ["Package is here", "Group"])
        self.assertEqual(new[0]["thread"], BERND)
        self.assertEqual(agent.new_incoming(0, store=self.home.store_path)[0]["title"],
                         "Anna")

    def test_no_store(self):
        missing = os.path.join(self.home.dir, "none.db")
        self.assertEqual(agent.list_threads(store=missing, sent=self.home.sent), [])
        self.assertEqual(agent.store_last_id(missing), 0)


class EndToEnd(unittest.TestCase):
    """The PC side talking to the agent, which runs here instead of on a phone."""

    def setUp(self):
        self.home = Home()
        self.home.store.add(ANNA, "Hello!", member_alias="Anna")
        patcher = mock.patch.dict(os.environ, self.home.env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.home.cleanup)
        self.dev = Device({"id": "test", "name": "Test", "host": "phone", "user": "me"})
        self.addCleanup(self.dev.stop)
        self.events = []
        self.dev.connect("sms", lambda d, new: self.events.append(new))

    def ask(self, cmd, args=None):
        box = {}
        self.dev.request(cmd, args, lambda r, e: box.update(result=r, error=e))
        self.assertTrue(run_loop_until(lambda: box, 15), "no answer to " + cmd)
        return box["result"], box["error"]

    def test_ssh_command(self):
        argv = ssh_argv({"user": "furios", "host": "10.0.0.5", "port": 2222})
        self.assertIn("furios@10.0.0.5", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertIn("BatchMode=yes", argv)
        self.assertTrue(argv[-1].startswith("python3 -u -c"))

    def test_conversation(self):
        self.dev.start()
        self.assertTrue(run_loop_until(lambda: self.dev.online, 15), self.dev.error)
        self.assertEqual(self.dev.hello["version"], agent.VERSION)
        self.assertTrue(self.dev.hello["has"]["sms"])

        self.assertTrue(run_loop_until(lambda: self.dev.status is not None, 10))
        self.assertEqual(self.dev.status["wifi"]["ssid"], "Testnet")
        self.assertEqual(self.dev.status["volume"], {"level": 0.4, "muted": False})

        threads, error = self.ask("sms.threads", {"baseline": 0})
        self.assertIsNone(error)
        self.assertEqual(threads[0]["title"], "Anna")
        msgs, error = self.ask("sms.messages", {"thread": ANNA})
        self.assertEqual([m["body"] for m in msgs], ["Hello!"])

        result, error = self.ask("gsettings.get", {"keys": [["no.such.schema", "key"]]})
        self.assertEqual(result, {"no.such.schema key": None})

        result, error = self.ask("nonsense")
        self.assertIsNone(result)
        self.assertIn("unknown command", error)
        self.assertEqual(self.ask("ping")[0], "pong")

        # chatty stores a new message - the agent reports it by itself
        self.home.store.add(BERND, "New message")
        self.assertTrue(run_loop_until(lambda: self.events, 30), "no sms event")
        self.assertEqual(self.events[0][0]["body"], "New message")

    def test_unreachable_phone(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_FAIL": "1"}):
            self.dev.start()
            self.assertTrue(run_loop_until(lambda: self.dev.error, 10))
        self.assertEqual(self.dev.state, "offline")
        self.assertIn("No route to host", self.dev.error)
        self.assertEqual(self.ask("ping"), (None, "not connected"))

    def test_lost_connection_fails_pending_requests(self):
        self.dev.start()
        self.assertTrue(run_loop_until(lambda: self.dev.online, 15))
        box = {}
        self.dev.request("ping", None, lambda r, e: box.update(e=e))
        self.dev._proc.force_exit()
        self.assertTrue(run_loop_until(lambda: not self.dev.online, 10))
        self.assertTrue(run_loop_until(lambda: box, 5))


class Payload(unittest.TestCase):
    def test_agent_runs_standalone(self):
        """The agent imports nothing from the package - it travels alone."""
        with open(agent.__file__, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("from .", source)
        self.assertNotIn("import phonebridge", source)


if __name__ == "__main__":
    unittest.main()
