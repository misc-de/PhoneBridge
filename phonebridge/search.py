# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Search over everything (Ctrl+K): contacts, conversations, calls and
appointments at once from what the app has; the messages' text and the
phone's files asked of the phone while typing pauses."""

import posixpath
import time

from gi.repository import Adw, GLib, Gtk

from . import text
from .contacts import contact_matches, event_when
from .i18n import _

SHOWN = 6                       # results per kind
DELAY = 300                     # ms of quiet typing before the phone is asked
EVENTS_SPAN = 365               # days back and ahead searched for appointments


class SearchDialog(Adw.Dialog):
    def __init__(self, app):
        super().__init__(title=_("Search"), content_width=640, content_height=600)
        self.app = app
        self.dev = app.active_device()
        self.events = []
        self._serial = 0
        self._timer = 0
        self._remote = {"messages": [], "files": []}

        self.entry = Gtk.SearchEntry(hexpand=True,
                                     placeholder_text=_("Contacts, messages, calls, "
                                                        "appointments, files"))
        self.entry.connect("search-changed", lambda *a: self._changed())
        self.entry.connect("activate", lambda *a: self._activate_first())
        self.entry.connect("stop-search", lambda *a: self.close())
        header = Adw.HeaderBar(title_widget=self.entry)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                               margin_top=12, margin_bottom=18, margin_start=12,
                               margin_end=12)
        self.status = Adw.StatusPage(icon_name="system-search-symbolic",
                                     title=_("Search everything"),
                                     description=_("At least two letters"))
        self.stack = Gtk.Stack()
        self.stack.add_named(Gtk.ScrolledWindow(child=Adw.Clamp(child=self.results),
                                                hscrollbar_policy=Gtk.PolicyType.NEVER),
                             "results")
        self.stack.add_named(self.status, "status")
        self.stack.set_visible_child_name("status")
        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        self.set_child(view)
        self.set_focus(self.entry)
        self._load_events()

    # -- what is searched -----------------------------------------------------------
    def _load_events(self):
        dev = self.dev
        if dev is None or not dev.online:
            return
        now = time.time()

        def got(result, error):
            if error is None:
                self.events = result
                self._show()

        dev.request("calendar.events", {"start": int(now - EVENTS_SPAN * 86400),
                                        "end": int(now + EVENTS_SPAN * 86400)}, got)

    def query(self):
        return self.entry.get_text().strip()

    def _changed(self):
        self._remote = {"messages": [], "files": []}
        self._serial += 1
        self._show()
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if len(self.query()) >= 2:
            self._timer = GLib.timeout_add(DELAY, self._ask_phone)

    def _ask_phone(self):
        self._timer = 0
        dev, serial, q = self.dev, self._serial, self.query()
        if dev is None or not dev.online:
            return False

        def got(kind):
            def back(result, error):
                if serial == self._serial and error is None:
                    self._remote[kind] = result if kind == "messages" else result["entries"]
                    self._show()
            return back

        dev.request("sms.search", {"query": q, "limit": SHOWN * 2}, got("messages"))
        dev.request("files.search", {"query": q, "limit": SHOWN * 2}, got("files"))
        return False

    def found(self):
        """{kind: [(title, subtitle, icon, what to do)]} for the query."""
        q = self.query()
        low = q.casefold()
        app, dev = self.app, self.dev
        out = {}
        if len(q) < 2 or dev is None:
            return out
        win = app.window
        contacts = win.contacts.contacts if win is not None else []
        out["contacts"] = [
            (c["name"] or c["org"] or _("No name"),
             c["phones"][0]["value"] if c["phones"] else (c["emails"] or [""])[0],
             "avatar-default-symbolic", lambda c=c: self._go(lambda: app.window.contacts
                                                              .show_contact(c), "contacts"))
            for c in contacts if contact_matches(c, q)][:SHOWN]
        threads = app.threads.get(dev.id, [])
        conv = [(t["title"], text.activity(t["last"]["time"]) if (t.get("last") or {}).get(
                 "time") else "", "mail-unread-symbolic",
                 lambda t=t: self._go(lambda: app.open_sms(t["thread"])))
                for t in threads if low in (t["title"] or "").casefold()
                or (low.replace(" ", "") and low.replace(" ", "") in t["thread"])]
        names = {t["thread"]: t["title"] for t in threads}
        conv += [((names.get(m["thread"]) or m["thread"]),
                  " ".join((m["body"] or "").split()), "mail-send-symbolic" if m["out"]
                  else "mail-unread-symbolic",
                  lambda m=m: self._go(lambda: app.open_sms(m["thread"])))
                 for m in self._remote["messages"]]
        out["messages"] = conv[:SHOWN]
        # the phone page's list once it was loaded, else the overview's newest
        calls = (win.phone.calls or win.overview.calls) if win is not None else []
        seen, call_rows = set(), []
        for c in calls:
            who = c["name"] or c["number"]
            if not who or c["number"] in seen or not (
                    low in (c["name"] or "").casefold() or low in (c["number"] or "")):
                continue
            seen.add(c["number"])
            call_rows.append((who, text.activity(c["start"]) if c.get("start") else "",
                              "call-start-symbolic",
                              lambda c=c: self._go(lambda: app.window.phone.show_number(
                                  c["number"]), "phone")))
        out["calls"] = call_rows[:SHOWN]
        now = time.time()

        def when(e):
            t = e["start"] if not e["allday"] else time.mktime(time.strptime(e["start"],
                                                                             "%Y-%m-%d"))
            return (t < now - 86400, abs(t - now))        # coming first, the nearest first

        events = [e for e in self.events if low in " ".join(
            (e["summary"] or "", e["location"] or "", e["description"] or "")).casefold()]
        out["events"] = [(e["summary"] or _("(no title)"), event_when(e),
                          "x-office-calendar-symbolic",
                          lambda e=e: self._go(lambda: app.window.calendar.show_event(e),
                                               "calendar"))
                         for e in sorted(events, key=when)][:SHOWN]
        out["files"] = [(f["name"], posixpath.dirname(f["path"]),
                         "folder-symbolic" if f["dir"] else "text-x-generic-symbolic",
                         lambda f=f: self._go(lambda: app.window.files.reveal(f["path"],
                                                                               f["dir"]),
                                              "files"))
                        for f in self._remote["files"]][:SHOWN]
        return out

    # -- showing ---------------------------------------------------------------------
    def _show(self):
        while (child := self.results.get_first_child()) is not None:
            self.results.remove(child)
        found = self.found()
        titles = (("contacts", _("Contacts")), ("messages", _("Messages")),
                  ("calls", _("Calls")), ("events", _("Appointments")), ("files", _("Files")))
        any_found = False
        self._first = None
        for kind, title in titles:
            rows = found.get(kind) or []
            if not rows:
                continue
            any_found = True
            group = Adw.PreferencesGroup(title=title)
            for name, sub, icon, go in rows:
                row = Adw.ActionRow(title=GLib.markup_escape_text(name or ""),
                                    subtitle=GLib.markup_escape_text(sub or ""),
                                    activatable=True)
                row.set_title_lines(1)
                row.set_subtitle_lines(1)
                row.add_prefix(Gtk.Image(icon_name=icon))
                row.connect("activated", lambda r, go=go: go())
                group.add(row)
                self._first = self._first or go
            self.results.append(group)
        if any_found:
            self.stack.set_visible_child_name("results")
        else:
            short = len(self.query()) < 2
            self.status.set_title(_("Search everything") if short else _("Nothing found"))
            self.status.set_description(_("At least two letters") if short else None)
            self.stack.set_visible_child_name("status")

    def _activate_first(self):
        if getattr(self, "_first", None) is not None:
            self._first()

    def _go(self, action, page=None):
        self.close()
        if page:
            self.app.show_window(page)
        action()
