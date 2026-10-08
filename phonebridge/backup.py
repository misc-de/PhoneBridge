# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Photo backup: what the phone's camera takes comes to this PC by itself -
a minute after the phone connects and every half hour, only while the
phone is on Wi-Fi (no mobile data spent), one file after the other.

What was backed up once is remembered (backup-<phone>.json): a picture
deleted on the PC is not fetched again."""

import json
import os
import re
import time

from gi.repository import Adw, GLib, Gtk

from . import config, files, text
from .i18n import _, n_

EVERY = 30 * 60                 # s between backups
FIRST = 60                      # s after the phone connected
SKIP = re.compile(r"(^\.|\.part$|\.tmp$|~$)")


def state_path(dev_id):
    d = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                     "phonebridge")
    return os.path.join(d, "backup-%s.json" % dev_id)


def on_wifi(dev):
    return bool(((dev.status or {}).get("wifi") or {}).get("ssid"))


class Backup:
    def __init__(self, app):
        self.app = app
        self.running = {}           # device id -> what this run does
        self.listeners = []
        GLib.timeout_add_seconds(EVERY, self._tick)

    # -- settings ---------------------------------------------------------------------
    def settings(self, dev_id):
        return self.app.cfg.setdefault("backup", {}).setdefault(
            dev_id, {"on": False, "target": None, "last": None, "count": 0})

    def target(self, dev):
        s = self.settings(dev.id)
        if s.get("target"):
            return s["target"]
        pictures = (GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
                    or os.path.expanduser("~/Pictures"))
        name = re.sub(r"[/\0]", "_", dev.name).strip() or dev.id
        return os.path.join(pictures, "PhoneBridge", name)

    def set(self, dev, **values):
        self.settings(dev.id).update(values)
        config.save(self.app.cfg)
        self._changed(dev)
        if values.get("on"):
            self.run(dev)

    def _changed(self, dev):
        for listener in list(self.listeners):
            listener(dev)

    # -- when --------------------------------------------------------------------------
    def device_online(self, dev):
        GLib.timeout_add_seconds(FIRST, lambda: self.run(dev, quiet=True) and False)

    def _tick(self):
        for dev in self.app.devices.values():
            self.run(dev, quiet=True)
        return True

    # -- what ------------------------------------------------------------------------------
    def _done(self, dev_id):
        try:
            with open(state_path(dev_id), encoding="utf-8") as f:
                return set(json.load(f))
        except (OSError, ValueError, TypeError):
            return set()

    def _save_done(self, dev_id, done):
        path = state_path(dev_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(done), f)
        os.replace(tmp, path)

    def run(self, dev, quiet=False):
        """quiet: the timer's - only when switched on and on Wi-Fi, and
        nothing told when there was nothing new."""
        s = self.settings(dev.id)
        if dev.id in self.running or not dev.online or (quiet and not s["on"]):
            return False
        if quiet and not on_wifi(dev):
            return False
        run = {"dev": dev, "quiet": quiet, "todo": [], "saved": 0, "failed": 0,
               "done": self._done(dev.id), "target": self.target(dev), "pending": 0}
        self.running[dev.id] = run
        self._changed(dev)
        dev.request("backup.sources", {}, lambda r, e: self._got_sources(run, r, e))
        return True

    def _got_sources(self, run, sources, error):
        if error is not None or not sources:
            self._finish(run, error or (None if run["quiet"] else _("No camera folder on the phone")))
            return
        run["pending"] = len(sources)
        for src in sources:
            run["dev"].request("files.list", {"path": src["path"]},
                               lambda r, e, src=src: self._got_list(run, src, r, e))

    def _got_list(self, run, src, result, error):
        run["pending"] -= 1
        if error is None:
            folder = os.path.basename(src["path"])
            for e in result["entries"]:
                if e["dir"] or SKIP.search(e["name"]):
                    continue
                key = "%s/%s:%d" % (folder, e["name"], e["size"])
                if key in run["done"]:
                    continue
                local = os.path.join(run["target"], e["name"])
                if os.path.isfile(local) and os.path.getsize(local) == e["size"]:
                    run["done"].add(key)        # there already
                    continue
                run["todo"].append((key, os.path.join(result["path"], e["name"]), e))
        if run["pending"] == 0:
            run["todo"].sort(key=lambda t: t[2]["mtime"])
            self._save_done(run["dev"].id, run["done"])
            self._next(run)

    def _next(self, run):
        if not run["todo"]:
            self._finish(run, None)
            return
        dev = run["dev"]
        if not dev.online:
            self._finish(run, "not connected")
            return
        key, remote, entry = run["todo"].pop(0)
        os.makedirs(run["target"], exist_ok=True)
        local = os.path.join(run["target"], entry["name"])
        if os.path.exists(local):               # the same name, another picture
            local = files.unique_path(local)
        t = files.Transfer(dev, "download", remote, local, size=entry["size"])

        def finished(error):
            if error is None:
                run["saved"] += 1
                run["done"].add(key)
                self._save_done(dev.id, run["done"])
            elif error == "cancelled":
                self._finish(run, None)
                return
            else:
                run["failed"] += 1
            self._next(run)

        if self.app.window is not None:
            self.app.window.files.run_transfer(t, finished)
        else:
            t.connect("finished", lambda t, error: finished(error))
            t.start()

    def _finish(self, run, error):
        dev = run["dev"]
        self.running.pop(dev.id, None)
        s = self.settings(dev.id)
        if error is None:
            s["last"] = int(time.time())
            s["count"] = s.get("count", 0) + run["saved"]
            config.save(self.app.cfg)
        if run["saved"]:
            self.app.tell(n_("%d photo or video backed up", "%d photos and videos backed up",
                             run["saved"]))
        elif error is not None and not run["quiet"]:
            self.app.tell(_("Backup: %s") % text.error(error))
        elif not run["quiet"]:
            self.app.tell(_("Nothing new to back up"))
        self._changed(dev)


class BackupGroup(Adw.PreferencesGroup):
    """In the phone's settings: on/off, where to, when last, now."""

    def __init__(self, app):
        super().__init__(title=_("Photo backup"), description=_(
            "New photos and videos of the phone's camera come to this PC by themselves - "
            "every half hour while the phone is on Wi-Fi. What was backed up once is not "
            "fetched again, even when deleted here."))
        self.app = app
        self.dev = None
        self.switch = Adw.SwitchRow(title=_("Back up photos and videos"))
        self.switch.connect("notify::active", self._on_switch)
        self.add(self.switch)
        self.where = Adw.ActionRow(title=_("Saved in"))
        choose = Gtk.Button(label=_("Choose …"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *a: self._choose())
        self.where.add_suffix(choose)
        self.add(self.where)
        self.state = Adw.ActionRow(title=_("Last backup"))
        self.add(self.state)
        self.now = Adw.ButtonRow(title=_("Back up now"))
        self.now.connect("activated", lambda *a: self.dev and app.backup.run(self.dev))
        self.add(self.now)
        self._filling = False
        app.backup.listeners.append(lambda dev: dev is self.dev and self.update())

    def set_device(self, dev):
        self.dev = dev
        self.update()

    def update(self):
        dev = self.dev
        self.set_sensitive(dev is not None)
        if dev is None:
            return
        b = self.app.backup
        s = b.settings(dev.id)
        self._filling = True
        self.switch.set_active(bool(s["on"]))
        self._filling = False
        self.where.set_subtitle(GLib.markup_escape_text(b.target(dev)))
        if dev.id in b.running:
            self.state.set_subtitle(_("Backing up …"))
        elif s.get("last"):
            self.state.set_subtitle(text.activity(s["last"]) + " · " + n_(
                "%d file so far", "%d files so far", s.get("count", 0)))
        else:
            self.state.set_subtitle(_("Not yet"))
        self.now.set_sensitive(dev.online and dev.id not in b.running)

    def _on_switch(self, *args):
        if not self._filling and self.dev is not None:
            self.app.backup.set(self.dev, on=self.switch.get_active())

    def _choose(self):
        dialog = Gtk.FileDialog(title=_("Save photos in"), modal=True)

        def chosen(d, res):
            try:
                folder = d.select_folder_finish(res)
            except GLib.Error:
                return
            if folder is not None and folder.get_path() and self.dev is not None:
                self.app.backup.set(self.dev, target=folder.get_path())

        dialog.select_folder(self.get_root(), None, chosen)
