# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The file browser: the agent's file commands, transfers over the stand-in
ssh (which runs the phone's side here), and the page. Everything lives in
throw-away folders; nothing is opened on the desktop."""

import errno
import hashlib
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, Gtk  # noqa: E402

from phonebridge import agent, config, files  # noqa: E402

from .support import Home, run_loop_until  # noqa: E402

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def picture(path, size=300):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, size, size // 2)
    pb.fill(0x3584e4ff)
    pb.savev(path, "png", [], [])


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phonebridge-test-files-")
        self.addCleanup(shutil.rmtree, self.dir, True)


class AgentFiles(Tmp):
    def test_list(self):
        write(os.path.join(self.dir, "a.txt"), b"hello")
        write(os.path.join(self.dir, ".hidden"))
        os.mkdir(os.path.join(self.dir, "Folder"))
        os.symlink("/nonexistent/target", os.path.join(self.dir, "broken"))
        os.symlink(os.path.join(self.dir, "Folder"), os.path.join(self.dir, "to-folder"))
        result = agent.cmd_files_list(None, {"path": self.dir + "/"})
        self.assertEqual(result["path"], self.dir)
        self.assertEqual(result["parent"], os.path.dirname(self.dir))
        self.assertTrue(result["writable"])
        by = {e["name"]: e for e in result["entries"]}
        self.assertEqual(set(by), {"a.txt", ".hidden", "Folder", "broken", "to-folder"})
        self.assertEqual((by["a.txt"]["size"], by["a.txt"]["dir"]), (5, False))
        self.assertTrue(by["Folder"]["dir"])
        self.assertTrue(by["to-folder"]["dir"] and by["to-folder"]["link"])
        self.assertEqual((by["broken"]["dir"], by["broken"]["mtime"]), (False, 0))
        self.assertIsNone(agent.cmd_files_list(None, {"path": "/"})["parent"])

    def test_list_limits_and_errors(self):
        for i in range(5):
            write(os.path.join(self.dir, "f%d" % i))
        with mock.patch.object(agent, "FILES_MAX", 3):
            result = agent.cmd_files_list(None, {"path": self.dir})
        self.assertEqual((len(result["entries"]), result["truncated"]), (3, True))
        with self.assertRaisesRegex(RuntimeError, os.strerror(errno.ENOENT)):
            agent.cmd_files_list(None, {"path": os.path.join(self.dir, "nope")})
        with self.assertRaisesRegex(RuntimeError, "absolute"):
            agent.cmd_files_list(None, {"path": "relative/path"})

    def test_places(self):
        for d in ("Documents", "Pictures"):
            os.mkdir(os.path.join(self.dir, d))
        with mock.patch.object(agent, "FILES_HOME", self.dir):
            result = agent.cmd_files_places(None, {})
            self.assertEqual(agent.cmd_files_list(None, {})["path"], self.dir)  # home
        self.assertEqual(result["home"], self.dir)
        self.assertEqual({p["id"]: p["path"] for p in result["places"]},
                         {"documents": os.path.join(self.dir, "Documents"),
                          "pictures": os.path.join(self.dir, "Pictures")})

    def test_mkdir_rename_delete(self):
        made = agent.cmd_files_mkdir(None, {"path": self.dir, "name": " New "})["path"]
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "New")))
        write(os.path.join(made, "inside.txt"))
        for bad in ("", "..", "a/b", "x\0"):
            with self.assertRaisesRegex(RuntimeError, "invalid name"):
                agent.cmd_files_mkdir(None, {"path": self.dir, "name": bad})
        write(os.path.join(self.dir, "taken"))
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            agent.cmd_files_rename(None, {"path": made, "name": "taken"})
        moved = agent.cmd_files_rename(None, {"path": made, "name": "Renamed"})["path"]
        self.assertTrue(os.path.isfile(os.path.join(moved, "inside.txt")))
        result = agent.cmd_files_delete(None, {"paths": [moved, os.path.join(self.dir, "taken"),
                                                         os.path.join(self.dir, "gone")]})
        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual([f["path"] for f in result["failed"]], [os.path.join(self.dir, "gone")])

    def test_delete_never_the_home_or_root(self):
        with mock.patch.object(agent, "FILES_HOME", self.dir):
            result = agent.cmd_files_delete(None, {"paths": [self.dir, "/", agent.HOME]})
        self.assertEqual([f["error"] for f in result["failed"]], ["not allowed"] * 3)
        self.assertTrue(os.path.isdir(self.dir))

    def test_thumbnails(self):
        pic = os.path.join(self.dir, "photo.png")
        picture(pic)
        write(os.path.join(self.dir, "notes.txt"), b"not a picture")
        thumbs = os.path.join(self.dir, "thumbs")
        with mock.patch.object(agent, "THUMBNAILS", thumbs):
            made = agent.thumbnail(pic)                         # made here, 128 wide
            pb = GdkPixbuf.Pixbuf.new_from_stream(
                Gio.MemoryInputStream.new_from_data(made, None), None)
            self.assertEqual((pb.get_width(), pb.get_height()), (128, 64))
            self.assertIsNone(agent.thumbnail(os.path.join(self.dir, "notes.txt")))
            # the phone's own thumbnail, when it is not older than the file
            digest = hashlib.md5(Gio.File.new_for_path(pic).get_uri().encode()).hexdigest()
            write(os.path.join(thumbs, "normal", digest + ".png"), b"PNG-from-the-phone")
            self.assertEqual(agent.thumbnail(pic), b"PNG-from-the-phone")
            os.utime(pic, (os.path.getmtime(pic) + 100,) * 2)  # the file changed since
            self.assertNotEqual(agent.thumbnail(pic), b"PNG-from-the-phone")
            with mock.patch.object(agent, "THUMB_FILE_MAX", 10):
                self.assertIsNone(agent.thumbnail(pic))         # too large to make one
            out = agent.cmd_files_thumbs(None, {"paths": [pic, os.path.join(self.dir, "x")]})
        self.assertTrue(out[pic])
        self.assertIsNone(out[os.path.join(self.dir, "x")])


class Helpers(Tmp):
    def test_unique_path(self):
        write(os.path.join(self.dir, "a.txt"))
        write(os.path.join(self.dir, "a (2).txt"))
        os.mkdir(os.path.join(self.dir, "dir.d"))
        self.assertEqual(files.unique_path(os.path.join(self.dir, "b.txt")),
                         os.path.join(self.dir, "b.txt"))
        self.assertEqual(files.unique_path(os.path.join(self.dir, "a.txt")),
                         os.path.join(self.dir, "a (3).txt"))
        self.assertEqual(files.unique_path(os.path.join(self.dir, "dir.d")),
                         os.path.join(self.dir, "dir.d (2)"))

    def test_open_cache(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.dir}):
            p = files.open_path("dev", "/home/furios/Documents/letter.odt")
            self.assertTrue(p.startswith(files.cache_dir()))
            self.assertEqual(os.path.basename(p), "letter.odt")
            self.assertNotEqual(p, files.open_path("other", "/home/furios/Documents/letter.odt"))
            write(p)
            old = os.path.dirname(p)
            os.utime(old, (1, 1))
            files.clean_open_cache()
            self.assertFalse(os.path.exists(old))

    def test_incomplete_upload_leaves_the_old_file(self):
        """The phone's side of an upload: fewer bytes than announced (the
        connection broke) - the old file stays, nothing half is left."""
        target = os.path.join(self.dir, "doc.txt")
        write(target, b"old")
        dev = types.SimpleNamespace(info={"host": "h", "user": "u"}, password=None)
        t = files.Transfer(dev, "upload", target, "/unused", size=10)
        t.total = 10
        p = subprocess.run(["sh", "-c", t.remote_command()], input=b"12345",
                           capture_output=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(b"incomplete", p.stderr)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"old")
        self.assertEqual(os.listdir(self.dir), ["doc.txt"])
        subprocess.run(["sh", "-c", t.remote_command()], input=b"0123456789", check=True)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"0123456789")


class Transfers(Tmp):
    """Real transfers through tests/fakebin/ssh, which runs the phone's
    command here."""

    def setUp(self):
        super().setUp()
        self.phone = os.path.join(self.dir, "phone")
        self.pc = os.path.join(self.dir, "pc")
        os.makedirs(self.phone)
        os.makedirs(self.pc)
        self.dev = types.SimpleNamespace(info={"id": "t", "host": "phone", "user": "me"},
                                         password=None)

    def run_transfer(self, *args, **kw):
        t = files.Transfer(self.dev, *args, **kw)
        result, progress = [], []
        t.connect("finished", lambda t, error: result.append(error))
        t.connect("progress", lambda t, done, total: progress.append((done, total)))
        t.start()
        self.assertTrue(run_loop_until(lambda: result, 20))
        return t, result[0], progress

    def test_file_both_ways(self):
        data = os.urandom(700 * 1024)
        write(os.path.join(self.phone, "big.bin"), data)
        local = os.path.join(self.pc, "big.bin")
        t, error, progress = self.run_transfer("download", os.path.join(self.phone, "big.bin"),
                                               local, size=len(data))
        self.assertIsNone(error)
        with open(local, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(progress[-1], (len(data), len(data)))
        self.assertEqual(os.listdir(self.pc), ["big.bin"])          # no .part left

        write(os.path.join(self.phone, "big.bin"), b"older")
        t, error, _p = self.run_transfer("upload", os.path.join(self.phone, "big.bin"), local)
        self.assertIsNone(error)
        with open(os.path.join(self.phone, "big.bin"), "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(os.listdir(self.phone), ["big.bin"])

    def test_names_that_need_quoting(self):
        name = "it's a \"file\" $(touch x) ;.txt"
        write(os.path.join(self.pc, name), b"q")
        t, error, _p = self.run_transfer("upload", os.path.join(self.phone, name),
                                         os.path.join(self.pc, name))
        self.assertIsNone(error)
        self.assertEqual(os.listdir(self.phone), [name])
        self.assertFalse(os.path.exists("x"))

    def test_folders_both_ways(self):
        write(os.path.join(self.pc, "Album", "a.jpg"), b"a" * 1000)
        write(os.path.join(self.pc, "Album", "sub", "b.jpg"), b"b" * 2000)
        t, error, _p = self.run_transfer("upload", os.path.join(self.phone, "Album"),
                                         os.path.join(self.pc, "Album"), is_dir=True,
                                         size=files.local_size(os.path.join(self.pc, "Album")))
        self.assertIsNone(error)
        self.assertEqual(sorted(os.listdir(self.phone)), ["Album"])     # no hidden leftovers
        with open(os.path.join(self.phone, "Album", "sub", "b.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"b" * 2000)

        local = os.path.join(self.pc, "Album (2)")
        t, error, _p = self.run_transfer("download", os.path.join(self.phone, "Album"), local,
                                         is_dir=True)
        self.assertIsNone(error)
        self.assertEqual(sorted(os.listdir(local)), ["a.jpg", "sub"])
        self.assertEqual(sorted(os.listdir(self.pc)), ["Album", "Album (2)"])

    def test_failures(self):
        local = os.path.join(self.pc, "missing.txt")
        t, error, _p = self.run_transfer("download", os.path.join(self.phone, "missing.txt"),
                                         local)
        self.assertTrue(error)                       # cat's own words, in any language
        self.assertEqual(os.listdir(self.pc), [])
        t, error, _p = self.run_transfer("upload", os.path.join(self.phone, "x"),
                                         os.path.join(self.pc, "nothing-here"))
        self.assertEqual(error, os.strerror(errno.ENOENT))
        with mock.patch.dict(os.environ, {"FAKE_SSH_FAIL": "1"}):
            write(os.path.join(self.pc, "y"))
            t, error, _p = self.run_transfer("upload", os.path.join(self.phone, "y"),
                                             os.path.join(self.pc, "y"))
        self.assertIn("No route to host", error)

    def test_cancel(self):
        write(os.path.join(self.phone, "old.bin"), b"keep me")
        write(os.path.join(self.pc, "old.bin"), os.urandom(64 * 1024 * 1024))
        t = files.Transfer(self.dev, "upload", os.path.join(self.phone, "old.bin"),
                           os.path.join(self.pc, "old.bin"))
        result = []
        t.connect("finished", lambda t, error: result.append(error))
        t.connect("progress", lambda t, done, total: t.cancel())
        t.start()
        self.assertTrue(run_loop_until(lambda: result, 20))
        self.assertEqual(result, ["cancelled"])
        run_loop_until(lambda: os.listdir(self.phone) == ["old.bin"], 5)
        with open(os.path.join(self.phone, "old.bin"), "rb") as f:
            self.assertEqual(f.read(), b"keep me")


@unittest.skipUnless(HAVE_DISPLAY and Gtk.init_check(), "no display")
class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gi.repository import Adw
        from phonebridge.app import PhoneBridgeApp
        from phonebridge.window import MainWindow
        cls.home = Home()
        cls.addClassCleanup(cls.home.cleanup)
        cls.phone = os.path.join(cls.home.dir, "phone-home")
        cls.pc = os.path.join(cls.home.dir, "pc")
        write(os.path.join(cls.phone, "Documents", "letter.txt"), b"Dear Anna")
        write(os.path.join(cls.phone, ".config", "x"))
        picture(os.path.join(cls.phone, "Pictures", "photo.png"))
        os.makedirs(cls.pc)
        cls.dialogs = []
        cls.patches = [
            mock.patch.dict(os.environ, dict(
                cls.home.env(), PHONEBRIDGE_FILES_HOME=cls.phone,
                PHONEBRIDGE_THUMBNAILS=os.path.join(cls.home.dir, "thumbs"),
                XDG_CACHE_HOME=os.path.join(cls.home.dir, "cache"))),
            mock.patch.object(config, "CONFIG_DIR", cls.home.config),
            mock.patch.object(Adw.AlertDialog, "present",
                              lambda d, parent=None: cls.dialogs.append(d)),
            mock.patch.object(Gtk.Widget, "get_mapped", lambda self: True),
        ]
        for p in cls.patches:
            p.start()
            cls.addClassCleanup(p.stop)
        config.save(dict(config.DEFAULTS, language="en", devices=[
            {"id": "test", "name": "Testphone", "host": "phone", "user": "me"}]))
        cls.app = app = PhoneBridgeApp()
        app.set_application_id("io.github.miscde.PhoneBridge.TestFiles")
        app.send_notification = lambda *a: None
        app.register(None)
        cls.dev = app.devices["test"]
        assert run_loop_until(lambda: cls.dev.online, 20)
        app.window = cls.win = MainWindow(app)
        cls.addClassCleanup(lambda: (cls.win.destroy(), app.do_shutdown()))
        cls.page = cls.win.files
        cls.toasts = []
        app.toast = cls.toasts.append

    def names(self):
        return sorted(self.page.sorted.get_item(i).name
                      for i in range(self.page.sorted.get_n_items()))

    def go(self, path):
        self.page.navigate(path)
        self.assertTrue(run_loop_until(lambda: self.page.path == path, 10))

    def select(self, name):
        for i in range(self.page.sorted.get_n_items()):
            if self.page.sorted.get_item(i).name == name:
                self.page.selection.select_item(i, True)
                return self.page.sorted.get_item(i)
        self.fail(name)

    def answer(self, response, text=None):
        self.assertTrue(run_loop_until(lambda: self.dialogs, 5))
        d = self.dialogs.pop()
        if text is not None:
            d.get_extra_child().set_text(text)
        d.emit("response", response)

    def test_1_home_places_and_hidden(self):
        self.win.show_page("files")
        self.page.load()
        self.assertTrue(run_loop_until(lambda: self.page.path == self.phone, 10))
        self.assertEqual(self.names(), ["Documents", "Pictures"])      # .config hidden
        self.page.acts["hidden"].change_state(GLib_true())
        self.assertEqual(self.names(), [".config", "Documents", "Pictures"])
        self.page.acts["hidden"].change_state(GLib_false())
        self.assertTrue(self.page.place_rows["documents"].get_visible())
        self.assertFalse(self.page.place_rows["music"].get_visible())  # there is none
        self.page.search.set_text("doc")
        self.page.filter.changed(Gtk.FilterChange.DIFFERENT)
        self.assertEqual(self.names(), ["Documents"])
        self.page.search.set_text("")
        self.page.filter.changed(Gtk.FilterChange.DIFFERENT)

    def test_2_navigate_and_thumbnails(self):
        self.go(self.phone)
        self.go(os.path.join(self.phone, "Pictures"))
        self.assertEqual(self.names(), ["photo.png"])
        item = self.page.sorted.get_item(0)
        self.assertTrue(item.wants_thumb)
        # the row on screen asks for its thumbnail
        self.page._shown[item.thumb_key] = Gtk.Image()
        self.page._want_thumb(item)
        self.assertTrue(run_loop_until(lambda: self.page._thumbs.get(item.thumb_key), 10))
        self.page.go_back()
        self.assertTrue(run_loop_until(lambda: self.page.path == self.phone, 10))
        self.go(os.path.join(self.phone, "Documents"))
        self.page.go_up()
        self.assertTrue(run_loop_until(lambda: self.page.path == self.phone, 10))

    def test_3_new_folder_rename_delete(self):
        self.go(self.phone)
        self.page.ask_new_folder()
        self.answer("ok", "Projects")
        self.assertTrue(run_loop_until(lambda: "Projects" in self.names(), 10))
        self.page.selection.unselect_all()
        self.select("Projects")
        self.page.ask_rename()
        self.answer("ok", "Work")
        self.assertTrue(run_loop_until(lambda: "Work" in self.names(), 10))
        self.page.selection.unselect_all()
        self.select("Work")
        self.page.ask_delete()
        self.answer("delete")
        self.assertTrue(run_loop_until(lambda: "Work" not in self.names(), 10))
        self.assertFalse(os.path.exists(os.path.join(self.phone, "Work")))

    def test_4_upload_download_open(self):
        docs = os.path.join(self.phone, "Documents")
        self.go(docs)
        write(os.path.join(self.pc, "letter.txt"), b"new letter")
        write(os.path.join(self.pc, "photos", "1.jpg"), b"1")
        self.page.upload_paths([os.path.join(self.pc, "letter.txt"),
                                os.path.join(self.pc, "photos")])
        self.answer("skip")                     # letter.txt is there: kept
        self.assertTrue(run_loop_until(lambda: "photos" in self.names(), 10))
        with open(os.path.join(docs, "letter.txt"), "rb") as f:
            self.assertEqual(f.read(), b"Dear Anna")
        self.page.upload_paths([os.path.join(self.pc, "letter.txt"),
                                os.path.join(self.pc, "photos")])
        self.answer("replace")
        self.assertTrue(run_loop_until(lambda: "photos (2)" in self.names(), 10))
        with open(os.path.join(docs, "letter.txt"), "rb") as f:
            self.assertEqual(f.read(), b"new letter")

        down = os.path.join(self.pc, "down")
        os.makedirs(down)
        self.page.selection.unselect_all()
        items = [self.select("letter.txt"), self.select("photos")]
        self.page.download(items, down)
        self.assertTrue(run_loop_until(
            lambda: sorted(os.listdir(down)) == ["letter.txt", "photos"], 10))

        launched = []
        with mock.patch.object(self.page, "_launch", launched.append):
            self.page.open_item(items[0])
            self.assertTrue(run_loop_until(lambda: launched, 10))
        with open(launched[0], "rb") as f:
            self.assertEqual(f.read(), b"new letter")
        self.assertTrue(launched[0].startswith(os.path.join(self.home.dir, "cache")))
        self.assertIn(launched[0], self.page._watching)

    def test_5_zoom(self):
        """Ctrl + / - / 0 and Ctrl + wheel: the icons of the list grow and shrink."""
        from gi.repository import Gdk
        from phonebridge import files_page
        page = self.page
        self.go(os.path.join(self.phone, "Pictures"))
        run_loop_until(lambda: page._name_boxes, 5)
        size = lambda: page._name_boxes[0].image.get_pixel_size()  # noqa: E731
        page.set_zoom(files_page.ZOOM_DEFAULT)
        self.assertEqual(size(), 32)
        self.assertFalse(page.acts["zoom-reset"].get_enabled())
        page.group.activate_action("zoom-in", None)
        self.assertEqual(size(), 48)
        self.assertEqual(config.load()["files_zoom"], files_page.ZOOM_DEFAULT + 1)  # kept
        for _ in range(10):
            page.group.activate_action("zoom-in", None)
        self.assertEqual(size(), 128)
        self.assertFalse(page.acts["zoom-in"].get_enabled())
        page.group.activate_action("zoom-reset", None)
        self.assertEqual(size(), 32)

        ctrl = Gdk.ModifierType.CONTROL_MASK
        self.assertFalse(page.zoom_scroll(-1, 0))                  # no Ctrl: it scrolls
        self.assertTrue(page.zoom_scroll(-1, ctrl))                # wheel up: larger
        self.assertEqual(size(), 48)
        for _ in range(4):
            page.zoom_scroll(0.3, ctrl)                            # touchpad: adds up
        self.assertEqual(size(), 32)
        for _ in range(10):
            page.zoom_scroll(1, ctrl)
        self.assertEqual(size(), 16)
        self.assertFalse(page.acts["zoom-out"].get_enabled())
        page.set_zoom(files_page.ZOOM_DEFAULT)


def GLib_true():
    from gi.repository import GLib
    return GLib.Variant("b", True)


def GLib_false():
    from gi.repository import GLib
    return GLib.Variant("b", False)


if __name__ == "__main__":
    unittest.main()
