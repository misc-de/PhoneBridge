# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Deleting a conversation: from chatty's store (copied first), with chatty
stopped meanwhile and started again as it ran - here a stand-in "chatty"."""

import os
import sqlite3
import subprocess
import textwrap
import time
import unittest
from unittest import mock

from phonebridge import agent
from phonebridge.connection import Device

from .support import ANNA, BERND, Home, run_loop_until


def counts(path):
    db = sqlite3.connect(path)
    out = {t: db.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
           for t in ("threads", "messages", "thread_members", "mm_messages")}
    db.close()
    return out


class Store(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        for _ in range(3):
            self.home.store.add(ANNA, "a")
        self.home.store.add(BERND, "b")

    def test_only_that_conversation(self):
        n = agent.delete_thread_rows(ANNA, self.home.store_path)
        self.assertEqual(n, 3)
        self.assertEqual(counts(self.home.store_path),
                         {"threads": 1, "messages": 1, "thread_members": 1, "mm_messages": 1})
        self.assertEqual(agent.delete_thread_rows("+49000", self.home.store_path), 0)

    def test_backups_kept_three(self):
        with mock.patch.object(agent, "DATA_DIR", self.home.data):
            made = []
            for i in range(5):
                with mock.patch.object(agent.time, "strftime",
                                       return_value="20261007-10000%d" % i):
                    made.append(agent.backup_store(self.home.store_path))
            left = sorted(os.listdir(os.path.join(self.home.data, "backups")))
        self.assertEqual(left, [os.path.basename(p) for p in made[-3:]])
        self.assertEqual(counts(made[-1])["messages"], 4)

    def test_drop_sent(self):
        for to in (ANNA, BERND, ANNA):
            agent.append_sent({"id": "s", "to": to, "body": "x", "time": 1},
                              path=self.home.sent)
        self.assertEqual(agent.drop_sent(ANNA, self.home.sent), 2)
        self.assertEqual([e["to"] for e in agent.read_sent(self.home.sent)], [BERND])
        self.assertEqual(agent.drop_sent(ANNA, self.home.sent), 0)


class WithChatty(unittest.TestCase):
    """The agent ends a (stand-in) chatty, deletes, and starts it again."""

    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.home.store.add(ANNA, "a")
        self.home.store.add(BERND, "b")
        bindir = os.path.join(self.home.dir, "bin")
        os.makedirs(bindir)
        self.starts = os.path.join(self.home.dir, "starts")
        self.fake = os.path.join(bindir, "chatty")
        with open(self.fake, "w") as f:
            f.write(textwrap.dedent("""\
                #!/bin/bash
                # stand-in for chatty: notes each start with its marker, then idles
                echo "$CHATTY_MARKER $*" >> "%s"
                trap 'exit 0' TERM
                while :; do sleep 0.1; done
                """ % self.starts))
        os.chmod(self.fake, 0o755)
        self.log = os.path.join(self.home.dir, "log")
        env = dict(os.environ, CHATTY_MARKER="from-the-session", FAKE_LOG=self.log)
        self.proc = subprocess.Popen([self.fake, "--gapplication-service"], env=env)
        self.assertTrue(run_loop_until(lambda: agent.chatty_processes(), 5))
        patcher = mock.patch.dict(os.environ, dict(self.home.env(), FAKE_LOG=self.log))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._kill_chatty)
        self.dev = Device({"id": "t", "host": "phone", "user": "me"})
        self.addCleanup(self.dev.stop)

    def _kill_chatty(self):
        self.proc.poll()
        for pid, _argv, _env in agent.chatty_processes():
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

    def test_delete_restarts_chatty(self):
        first = [pid for pid, _a, _e in agent.chatty_processes()]
        self.dev.start()
        self.assertTrue(run_loop_until(lambda: self.dev.online, 15))
        box = {}
        self.dev.request("sms.delete_thread", {"thread": ANNA},
                         lambda r, e: box.update(r=r, e=e))
        self.assertTrue(run_loop_until(lambda: box, 20))
        self.assertIsNone(box["e"])
        self.assertEqual(box["r"], {"deleted": 1, "restarted": True})
        self.assertEqual(counts(self.home.store_path)["threads"], 1)
        self.proc.wait(5)                                   # the first one ended ...
        self.assertTrue(run_loop_until(lambda: agent.chatty_processes(), 5))
        again = [pid for pid, _a, _e in agent.chatty_processes()]
        self.assertTrue(set(again).isdisjoint(first))      # ... and a new one runs
        self.assertTrue(run_loop_until(
            lambda: open(self.starts).read().count("\n") == 2, 5))
        with open(self.starts) as f:
            self.assertEqual(f.read().splitlines(),
                             ["from-the-session --gapplication-service"] * 2)
        # the copy made before deleting goes once the deleting worked
        self.assertEqual(os.listdir(os.path.join(self.home.data, "backups")), [])
        self.assertEqual(os.stat(os.path.join(self.home.data, "backups")).st_mode & 0o777,
                         0o700)
        with open(self.log) as f:
            self.assertIn("systemd-run --user --scope", f.read())


if __name__ == "__main__":
    unittest.main()
