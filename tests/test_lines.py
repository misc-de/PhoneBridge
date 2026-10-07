# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Lines: SIM cards (by their ICCID, whatever slot they are in) and GNOME
Calls' SIP accounts, and a call going out on the chosen one."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from phonebridge import agent

SIP_CFG = """[sip-00]
Id=work-account
Host=sip.example.org
User=4930123
DisplayName=Büro

[sip-01]
Host=voip.example.net
User=me
"""


class FakeOfono:
    def __init__(self, cards):
        self.cards = cards            # modem path -> ICCID
        self.dialled = []
        self.activated = []

    def __call__(self, conn, name, path, iface, method, args=None, reply=None, timeout=5000):
        if method == "GetModems":
            return ([(m, {}) for m in self.cards],)
        if iface == "org.ofono.SimManager":
            iccid = self.cards[path]
            return ({"Present": iccid is not None, "CardIdentifier": iccid or "",
                     "SubscriberNumbers": ["+4915550000099"] if path == "/ril_1" else []},)
        if iface == "org.ofono.NetworkRegistration":
            return ({"Name": "Netz %s" % path[-1], "Status": "registered"},)
        if method == "Dial":
            self.dialled.append((path, args.unpack()))
            return ("/ril_x/voicecall01",)
        if method == "Activate":
            self.activated.append((name, args.unpack()))
            return None
        raise RuntimeError("unexpected %s" % method)


class Lines(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phonebridge-lines-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = os.path.join(self.dir, "sip-account.cfg")
        with open(self.cfg, "w") as f:
            f.write(SIP_CFG)
        p = mock.patch.object(agent, "SIP_KEYFILE", self.cfg)
        p.start()
        self.addCleanup(p.stop)
        self.agent = mock.Mock(spec=["system", "session"])

    def use(self, cards):
        fake = FakeOfono(cards)
        p = mock.patch.object(agent, "call", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def test_two_sims_and_sip(self):
        self.use({"/ril_0": "8949001", "/ril_1": "8949002"})
        lines = agent.cmd_lines(self.agent, {})
        self.assertEqual([(l["id"], l.get("slot")) for l in lines],
                         [("sim:8949001", 1), ("sim:8949002", 2),
                          ("sip:work-account", None), ("sip:sip-01", None)])
        self.assertEqual(lines[1]["number"], "+4915550000099")
        self.assertEqual(lines[2]["name"], "Büro")
        self.assertEqual(lines[3]["address"], "me@voip.example.net")

    def test_empty_slot_is_no_line(self):
        self.use({"/ril_0": "8949001", "/ril_1": None})
        self.assertEqual([l["id"] for l in agent.sim_lines(self.agent)], ["sim:8949001"])

    def test_dial_on_the_card_wherever_it_is(self):
        fake = self.use({"/ril_0": "8949002", "/ril_1": "8949001"})   # cards swapped
        agent.cmd_call(self.agent, {"number": "0155 50000001", "line": "sim:8949001"})
        self.assertEqual(fake.dialled, [("/ril_1", ("+4915550000001", "default"))])

    def test_dial_sip(self):
        fake = self.use({"/ril_0": "8949001"})
        agent.cmd_call(self.agent, {"number": "+4915550000001", "line": "sip:work-account"})
        self.assertEqual(fake.activated, [("org.gnome.Calls", (
            "dial-sip", [("+4915550000001", "work-account")], {}))])

    def test_unknown_lines(self):
        self.use({"/ril_0": "8949001"})
        with self.assertRaises(RuntimeError):
            agent.cmd_call(self.agent, {"number": "1", "line": "sim:gone"})
        with self.assertRaises(RuntimeError):
            agent.cmd_call(self.agent, {"number": "1", "line": "sip:gone"})

    def test_no_keyfile(self):
        self.assertEqual(agent.sip_lines(os.path.join(self.dir, "none.cfg")), [])


if __name__ == "__main__":
    unittest.main()
