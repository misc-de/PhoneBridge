# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""SIP accounts of GNOME Calls: written as Calls writes them, with Calls
(a stand-in here) ended for the change and started again - never during a
call; the passwords go to a keyring (none in the tests)."""

import os
import stat
import subprocess
import textwrap
import unittest
from unittest import mock

from phonebridge import agent
from phonebridge.connection import Device

from .support import Home, run_loop_until

EXISTING = """[sip-00]
Id=work
Host=sip.example.org
User=4930123
DisplayName=Büro
Protocol=UDP
Port=0
AutoConnect=true
DirectMode=false
LocalPort=0
CanTel=true
MediaEncryption=0
"""


class KeyFile(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.path = os.path.join(self.home.dir, "calls", "sip-account.cfg")
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write(EXISTING)

    def test_add_change_remove_keeps_the_others(self):
        group = agent.write_sip_account({"host": "voip.example.net", "user": "me",
                                         "display_name": "Private", "protocol": "TLS",
                                         "port": 5061, "can_tel": False}, self.path)
        self.assertEqual(group, "sip-01")
        accounts = {a["id"]: a for a in agent.sip_accounts(self.path)}
        self.assertEqual(set(accounts), {"work", "me@voip.example.net"})
        self.assertEqual(accounts["me@voip.example.net"]["protocol"], "TLS")
        self.assertEqual(accounts["me@voip.example.net"]["port"], 5061)
        self.assertEqual(accounts["work"]["display_name"], "Büro")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

        agent.write_sip_account(dict(accounts["work"], display_name="Work"), self.path)
        self.assertEqual({a["id"]: a["display_name"] for a in agent.sip_accounts(self.path)},
                         {"work": "Work", "me@voip.example.net": "Private"})
        agent.write_sip_account({"id": "work"}, self.path, remove=True)
        self.assertEqual([a["id"] for a in agent.sip_accounts(self.path)],
                         ["me@voip.example.net"])

    def test_checks(self):
        for bad in ({"host": "", "user": "me"}, {"host": "h", "user": "a b"},
                    {"host": "h", "user": "me", "protocol": "SCTP"}):
            with self.assertRaises(RuntimeError):
                agent.write_sip_account(bad, self.path)
        with self.assertRaises(RuntimeError):
            agent.write_sip_account({"id": "nope"}, self.path, remove=True)


class OverTheWire(unittest.TestCase):
    """sip.save through the agent: the stand-in Calls goes and comes back."""

    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.cfg = os.path.join(self.home.dir, "calls", "sip-account.cfg")
        self.log = os.path.join(self.home.dir, "log")
        bindir = os.path.join(self.home.dir, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "gnome-calls")
        with open(fake, "w") as f:
            f.write(textwrap.dedent("""\
                #!/bin/bash
                # stand-in for gnome-calls --daemon: notes each start, then idles
                echo "start $*" >> "%s"
                trap 'exit 0' TERM
                while :; do sleep 0.1; done
                """ % self.log))
        os.chmod(fake, 0o755)
        env = dict(os.environ, FAKE_LOG=self.log)
        self.calls = subprocess.Popen([fake, "--daemon"], env=env)
        self.assertTrue(run_loop_until(lambda: agent.processes_named("gnome-calls"), 5))
        self.addCleanup(self._kill)
        p = mock.patch.dict(os.environ, dict(self.home.env(), PHONEBRIDGE_SIP_KEYFILE=self.cfg,
                                             FAKE_LOG=self.log))
        p.start()
        self.addCleanup(p.stop)
        self.dev = Device({"id": "t", "host": "phone", "user": "me"})
        self.addCleanup(self.dev.stop)
        self.dev.start()
        self.assertTrue(run_loop_until(lambda: self.dev.online, 15))

    def _kill(self):
        for pid, _a, _e in agent.processes_named("gnome-calls"):
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

    def ask(self, cmd, args=None):
        box = {}
        self.dev.request(cmd, args, lambda r, e: box.update(r=r, e=e))
        self.assertTrue(run_loop_until(lambda: box, 20), cmd)
        return box["r"], box["e"]

    def test_add_and_remove(self):
        result, error = self.ask("sip.save", {"account": {
            "host": "voip.example.net", "user": "me", "display_name": "Private",
            "protocol": "UDP", "port": 0, "auto_connect": True, "can_tel": True,
            "media_encryption": 1}, "password": "geheim"})
        self.assertIsNone(error)
        self.assertEqual(result["id"], "me@voip.example.net")
        accounts, _e = self.ask("sip.list")
        self.assertEqual([a["display_name"] for a in accounts], ["Private"])
        lines, _e = self.ask("lines.list")
        self.assertIn("sip:me@voip.example.net", [line["id"] for line in lines])
        self.calls.wait(5)                                      # the first Calls ended ...
        self.assertTrue(run_loop_until(lambda: agent.processes_named("gnome-calls"), 5))
        with open(self.log) as f:
            log = f.read()
        self.assertEqual(log.count("start --daemon"), 2)        # ... and came back as it ran
        self.assertIn("secret store me@voip.example.net", log)

        result, error = self.ask("sip.delete", {"id": "me@voip.example.net"})
        self.assertIsNone(error)
        self.assertEqual(self.ask("sip.list")[0], [])
        with open(self.log) as f:
            self.assertIn("secret clear me@voip.example.net", f.read())

    def test_not_during_a_call(self):
        with mock.patch.object(agent.Agent, "active_calls", return_value=[{"path": "/c"}]):
            agent_obj = mock.Mock(active_calls=lambda: [{"path": "/c"}])
            with self.assertRaises(RuntimeError) as ctx:
                agent._with_calls_stopped(agent_obj, lambda: None)
        self.assertIn("not during a call", str(ctx.exception))
        self.assertEqual(len(agent.processes_named("gnome-calls")), 1)   # left running


if __name__ == "__main__":
    unittest.main()
