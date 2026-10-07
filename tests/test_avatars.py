# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Profile pictures and names: from the address book and from chatty,
handed to the PC by key and cached there."""

import base64
import os
import unittest
from unittest import mock

from phonebridge import agent
from phonebridge.connection import Device

from .support import ANNA, BERND, GROUP, PNG, Home, make_addressbook, run_loop_until

B64 = base64.b64encode(PNG).decode()
CARDS = [
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Anna Beispiel\r\nTEL;TYPE=CELL:0155 50000001\r\n"
    "PHOTO;ENCODING=b;TYPE=PNG:" + B64[:30] + "\r\n " + B64[30:] + "\r\nEND:VCARD",
    "BEGIN:VCARD\r\nVERSION:4.0\r\nN:Muster;Bernd;;;\r\nTEL:tel:+4915550000002\r\n"
    "PHOTO:data:image/png;base64," + B64 + "\r\nEND:VCARD",
]


class VCard(unittest.TestCase):
    def test_folded_base64_photo(self):
        name, numbers, photo = agent.parse_vcard(CARDS[0])
        self.assertEqual(name, "Anna Beispiel")
        self.assertEqual(numbers, ["0155 50000001"])
        self.assertEqual(photo, ("data", PNG))

    def test_vcard4(self):
        name, numbers, photo = agent.parse_vcard(CARDS[1])
        self.assertEqual(name, "Bernd Muster")
        self.assertEqual(numbers, ["+4915550000002"])
        self.assertEqual(photo, ("data", PNG))

    def test_photo_file(self):
        _n, _nums, photo = agent.parse_vcard(
            "BEGIN:VCARD\nFN:X\nPHOTO:file:///home/u/p%20q.jpg\nEND:VCARD")
        self.assertEqual(photo, ("file", "/home/u/p q.jpg"))


class Pictures(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.home.store.add(ANNA, "Hallo")
        self.home.store.add(BERND, "Hi", member_alias="Bernd vom Chat")
        self.home.store.add(GROUP, "Alle", kind=1)
        make_addressbook(self.home.books, CARDS)
        self.book = agent.Book(self.home.books)

    def threads(self):
        return {t["thread"]: t for t in agent.list_threads(
            store=self.home.store_path, sent=self.home.sent, book=self.book)}

    def test_names_and_pictures_from_contacts(self):
        t = self.threads()
        self.assertEqual(t[ANNA]["title"], "Anna Beispiel")      # chatty had none
        self.assertEqual(t[BERND]["title"], "Bernd vom Chat")    # chatty's wins
        self.assertIsNotNone(t[ANNA]["avatar"])
        self.assertEqual(agent.AVATARS[t[ANNA]["avatar"]], ("data", PNG))
        self.assertIsNone(t[GROUP]["avatar"])

    def test_same_picture_same_key(self):
        t = self.threads()
        self.assertEqual(t[ANNA]["avatar"], t[BERND]["avatar"])

    def test_chatty_picture_wins(self):
        path = os.path.join(self.home.dir, "anna.png")
        with open(path, "wb") as f:
            f.write(PNG + b"\0")
        self.home.store.set_avatar(ANNA, path)
        key = self.threads()[ANNA]["avatar"]
        self.assertEqual(agent.AVATARS[key], ("file", path))
        self.assertEqual(agent.picture_bytes(agent.AVATARS[key]), PNG + b"\0")

    def test_no_book(self):
        t = {x["thread"]: x for x in agent.list_threads(
            store=self.home.store_path, sent=self.home.sent, book=None)}
        self.assertEqual(t[ANNA]["title"], ANNA)
        self.assertIsNone(t[ANNA]["avatar"])

    def test_new_incoming_names_the_contact(self):
        new = agent.new_incoming(0, store=self.home.store_path, book=self.book)
        self.assertEqual(new[0]["title"], "Anna Beispiel")


class OverTheWire(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.home.store.add(ANNA, "Hallo")
        make_addressbook(self.home.books, CARDS)
        patcher = mock.patch.dict(os.environ, self.home.env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.home.cleanup)
        self.dev = Device({"id": "t", "host": "phone", "user": "me"})
        self.addCleanup(self.dev.stop)

    def test_fetch_and_cache(self):
        from phonebridge import avatars
        self.dev.start()
        self.assertTrue(run_loop_until(lambda: self.dev.online, 15))
        box = {}
        self.dev.request("sms.threads", {}, lambda r, e: box.update(r=r))
        self.assertTrue(run_loop_until(lambda: box, 10))
        key = box["r"][0]["avatar"]
        with mock.patch.object(avatars, "CACHE_DIR", self.home.cache):
            cache = avatars.Avatars()
            got = []
            cache.get(self.dev, key, got.append)
            self.assertTrue(run_loop_until(lambda: got, 10))
            self.assertEqual(got[0].get_width(), 2)
            with open(os.path.join(self.home.cache, key), "rb") as f:
                self.assertEqual(f.read(), PNG)
            # from the disk the next time, without asking the phone
            self.dev.stop()
            again = []
            avatars.Avatars().get(self.dev, key, again.append)
            self.assertEqual(len(again), 1)

    def test_unknown_key(self):
        self.dev.start()
        self.assertTrue(run_loop_until(lambda: self.dev.online, 15))
        box = {}
        self.dev.request("avatar", {"key": "0" * 20}, lambda r, e: box.update(e=e))
        self.assertTrue(run_loop_until(lambda: box, 10))
        self.assertEqual(box["e"], "unknown picture")


if __name__ == "__main__":
    unittest.main()
