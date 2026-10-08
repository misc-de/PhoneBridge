# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Search over everything: the phone's messages and files (asked of the
agent), and the dialog with what the app has at once."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from phonebridge import agent, config  # noqa: E402

from .support import ANNA, BERND, Home, Store, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def write(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


class OnThePhone(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_messages(self):
        store = Store(os.path.join(self.dir, "store.db"))
        store.add(ANNA, "The keys are under the mat", at=100)
        store.add(BERND, "50% off at the market", at=200)
        store.add(BERND, "Nothing here", at=300)
        path = store.path
        found = agent.search_messages("KEYS", store=path, sent="/nonexistent")
        self.assertEqual([(m["thread"], m["body"]) for m in found],
                         [(ANNA, "The keys are under the mat")])
        self.assertEqual(len(agent.search_messages("50%", store=path, sent="/x")), 1)
        self.assertEqual(agent.search_messages("_", store=path, sent="/x"), [])  # too short
        self.assertEqual(agent.search_messages("%%", store=path, sent="/x"), [])  # no wildcards

    def test_files(self):
        write(os.path.join(self.dir, "Documents", "Invoice 2026.pdf"))
        write(os.path.join(self.dir, "Documents", "old", "invoice-2025.pdf"))
        write(os.path.join(self.dir, ".hidden", "invoice-secret.pdf"))
        os.makedirs(os.path.join(self.dir, "Invoices"))
        found = agent.search_files("invoice", root=self.dir)
        self.assertTrue(found["complete"])
        self.assertEqual(sorted(e["name"] for e in found["entries"]),
                         ["Invoice 2026.pdf", "Invoices", "invoice-2025.pdf"])
        self.assertTrue(next(e for e in found["entries"] if e["name"] == "Invoices")["dir"])
        limited = agent.search_files("invoice", limit=1, root=self.dir)
        self.assertEqual((len(limited["entries"]), limited["complete"]), (1, False))
        with mock.patch.object(agent, "SEARCH_FILES_SECONDS", -1):
            self.assertFalse(agent.search_files("zzz", root=self.dir)["complete"])


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class Dialog(unittest.TestCase):
    def test_search_and_go(self):
        from phonebridge.app import PhoneBridgeApp
        from phonebridge.window import MainWindow
        home = Home()
        self.addCleanup(home.cleanup)
        home.store.add(ANNA, "The keys are under the mat", member_alias="Anna")
        write(os.path.join(home.dir, "phone-home", "Documents", "keys-list.txt"))
        for p in (mock.patch.dict(os.environ, home.env()),
                  mock.patch.object(config, "CONFIG_DIR", home.config)):
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestSearch")
        app.send_notification = lambda *a: None
        app.register(None)
        self.addCleanup(app.do_shutdown)
        dev = app.devices["test"]
        self.assertTrue(run_loop_until(lambda: dev.online and app.threads.get("test"), 20))
        app.window = win = MainWindow(app)
        self.addCleanup(win.destroy)
        app.show_window = lambda page=None: (page and win.show_page(page)) or win
        win.contacts.contacts = [{"source": "s", "uid": "k", "name": "Keira Example",
                                  "org": "", "phones": [{"value": "+4915550000009",
                                                         "type": "mobile"}],
                                  "emails": [], "given": "Keira", "family": "Example",
                                  "birthday": "", "note": "", "book": "Contacts",
                                  "writable": False, "avatar": None}]

        from phonebridge.search import SearchDialog
        dialog = SearchDialog(app)
        self.assertEqual(dialog.stack.get_visible_child_name(), "status")
        dialog.entry.set_text("kei")
        dialog._changed()
        found = dialog.found()
        self.assertEqual([r[0] for r in found["contacts"]], ["Keira Example"])
        dialog.entry.set_text("keys")
        dialog._changed()
        self.assertTrue(run_loop_until(lambda: dialog.found().get("messages")
                                       and dialog.found().get("files"), 10))
        found = dialog.found()
        self.assertEqual(found["messages"][0][1], "The keys are under the mat")
        self.assertEqual(found["files"][0][0], "keys-list.txt")
        self.assertEqual(dialog.stack.get_visible_child_name(), "results")

        # a file: its folder in the files page, the file alone in view
        with mock.patch.object(dialog, "close", lambda: None):
            found["files"][0][3]()
        self.assertEqual(win.current_page(), "files")
        self.assertTrue(run_loop_until(lambda: win.files.path and win.files.path.endswith(
            "Documents"), 10))
        self.assertEqual(win.files.search.get_text(), "keys-list.txt")
        # a message: its conversation
        with mock.patch.object(dialog, "close", lambda: None):
            found["messages"][0][3]()
        self.assertEqual(win.current_page(), "messages")
        self.assertEqual(win.messages.thread, ANNA)

        dialog.entry.set_text("zzzz-nothing")
        dialog._changed()
        self.assertEqual(dialog.status.get_title(), "Nothing found")


if __name__ == "__main__":
    unittest.main()
