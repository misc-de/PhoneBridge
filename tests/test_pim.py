# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Contacts, appointments and calls: the vCard and iCalendar handling of
the agent, recurring appointments, and adding, changing and deleting
through a stand-in evolution-data-server on the private test bus."""

import base64
import datetime as dt
import os
import sqlite3
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from phonebridge import agent
from phonebridge.connection import Device

from .support import PNG, Home, run_loop_until

BERLIN = ZoneInfo("Europe/Berlin")

CARD = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:abc-1\r\nFN:Anna Example\r\n"
        "N:Example;Anna;;;\r\nTEL;TYPE=CELL:+49 155 50000001\r\n"
        "TEL;TYPE=WORK,VOICE:030 1234\r\nEMAIL;TYPE=INTERNET:anna@example.org\r\n"
        "ADR;TYPE=HOME:;;Weg 1;Stadt;;12345;\r\nBDAY:1990-12-24\r\n"
        "NOTE:Zeile 1\\nZeile 2\r\nX-EVOLUTION-WEBDAV-ETAG:\"etag-7\"\r\n"
        "PHOTO;ENCODING=b;TYPE=PNG:" + base64.b64encode(PNG).decode() + "\r\nEND:VCARD\r\n")


def vevent(*lines):
    return "\r\n".join(("BEGIN:VEVENT",) + lines + ("END:VEVENT",)) + "\r\n"


def epoch(y, mo, d, h=0, mi=0, tz=BERLIN):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp())


class VCards(unittest.TestCase):
    def test_read(self):
        c = agent.contact_from_vcard(CARD)
        self.assertEqual((c["uid"], c["name"], c["given"], c["family"]),
                         ("abc-1", "Anna Example", "Anna", "Example"))
        self.assertEqual(c["phones"], [{"type": "mobile", "value": "+49 155 50000001"},
                                       {"type": "work", "value": "030 1234"}])
        self.assertEqual(c["emails"], ["anna@example.org"])
        self.assertEqual(c["birthday"], "1990-12-24")
        self.assertEqual(c["note"], "Zeile 1\nZeile 2")
        self.assertEqual(agent.AVATARS[c["avatar"]], ("data", PNG))

    def test_change_keeps_what_the_editor_does_not_own(self):
        c = agent.contact_from_vcard(CARD)
        c["given"] = "Anna-Lena"
        c["phones"] = [{"type": "home", "value": "040 999"}]
        c["note"] = "a;b,c"
        out = agent.vcard_from_contact(c, CARD)
        self.assertIn("UID:abc-1", out)
        self.assertIn('X-EVOLUTION-WEBDAV-ETAG:"etag-7"', out)
        self.assertIn("ADR;TYPE=HOME:;;Weg 1;Stadt;;12345;", out)
        self.assertIn("FN:Anna-Lena Example", out)
        self.assertIn("N:Example;Anna-Lena;;;", out)
        self.assertIn("TEL;TYPE=HOME,VOICE:040 999", out)
        self.assertNotIn("030 1234", out)
        self.assertIn("NOTE:a\\;b\\,c", out)
        self.assertIn("PHOTO;ENCODING=b", out)            # kept: photo=None
        again = agent.contact_from_vcard(out)
        self.assertEqual(again["note"], "a;b,c")
        self.assertEqual(again["phones"], [{"type": "home", "value": "040 999"}])

    def test_photo_removed_and_set(self):
        c = agent.contact_from_vcard(CARD)
        self.assertNotIn("PHOTO", agent.vcard_from_contact(c, CARD, photo=""))
        out = agent.vcard_from_contact(c, CARD, photo=("image/jpeg", b"\xff\xd8jpeg"))
        self.assertIn("PHOTO;ENCODING=b;TYPE=JPEG:", out)
        for line in out.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75, line[:30])

    def test_new_contact(self):
        out = agent.vcard_from_contact({"given": "", "family": "", "org": "Example Ltd",
                                        "phones": [{"type": "mobile", "value": "0155"}]})
        self.assertTrue(out.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\n"))
        self.assertIn("FN:Example Ltd", out)
        self.assertIn("ORG:Example Ltd", out)
        self.assertNotIn("UID", out)


class Recurrence(unittest.TestCase):
    def occ(self, rule, start, until, ex=()):
        return list(agent.recurrences(start, rule, until, ex))

    def test_yearly_birthday(self):
        days = self.occ("FREQ=YEARLY", dt.date(1990, 12, 24), dt.date(2027, 1, 1))
        self.assertEqual(days[-2:], [dt.date(2025, 12, 24), dt.date(2026, 12, 24)])
        self.assertEqual(len(days), 37)

    def test_leap_day_only_in_leap_years(self):
        days = self.occ("FREQ=YEARLY", dt.date(2024, 2, 29), dt.date(2033, 1, 1))
        self.assertEqual([d.year for d in days], [2024, 2028, 2032])

    def test_weekly_byday_count(self):
        start = dt.datetime(2026, 10, 5, 18, 0, tzinfo=BERLIN)      # a Monday
        got = self.occ("FREQ=WEEKLY;BYDAY=MO,WE;COUNT=5", start, dt.datetime(2027, 1, 1))
        self.assertEqual([g.day for g in got], [5, 7, 12, 14, 19])

    def test_weekly_keeps_local_time_over_dst(self):
        start = dt.datetime(2026, 10, 19, 10, 0, tzinfo=BERLIN)
        got = self.occ("FREQ=WEEKLY", start, dt.datetime(2026, 11, 3))
        self.assertEqual([g.hour for g in got], [10, 10, 10])
        self.assertEqual(got[1].utcoffset(), dt.timedelta(hours=1))   # winter time

    def test_monthly_second_tuesday_until(self):
        start = dt.datetime(2026, 10, 13, 9, 0, tzinfo=BERLIN)
        got = self.occ("FREQ=MONTHLY;BYDAY=2TU;UNTIL=20261231T235959Z", start,
                       dt.datetime(2027, 6, 1))
        self.assertEqual([(g.month, g.day) for g in got], [(10, 13), (11, 10), (12, 8)])

    def test_monthly_last_day(self):
        got = self.occ("FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=3", dt.date(2026, 1, 31),
                       dt.date(2027, 1, 1))
        self.assertEqual(got, [dt.date(2026, 1, 31), dt.date(2026, 2, 28),
                               dt.date(2026, 3, 31)])

    def test_exdate_and_interval(self):
        start = dt.datetime(2026, 10, 1, 8, 0, tzinfo=BERLIN)
        got = self.occ("FREQ=DAILY;INTERVAL=2", start, dt.datetime(2026, 10, 9, 23),
                       ex=[dt.datetime(2026, 10, 5, 8, 0)])
        self.assertEqual([g.day for g in got], [1, 3, 7, 9])


class Events(unittest.TestCase):
    RANGE = (epoch(2026, 10, 1), epoch(2026, 11, 1))

    def test_expand_series_override_cancelled(self):
        series = vevent("UID:s1", "SUMMARY:Sport", "DTSTART;TZID=Europe/Berlin:20261005T180000",
                        "DTEND;TZID=Europe/Berlin:20261005T190000", "RRULE:FREQ=WEEKLY")
        moved = vevent("UID:s1", "SUMMARY:Sport (later)",
                       "RECURRENCE-ID;TZID=Europe/Berlin:20261012T180000",
                       "DTSTART;TZID=Europe/Berlin:20261012T200000",
                       "DTEND;TZID=Europe/Berlin:20261012T210000")
        gone = vevent("UID:c1", "SUMMARY:Abgesagt", "STATUS:CANCELLED",
                      "DTSTART:20261010T100000Z", "DTEND:20261010T110000Z")
        allday = vevent("UID:a1", "SUMMARY:Holiday", "DTSTART;VALUE=DATE:20261030",
                        "DTEND;VALUE=DATE:20261103")
        got = agent.expand_events([series + moved, gone, allday], *self.RANGE)
        sport = sorted((s for ev, s, e, rid in got if ev["uid"] == "s1"),
                       key=lambda s: s.timestamp())
        self.assertEqual([(s.day, s.hour) for s in sport],
                         [(5, 18), (12, 20), (19, 18), (26, 18)])
        self.assertNotIn("c1", [ev["uid"] for ev, *_ in got])
        urlaub = [(s, e) for ev, s, e, rid in got if ev["uid"] == "a1"]
        self.assertEqual(urlaub, [(dt.date(2026, 10, 30), dt.date(2026, 11, 3))])

    def test_alarm_minutes(self):
        ev = agent.event_from_lines(agent.components(vevent(
            "UID:x", "DTSTART:20261005T180000Z", "BEGIN:VALARM", "ACTION:DISPLAY",
            "TRIGGER;RELATED=START:-PT15M", "END:VALARM"), "VEVENT")[0])
        self.assertEqual(ev["alarm"], 15)

    def test_new_event_in_local_zone(self):
        with mock.patch.object(agent, "local_zone_name", return_value="Europe/Berlin"):
            text = agent.vevent_from_fields({"summary": "Doctor", "location": "Practice",
                                             "allday": False,
                                             "start": epoch(2026, 10, 7, 9),
                                             "end": epoch(2026, 10, 7, 10), "alarm": 30})
        self.assertIn("DTSTART;TZID=Europe/Berlin:20261007T090000", text)
        self.assertIn("DTEND;TZID=Europe/Berlin:20261007T100000", text)
        self.assertIn("TRIGGER;RELATED=START:-PT30M", text)
        self.assertIn("UID:", text)
        self.assertIn("SEQUENCE:0", text)

    def test_edit_keeps_series_alarms_and_moves_it(self):
        original = vevent("UID:s1", "SUMMARY:Sport", "SEQUENCE:2", "ATTENDEE:mailto:x@y",
                          "DTSTART;TZID=Europe/Berlin:20261005T180000",
                          "DTEND;TZID=Europe/Berlin:20261005T190000", "RRULE:FREQ=WEEKLY",
                          "BEGIN:VALARM", "TRIGGER:-PT10M", "END:VALARM")
        # the occurrence on the 19th moves an hour later
        text = agent.vevent_from_fields(
            {"summary": "Sport!", "allday": False, "start": epoch(2026, 10, 19, 19),
             "end": epoch(2026, 10, 19, 20), "alarm": "keep"},
            original, shift=(epoch(2026, 10, 19, 18), epoch(2026, 10, 19, 19)))
        self.assertIn("DTSTART;TZID=Europe/Berlin:20261005T190000", text)
        self.assertIn("DTEND;TZID=Europe/Berlin:20261005T200000", text)
        self.assertIn("RRULE:FREQ=WEEKLY", text)
        self.assertIn("ATTENDEE:mailto:x@y", text)
        self.assertIn("TRIGGER:-PT10M", text)
        self.assertIn("SEQUENCE:3", text)
        self.assertIn("SUMMARY:Sport!", text)
        self.assertEqual(text.count("SUMMARY"), 1)

    def test_alarm_replaced_or_removed(self):
        original = vevent("UID:e", "DTSTART:20261005T180000Z", "DTEND:20261005T190000Z",
                          "BEGIN:VALARM", "TRIGGER:-PT10M", "END:VALARM")
        f = {"summary": "x", "allday": False, "start": epoch(2026, 10, 5, 20, tz=ZoneInfo("UTC")),
             "end": epoch(2026, 10, 5, 21, tz=ZoneInfo("UTC"))}
        none = agent.vevent_from_fields(dict(f, alarm=None), original)
        self.assertNotIn("VALARM", none)
        self.assertIn("DTSTART:20261005T200000Z", none)      # stays in UTC
        hour = agent.vevent_from_fields(dict(f, alarm=60), original)
        self.assertEqual(hour.count("BEGIN:VALARM"), 1)
        self.assertIn("-PT60M", hour)

    def test_allday(self):
        text = agent.vevent_from_fields({"summary": "Holiday", "allday": True,
                                         "start": "2026-10-30", "end": "2026-11-03",
                                         "alarm": None})
        self.assertIn("DTSTART;VALUE=DATE:20261030", text)
        self.assertIn("DTEND;VALUE=DATE:20261103", text)

    def test_exdate_like_dtstart(self):
        lines = agent.components(vevent("UID:s", "DTSTART;TZID=Europe/Berlin:20261005T180000",
                                        "RRULE:FREQ=WEEKLY"), "VEVENT")[0]
        out = agent.add_exdate(lines, epoch(2026, 10, 12, 18))
        self.assertIn("EXDATE;TZID=Europe/Berlin:20261012T180000", out)
        lines = agent.components(vevent("UID:b", "DTSTART;VALUE=DATE:19901224",
                                        "RRULE:FREQ=YEARLY"), "VEVENT")[0]
        self.assertIn("EXDATE;VALUE=DATE:20261224", agent.add_exdate(lines, "2026-12-24"))


class CallHistory(unittest.TestCase):
    def test_read(self):
        home = Home()
        self.addCleanup(home.cleanup)
        path = os.path.join(home.dir, "records.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, target TEXT, inbound INTEGER,"
                   " start BLOB, answered BLOB, end BLOB, protocol TEXT)")
        db.execute("INSERT INTO calls VALUES (1, '+4915550000001', 1, '2026-10-07T06:00:00.5Z',"
                   " '2026-10-07T06:00:05Z', '2026-10-07T06:01:05Z', 'tel')")
        db.execute("INSERT INTO calls VALUES (2, '+4915550000002', 1, '2026-10-07T07:00:00Z',"
                   " NULL, '2026-10-07T07:00:20Z', 'tel')")
        db.commit()
        db.close()
        calls = agent.call_history(path=path, book=None)
        self.assertEqual([c["id"] for c in calls], [2, 1])
        self.assertFalse(calls[0]["answered"])
        self.assertEqual(calls[1]["duration"], 60)
        self.assertEqual(calls[1]["start"], dt.datetime(2026, 10, 7, 6, 0, 0, 500000,
                                                         tzinfo=ZoneInfo("UTC")).timestamp())


@unittest.skipUnless(os.environ.get("DBUS_SESSION_BUS_ADDRESS"), "no session bus")
class OverEDS(unittest.TestCase):
    """The agent changing contacts and appointments through EDS' D-Bus API."""

    def setUp(self):
        from .fake_eds import FakeEDS
        self.eds = FakeEDS()
        self.addCleanup(self.eds.close)
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        patcher = mock.patch.dict(os.environ, self.home.env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dev = Device({"id": "t", "host": "phone", "user": "me"})
        self.addCleanup(self.dev.stop)
        self.dev.start()
        self.assertTrue(run_loop_until(lambda: self.dev.online, 15))

    def ask(self, cmd, args=None):
        box = {}
        self.dev.request(cmd, args, lambda r, e: box.update(r=r, e=e))
        self.assertTrue(run_loop_until(lambda: box, 15), cmd)
        self.assertIsNone(box["e"], "%s: %s" % (cmd, box["e"]))
        return box["r"]

    def test_sources(self):
        sources = self.ask("pim.sources")
        self.assertEqual(sorted((s["kind"], s["name"]) for s in sources),
                         [("calendar", "Personal"), ("contacts", "Contacts")])
        self.assertTrue(all(s["writable"] for s in sources))
        self.assertEqual({s["account"] for s in sources}, {"me@cloud.example"})

    def test_contact_add_change_delete(self):
        from .fake_eds import BOOK_UID
        uid = self.ask("contacts.save", {"source": BOOK_UID, "contact": {
            "given": "Carla", "family": "Newman", "org": "", "note": "",
            "phones": [{"type": "mobile", "value": "0155 50000003"}], "emails": [],
            "birthday": "1985-05-01"},
            "photo": {"mime": "image/png", "data": base64.b64encode(PNG).decode()}})["uid"]
        contacts = self.ask("contacts.list")
        self.assertEqual([(c["name"], c["uid"]) for c in contacts], [("Carla Newman", uid)])
        self.assertIsNotNone(contacts[0]["avatar"])
        self.assertEqual(contacts[0]["birthday"], "1985-05-01")

        self.ask("contacts.save", {"source": BOOK_UID, "uid": uid, "contact": dict(
            contacts[0], given="Carla Maria"), "photo": None})
        changed = self.ask("contacts.list")[0]
        self.assertEqual(changed["name"], "Carla Maria Newman")
        self.assertIsNone(changed["avatar"])
        self.assertIn("UID:" + uid, self.eds.contacts[uid])

        self.ask("contacts.delete", {"source": BOOK_UID, "uid": uid})
        self.assertEqual(self.ask("contacts.list"), [])

    def test_event_add_skip_one_delete(self):
        from .fake_eds import CAL_UID
        start = int(time.time()) // 3600 * 3600 + 86400
        uid = self.ask("calendar.save", {"source": CAL_UID, "event": {
            "summary": "Sport", "location": "Gym", "description": "", "allday": False,
            "start": start, "end": start + 3600, "alarm": 15}})["uid"]
        self.eds.events[uid] = self.eds.events[uid].replace(
            "END:VEVENT", "RRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT")
        rng = {"start": start - 3600, "end": start + 5 * 86400}
        events = self.ask("calendar.events", rng)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["summary"], "Sport")
        self.assertEqual(events[0]["alarm"], 15)
        self.assertTrue(events[0]["recurring"])
        self.assertEqual(events[0]["color"], "#62a0ea")

        self.ask("calendar.delete", {"source": CAL_UID, "uid": uid, "rid": events[1]["rid"],
                                     "scope": "this", "start": events[1]["start"]})
        left = self.ask("calendar.events", rng)
        self.assertEqual([e["start"] for e in left], [events[0]["start"], events[2]["start"]])

        # changing the series through its last day moves all of it by an hour
        self.ask("calendar.save", {"source": CAL_UID, "uid": uid, "old_source": CAL_UID,
                                   "occurrence_start": left[1]["start"], "event": {
                                       "summary": "Sport", "allday": False, "alarm": "keep",
                                       "start": left[1]["start"] + 3600,
                                       "end": left[1]["end"] + 3600}})
        moved = self.ask("calendar.events", rng)
        self.assertEqual([e["start"] for e in moved],
                         [events[0]["start"] + 3600, events[2]["start"] + 3600])

        self.ask("calendar.delete", {"source": CAL_UID, "uid": uid, "rid": "",
                                     "scope": "all", "start": start})
        self.assertEqual(self.ask("calendar.events", rng), [])


if __name__ == "__main__":
    unittest.main()
