# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""A made-up chatty store and a throw-away home for the tests.
Names and numbers are invented."""

import os
import shutil
import sqlite3
import tempfile
import time

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
  alias TEXT, avatar_id INTEGER, type INTEGER NOT NULL, UNIQUE (username, type));
CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
  password TEXT, enabled INTEGER DEFAULT 0, protocol INTEGER NOT NULL);
CREATE TABLE threads (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  alias TEXT, avatar_id INTEGER, account_id INTEGER NOT NULL, type INTEGER NOT NULL,
  encrypted INTEGER DEFAULT 0, last_read_id INTEGER, visibility INT NOT NULL DEFAULT 0,
  notification INTEGER NOT NULL DEFAULT 1);
CREATE TABLE thread_members (id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER NOT NULL, user_id INTEGER NOT NULL);
CREATE TABLE files (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
  url TEXT NOT NULL UNIQUE, path TEXT, mime_type_id INTEGER, status INT, size INTEGER);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL,
  thread_id INTEGER NOT NULL, sender_id INTEGER, user_alias TEXT, body TEXT NOT NULL,
  body_type INTEGER NOT NULL, direction INTEGER NOT NULL, time INTEGER NOT NULL,
  status INTEGER, encrypted INTEGER DEFAULT 0, preview_id INTEGER, subject TEXT);
"""

ANNA = "+4915550000001"
BERND = "+4915550000002"
GROUP = "+4915550000001,+4915550000002"


class Store:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO users (username, type) VALUES ('me', 1)")
        self.db.execute("INSERT INTO accounts (user_id, protocol) VALUES (1, 1)")
        self.threads = {}
        self.users = {}
        self.db.commit()

    def thread(self, name, alias=None, member_alias=None, kind=0):
        if name not in self.threads:
            cur = self.db.execute(
                "INSERT INTO threads (name, alias, account_id, type) VALUES (?, ?, 1, ?)",
                (name, alias, kind))
            self.threads[name] = cur.lastrowid
            cur = self.db.execute("INSERT INTO users (username, alias, type) VALUES (?, ?, 1)",
                                  (name, member_alias))
            self.users[name] = cur.lastrowid
            self.db.execute("INSERT INTO thread_members (thread_id, user_id) VALUES (?, ?)",
                            (self.threads[name], cur.lastrowid))
            self.db.commit()
        return self.threads[name]

    def set_avatar(self, name, path):
        cur = self.db.execute("INSERT INTO files (url, path) VALUES (?, ?)",
                              ("file://" + path, path))
        self.db.execute("UPDATE users SET avatar_id = ? WHERE id = ?",
                        (cur.lastrowid, self.users[name]))
        self.db.commit()

    def add(self, name, body, incoming=True, at=None, **kw):
        tid = self.thread(name, **kw)
        cur = self.db.execute(
            "INSERT INTO messages (uid, thread_id, sender_id, body, body_type, direction,"
            " time, status) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            ("uid-%f" % time.time(), tid, self.users[name] if incoming else 1, body,
             1 if incoming else -1, int(at if at is not None else time.time()),
             None if incoming else 1))
        self.db.commit()
        return cur.lastrowid


class Home:
    """A temporary directory with the store and the agent's data dir."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="phonebridge-test-")
        self.store_path = os.path.join(self.dir, "chatty-history.db")
        self.data = os.path.join(self.dir, "data")
        self.sent = os.path.join(self.data, "sent.jsonl")
        self.config = os.path.join(self.dir, "config")
        self.books = os.path.join(self.dir, "addressbook")
        self.cache = os.path.join(self.dir, "cache")
        self.store = Store(self.store_path)

    def env(self):
        return {"PHONEBRIDGE_CHATTY_DB": self.store_path,
                "PHONEBRIDGE_DATA": self.data,
                "PHONEBRIDGE_CONFIG": self.config,
                "PHONEBRIDGE_ADDRESSBOOKS": self.books,
                "PHONEBRIDGE_CACHE": self.cache,
                "XDG_CONFIG_HOME": os.path.join(self.dir, "xdg")}

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def run_loop_until(predicate, timeout=10):
    from gi.repository import GLib
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            return False
        ctx.iteration(False)
        time.sleep(0.005)
    return True


# a 2x2 red PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000002000000020802000000fdd49a73"
    "0000001049444154789c63f8cfc000440c100a001fee03fd8b5f14d40000000049"
    "454e44ae426082")


def make_addressbook(root, cards):
    """An evolution-data-server contacts.db with these vCards."""
    import sqlite3 as sq
    d = os.path.join(root, "system")
    os.makedirs(d, exist_ok=True)
    db = sq.connect(os.path.join(d, "contacts.db"))
    db.execute("CREATE TABLE folders (folder_id TEXT)")
    db.execute("INSERT INTO folders VALUES ('folder_id')")
    db.execute("CREATE TABLE folder_id (uid TEXT, is_list INTEGER, vcard TEXT)")
    for n, card in enumerate(cards):
        db.execute("INSERT INTO folder_id VALUES (?, 0, ?)", ("c%d" % n, card))
    db.commit()
    db.close()


def make_cache_book(root, account, cards, deleted=()):
    """A synced address book: <root>/<account>/cache.db with ECacheObjects."""
    import sqlite3 as sq
    d = os.path.join(root, account)
    os.makedirs(d, exist_ok=True)
    db = sq.connect(os.path.join(d, "cache.db"))
    db.execute("CREATE TABLE ECacheObjects (ECacheUID TEXT, ECacheOBJ TEXT,"
               " ECacheState INTEGER, is_list INTEGER)")
    for n, card in enumerate(cards):
        db.execute("INSERT INTO ECacheObjects VALUES (?, ?, ?, 0)",
                   ("c%d" % n, card, 3 if n in deleted else 0))
    db.commit()
    db.close()
    return d
