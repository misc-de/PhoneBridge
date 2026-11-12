# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Calls over SIP shown like calls over the SIM: the number out of the SIP
address, the name from the address book - in the call history and while
the call is on (those never pass ofono: they come from GNOME Calls, a
stand-in on the private session bus here), answered and hung up there."""

import os
import sqlite3
import types
import unittest
from unittest import mock

from gi.repository import Gio

from phonebridge import agent

from .fake_calls import FakeCalls
from .support import Home, make_addressbook

ANNA = ("BEGIN:VCARD\nVERSION:3.0\nFN:Anna Example\n"
        "TEL;TYPE=CELL:+49 155 50000001\nEND:VCARD")


def session():
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception:  # noqa: BLE001
        return None


class Number(unittest.TestCase):
    def test_out_of_the_sip_address(self):
        for address, number in (
                ("sip:+4915550000001@sip.example.org", "+4915550000001"),
                ("sips:015550000001@sip.example.org;user=phone", "015550000001"),
                ("sip:0155%2050000001@sip.example.org", "0155 50000001"),
                ("tel:+4915550000001", "+4915550000001"),
                ("sip:anonymous@anonymous.invalid", ""),
                ("sip:alice@example.org", "sip:alice@example.org"),     # a name: as it is
                ("+4915550000001", "+4915550000001"),                    # SIM calls
                ("", ""), (None, "")):
            self.assertEqual(agent.call_number(address), number, address)


class History(unittest.TestCase):
    def test_sip_calls_with_the_contact(self):
        home = Home()
        self.addCleanup(home.cleanup)
        make_addressbook(home.books, [ANNA])
        path = os.path.join(home.dir, "records.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, target TEXT, inbound INTEGER,"
                   " start BLOB, answered BLOB, end BLOB, protocol TEXT)")
        db.executemany("INSERT INTO calls VALUES (?, ?, 1, '2026-10-07T06:00:00Z', NULL,"
                       " '2026-10-07T06:00:20Z', ?)", (
                           (1, "sip:015550000001@sip.example.org", "sip"),
                           (2, "+4915550000001", "tel"),
                           (3, "sip:alice@example.org", "sip")))
        db.commit()
        db.close()
        calls = agent.call_history(path=path, book=agent.Book(home.books))
        by_id = {c["id"]: c for c in calls}
        self.assertEqual((by_id[1]["number"], by_id[1]["name"]),
                         ("015550000001", "Anna Example"))
        self.assertEqual(by_id[2]["name"], "Anna Example")
        self.assertEqual((by_id[3]["number"], by_id[3]["name"]),
                         ("sip:alice@example.org", ""))
        # one person's calls: the SIP one counts as hers too
        mine = agent.call_history(path=path, book=None, numbers=["+49 155 50000001"])
        self.assertEqual([c["id"] for c in mine], [2, 1])


@unittest.skipUnless(session() is not None, "no session bus")
class WhileOn(unittest.TestCase):
    def setUp(self):
        self.calls = FakeCalls()
        self.addCleanup(self.calls.close)
        home = Home()
        self.addCleanup(home.cleanup)
        make_addressbook(home.books, [ANNA])
        patch = mock.patch.object(agent, "BOOK", agent.Book(home.books))
        patch.start()
        self.addCleanup(patch.stop)
        self.agent = types.SimpleNamespace(modem=None, system=None, session=session())

    def test_shown_like_a_sim_call(self):
        self.calls.add("tel:+4915550000002", state=1, protocol="tel")   # ofono has it
        path = self.calls.add("sip:015550000001@sip.example.org")
        anon = self.calls.add("sip:anonymous@anonymous.invalid", state=6)
        calls = agent.Agent.active_calls(self.agent)
        self.assertEqual([c["path"] for c in calls], [path, anon])
        c = calls[0]
        self.assertEqual((c["state"], c["number"], c["name"], c["line"]),
                         ("incoming", "015550000001", "Anna Example", "sip"))
        self.assertEqual((calls[1]["state"], calls[1]["number"], calls[1]["name"]),
                         ("waiting", "withheld", ""))

    def test_unknown_caller_without_a_name(self):
        self.calls.add("sip:+4915550000009@sip.example.org")
        self.calls.add("sip:alice@example.org", display_name="Alice")
        self.calls.add("sip:bob@example.org")                   # Calls: "bob"
        calls = agent.Agent.active_calls(self.agent)
        # Calls' display name of a SIP caller is the number: no name
        self.assertEqual([(c["number"], c["name"]) for c in calls],
                         [("+4915550000009", ""), ("sip:alice@example.org", "Alice"),
                          ("sip:bob@example.org", "")])

    def test_answer_and_hang_up_in_calls(self):
        path = self.calls.add("sip:015550000001@sip.example.org")
        self.assertTrue(agent.cmd_answer(self.agent, {"path": path}))
        self.assertEqual(agent.Agent.active_calls(self.agent)[0]["state"], "active")
        self.assertTrue(agent.cmd_hangup(self.agent, {"path": path}))
        self.assertEqual(self.calls.methods, [(path, "Accept"), (path, "Hangup")])
        self.assertEqual(agent.Agent.active_calls(self.agent), [])

    def test_calls_not_started_for_it(self):
        self.calls.close()
        self.assertEqual(agent.sip_calls(session()), [])


if __name__ == "__main__":
    unittest.main()
