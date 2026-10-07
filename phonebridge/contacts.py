# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Contacts: the phone's address books (evolution-data-server), to look
up, call, write to, add, change and remove - changes sync to the accounts
the address books belong to (CardDAV, Google ...) like on the phone.

Beside a contact (below it, when the window is narrow): what there is with
the person - calls and voice messages, the last messages, appointments
that name them."""

import base64
import collections
import datetime as dt
import re
import time

from gi.repository import Adw, GdkPixbuf, Gio, GLib, Gtk, Pango

from . import text
from .i18n import N_, _
from .widgets import MONTHS, parse_date, short_date

ACTIVITY_CALLS = 5
ACTIVITY_MESSAGES = 6
ACTIVITY_EVENTS = 6
EVENTS_BACK, EVENTS_AHEAD = 30, 180     # days around today searched for appointments

PHONE_TYPES = (("mobile", N_("Mobile")), ("home", N_("Home")), ("work", N_("Work")),
               ("other", N_("Other")))
SOURCE_NAMES = {"Personal": N_("Personal"), "Contacts": N_("Contacts")}


def source_label(source):
    name = _(SOURCE_NAMES[source["name"]]) if source["name"] in SOURCE_NAMES else source["name"]
    if source.get("account") and source.get("backend") != "local":
        return "%s (%s)" % (name, source["account"])
    return name


def mentions(event, contact, given_counts):
    """Whether an appointment names the person: the full name, or the
    first name alone when no other contact has it."""
    hay = " ".join((event.get("summary") or "", event.get("location") or "",
                    event.get("description") or "")).casefold()
    full = (contact.get("name") or "").strip().casefold()
    if len(full) > 2 and " " in full and full in hay:
        return True
    given = (contact.get("given") or "").strip().casefold()
    return (len(given) > 2 and given_counts.get(given, 0) <= 1
            and re.search(r"\b%s\b" % re.escape(given), hay) is not None)


def event_when(ev):
    if ev["allday"]:
        try:
            d = dt.date.fromisoformat(ev["start"])
        except ValueError:
            return ev["start"]
        return "%s, %s" % (short_date(d), _("All day"))
    return text.when_long(ev["start"])


def birthday_text(iso):
    try:
        d = dt.date.fromisoformat(iso)
    except ValueError:
        return iso
    return _("%(day)d %(month)s %(year)d") % {"day": d.day, "month": _(MONTHS[d.month - 1]),
                                              "year": d.year}


class ContactRow(Gtk.ListBoxRow):
    def __init__(self, app, dev, contact):
        super().__init__()
        self.contact = contact
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        avatar = Adw.Avatar(size=36, text=contact["name"], show_initials=True)
        if contact.get("avatar"):
            app.avatars.get(dev, contact["avatar"], avatar.set_custom_image)
        box.append(avatar)
        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        lines.append(Gtk.Label(label=contact["name"] or _("No name"), xalign=0,
                               ellipsize=Pango.EllipsizeMode.END))
        if contact["phones"]:
            sub = Gtk.Label(label=contact["phones"][0]["value"], xalign=0,
                            ellipsize=Pango.EllipsizeMode.END)
            sub.add_css_class("dim-label")
            sub.add_css_class("caption")
            lines.append(sub)
        box.append(lines)
        self.set_child(box)

    def matches(self, query):
        c = self.contact
        hay = " ".join([c["name"], c["org"]] + [p["value"] for p in c["phones"]]
                       + c["emails"]).lower()
        digits = "".join(ch for ch in query if ch.isdigit())
        if digits and len(digits) >= 3 and any(
                digits in "".join(ch for ch in p["value"] if ch.isdigit())
                for p in c["phones"]):
            return True
        return all(w in hay for w in query.lower().split())


class ContactsPage(Gtk.Box):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.dev = None
        self.contacts = []
        self.sources = []
        self.current = None
        self._loaded_for = None
        self._serial = 0
        self._have = None           # the phone whose contacts are in self.contacts
        self._activity_serial = 0
        self._pending_number = None

        self.search = Gtk.SearchEntry(placeholder_text=_("Search contacts"),
                                      margin_start=8, margin_end=8, margin_top=6,
                                      margin_bottom=6)
        self.search.connect("search-changed", lambda *a: self.list.invalidate_filter())
        self.list = Gtk.ListBox()
        self.list.add_css_class("navigation-sidebar")
        self.list.set_filter_func(lambda row: row.matches(self.search.get_text()))
        self.list.connect("row-selected", self._on_row)
        self.status = Adw.StatusPage(icon_name="x-office-address-book-symbolic")
        self.status.add_css_class("compact")
        self.list_stack = Gtk.Stack()
        self.list_stack.add_named(Gtk.ScrolledWindow(child=self.list, vexpand=True,
                                                     hscrollbar_policy=Gtk.PolicyType.NEVER),
                                  "list")
        self.list_stack.add_named(self.status, "status")
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.append(self.search)
        side.append(self.list_stack)
        side_header = Adw.HeaderBar(show_end_title_buttons=False,
                                    show_start_title_buttons=False)
        self.add_button = Gtk.Button(icon_name="list-add-symbolic",
                                     tooltip_text=_("New contact"))
        self.add_button.connect("clicked", lambda *a: self.edit(None))
        side_header.pack_start(self.add_button)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Reload"))
        refresh.connect("clicked", lambda *a: self.load(force=True))
        side_header.pack_end(refresh)
        side_view = Adw.ToolbarView(content=side)
        side_view.add_top_bar(side_header)
        sidebar = Adw.NavigationPage(title=_("Contacts"), child=side_view)

        self.edit_button = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text=_("Edit"))
        self.edit_button.connect("clicked", lambda *a: self.edit(self.current))
        self.delete_button = Gtk.Button(icon_name="user-trash-symbolic",
                                        tooltip_text=_("Delete"))
        self.delete_button.connect("clicked", lambda *a: self.delete(self.current))
        detail_header = Adw.HeaderBar(show_end_title_buttons=False,
                                      show_start_title_buttons=False)
        detail_header.pack_end(self.delete_button)
        detail_header.pack_end(self.edit_button)
        self.detail = Adw.Bin(vexpand=True)
        self.empty = Adw.StatusPage(icon_name="avatar-default-symbolic",
                                    title=_("Choose a contact"))
        self.detail_stack = Gtk.Stack()
        self.detail_stack.add_named(self.empty, "empty")
        self.detail_stack.add_named(self.detail, "contact")
        detail_view = Adw.ToolbarView(content=self.detail_stack)
        detail_view.add_top_bar(detail_header)
        self.content = Adw.NavigationPage(title=_("Contact"), child=detail_view)
        self.split = Adw.NavigationSplitView(sidebar=sidebar, content=self.content,
                                             hexpand=True, min_sidebar_width=260,
                                             max_sidebar_width=360)
        self.append(self.split)
        self._show(None)

    # -- loading ------------------------------------------------------------------
    def set_device(self, dev):
        if dev is not self.dev:
            self.dev = dev
            self._loaded_for = None
            self._serial += 1           # late answers of the last phone: not here
            self.contacts = []
            self._fill()
            self._show(None)
        self.device_changed()

    def device_changed(self):
        online = self.dev is not None and self.dev.online
        self.add_button.set_sensitive(online and any(s["writable"] for s in self.sources))
        if online:      # also unseen: the dial pad and calls look names up here
            self.load()
        else:
            self._loaded_for = None
        self._update_buttons()

    def load(self, force=False):
        dev = self.dev
        if dev is None or not dev.online or (self._loaded_for is dev and not force):
            return
        self._loaded_for = dev
        self._serial += 1
        serial = self._serial
        if not self.contacts:
            self.status.set_title(_("Loading contacts …"))
            self.list_stack.set_visible_child_name("status")

        def got_sources(result, error):
            if serial == self._serial and error is None:
                self.sources = [s for s in result if s["kind"] == "contacts"]
                self.device_changed()

        def got(result, error):
            if serial != self._serial:
                return
            if error is not None:
                self._loaded_for = None
                self.status.set_title(text.error(error))
                self.list_stack.set_visible_child_name("status")
                return
            self.contacts = result
            self._have = dev
            self._fill()
            if self._pending_number is not None:
                number, self._pending_number = self._pending_number, None
                self.open_number(number)

        dev.request("pim.sources", {}, got_sources)
        dev.request("contacts.list", {}, got)

    def _fill(self):
        keep = self.current and (self.current["source"], self.current["uid"])
        while (row := self.list.get_row_at_index(0)) is not None:
            self.list.remove(row)
        selected = None
        for c in self.contacts:
            row = ContactRow(self.app, self.dev, c)
            self.list.append(row)
            if keep == (c["source"], c["uid"]):
                selected = row
        if not self.contacts:
            self.status.set_title(_("No contacts"))
        self.list_stack.set_visible_child_name("list" if self.contacts else "status")
        if selected is not None:
            self.list.select_row(selected)
            self._show(selected.contact)
        elif keep:
            self._show(None)

    def find_number(self, number):
        """The contact with this phone number, or None."""
        country = self.app.cfg["country"]
        if getattr(self, "_by_number_of", None) is not self.contacts:
            self._by_number = {}
            for c in self.contacts:
                for p in c["phones"]:
                    self._by_number.setdefault(text.normalize(p["value"], country), c)
            self._by_number_of = self.contacts
        return self._by_number.get(text.normalize(number, country))

    def open_number(self, number):
        """Shows the contact with this number, or starts a new one with it;
        while the contacts are still coming, once they are there."""
        self.load()
        contact = self.find_number(number) if self._have is self.dev else None
        if contact is not None:
            self.show_contact(contact)
        elif self._have is self.dev and self.dev is not None:
            self.edit(None, number=number)
        else:
            self._pending_number = number

    def show_contact(self, contact):
        self.search.set_text("")
        key = (contact["source"], contact["uid"])
        i = 0
        while (row := self.list.get_row_at_index(i)) is not None:
            if (row.contact["source"], row.contact["uid"]) == key:
                self.list.select_row(row)       # no signal when it already was
                if self.current is not row.contact:
                    self._show(row.contact)
                row.grab_focus()
                break
            i += 1
        else:
            self._show(contact)
        self.split.set_show_content(True)

    # -- showing -------------------------------------------------------------------
    def _on_row(self, listbox, row):
        if row is not None:
            self._show(row.contact)
            self.split.set_show_content(True)

    def _update_buttons(self):
        c = self.current
        online = self.dev is not None and self.dev.online
        writable = bool(c and c.get("writable") and online)
        self.edit_button.set_visible(c is not None)
        self.delete_button.set_visible(c is not None)
        self.edit_button.set_sensitive(writable)
        self.delete_button.set_sensitive(writable)

    def _show(self, c):
        self.current = c
        self._update_buttons()
        if c is None:
            self.detail_stack.set_visible_child_name("empty")
            self.content.set_title(_("Contact"))
            return
        self.content.set_title(c["name"] or _("No name"))
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12)
        avatar = Adw.Avatar(size=96, text=c["name"], show_initials=True)
        if c.get("avatar"):
            self.app.avatars.get(self.dev, c["avatar"], avatar.set_custom_image)
        head.append(avatar)
        name = Gtk.Label(label=c["name"] or _("No name"), wrap=True, justify=Gtk.Justification.CENTER)
        name.add_css_class("title-1")
        head.append(name)
        if c["org"] and c["org"] != c["name"]:
            org = Gtk.Label(label=c["org"])
            org.add_css_class("dim-label")
            head.append(org)
        top = Adw.PreferencesGroup()
        top.add(head)
        info.append(top)

        if c["phones"]:
            group = Adw.PreferencesGroup(title=_("Phone"))
            types = dict(PHONE_TYPES)
            for p in c["phones"]:
                row = Adw.ActionRow(title=GLib.markup_escape_text(p["value"]),
                                    subtitle=_(types.get(p["type"], "Other")))
                row.set_title_selectable(True)
                call = Gtk.Button(icon_name="call-start-symbolic", valign=Gtk.Align.CENTER,
                                  tooltip_text=_("Call from the phone"))
                call.add_css_class("flat")
                call.connect("clicked", lambda b, n=p["value"]: self.app.dial(n))
                sms = Gtk.Button(icon_name="mail-send-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text=_("Write a message"))
                sms.add_css_class("flat")
                sms.connect("clicked", lambda b, n=p["value"]: self.app.open_sms(n))
                row.add_suffix(call)
                row.add_suffix(sms)
                group.add(row)
            info.append(group)
        if c["emails"]:
            group = Adw.PreferencesGroup(title=_("Email"))
            for e in c["emails"]:
                row = Adw.ActionRow(title=GLib.markup_escape_text(e), activatable=True,
                                    tooltip_text=_("Write an email"))
                row.add_suffix(Gtk.Image(icon_name="mail-unread-symbolic"))
                row.connect("activated", lambda r, a=e: self.write_email(a))
                group.add(row)
            info.append(group)
        more = Adw.PreferencesGroup()
        if c["birthday"]:
            more.add(Adw.ActionRow(title=_("Birthday"), subtitle=birthday_text(c["birthday"])))
        if c["note"]:
            row = Adw.ActionRow(title=_("Note"), subtitle=GLib.markup_escape_text(c["note"]))
            row.set_subtitle_selectable(True)
            more.add(row)
        more.add(Adw.ActionRow(title=_("Address book"),
                               subtitle=GLib.markup_escape_text(c.get("book", ""))))
        info.append(more)

        # the person's activity: a column of its own beside the contact - below
        # it when there is no room; only when there is something to show
        self.activity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                                visible=False)
        columns = Gtk.Box(spacing=36, margin_top=12, margin_bottom=24, margin_start=18,
                          margin_end=18, homogeneous=True)
        columns.append(Adw.Clamp(child=info, maximum_size=600, valign=Gtk.Align.START))
        self.activity_clamp = Adw.Clamp(child=self.activity, maximum_size=600,
                                        valign=Gtk.Align.START, visible=False)
        columns.append(self.activity_clamp)
        self.columns = columns
        # the scrolling inside: a BreakpointBin tells only its minimum height,
        # so around it nothing below that could be scrolled to
        scroller = Gtk.ScrolledWindow(child=columns, hscrollbar_policy=Gtk.PolicyType.NEVER)
        holder = Adw.BreakpointBin(child=scroller, width_request=280, height_request=200)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        narrow.add_setter(columns, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(columns, "homogeneous", False)     # no gap below the contact
        holder.add_breakpoint(narrow)
        self.detail.set_child(holder)
        self.detail_stack.set_visible_child_name("contact")
        self._load_activity(c)

    def write_email(self, address):
        """A new email to the address, in the PC's mail program."""
        uri = "mailto:" + GLib.Uri.escape_string(address.strip(), "@", False)

        def done(launcher, res):
            try:
                launcher.launch_finish(res)
            except GLib.Error as e:
                self.app.toast(_("No mail program: %s") % e.message)

        Gtk.UriLauncher.new(uri).launch(self.get_root(), None, done)

    # -- the person's activity ----------------------------------------------------
    def _load_activity(self, c):
        self._activity_serial += 1
        serial = self._activity_serial
        dev = self.dev
        if dev is None or not dev.online:
            return
        country = self.app.cfg["country"]
        numbers = [p["value"] for p in c["phones"] if p["value"].strip()]
        mine = {text.normalize(n, country) for n in numbers}
        threads = [t["thread"] for t in self.app.threads.get(dev.id, [])
                   if "," not in t["thread"] and text.normalize(t["thread"], country) in mine]
        got = {"calls": None, "messages": {} if threads else [], "events": None}
        if not numbers:
            got["calls"] = []

        def done():
            if serial != self._activity_serial or self.current is not c:
                return
            if got["calls"] is None or got["events"] is None or isinstance(got["messages"], dict):
                return
            self._show_activity(c, got)

        def calls(result, error):
            got["calls"] = result if error is None else []
            done()

        def messages(thread):
            def back(result, error):
                got["messages"][thread] = result if error is None else []
                if len(got["messages"]) == len(threads):
                    got["messages"] = [(t, got["messages"][t]) for t in threads]
                    done()
            return back

        given_counts = collections.Counter((x.get("given") or "").strip().casefold()
                                           for x in self.contacts)

        def events(result, error):
            got["events"] = ([e for e in result if mentions(e, c, given_counts)]
                             if error is None else [])
            done()

        if numbers:
            dev.request("calls.history", {"numbers": numbers, "limit": ACTIVITY_CALLS,
                                          "country": country}, calls)
        for t in threads:
            dev.request("sms.messages", {"thread": t, "limit": ACTIVITY_MESSAGES,
                                         "country": country}, messages(t))
        now = time.time()
        dev.request("calendar.events", {"start": int(now - EVENTS_BACK * 86400),
                                        "end": int(now + EVENTS_AHEAD * 86400)}, events)
        done()

    def _show_activity(self, c, got):
        box = self.activity
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        calls, threads, events = got["calls"], got["messages"], got["events"]
        msgs = [(t, m) for t, ms in threads for m in ms]
        if not (calls or msgs or events):
            self.activity_clamp.set_visible(False)
            box.set_visible(False)
            return
        several = len({x["number"] for x in calls}) > 1 or len(threads) > 1

        last = max([x["start"] for x in calls if x.get("start")]
                   + [m["time"] for _t, m in msgs if m.get("time")], default=None)
        if last:
            summary = Adw.PreferencesGroup(title=_("Activity"))
            summary.add(Adw.ActionRow(title=_("Last contact"), subtitle=text.activity(last)))
            box.append(summary)

        if calls:
            from .phone import duration
            group = Adw.PreferencesGroup(title=_("Calls"))
            for x in calls:
                vb = x.get("voicebox")
                missed = (x["inbound"] and not x["answered"]) or bool(vb)
                parts = []
                if vb and vb.get("audio") and not vb.get("missed"):
                    parts.append(_("Voicebox: %s") % duration(int(round(vb["duration"]))))
                elif missed:
                    parts.append(_("Missed"))
                elif x.get("duration"):
                    parts.append(duration(x["duration"]))
                if several:
                    parts.append(x["number"])
                row = Adw.ActionRow(title=text.activity(x["start"]) if x.get("start") else "",
                                    subtitle=GLib.markup_escape_text(" · ".join(parts)),
                                    activatable=True)
                row.add_prefix(Gtk.Image(icon_name="call-missed-symbolic" if missed else
                                         "call-incoming-symbolic" if x["inbound"] else
                                         "call-outgoing-symbolic"))
                if missed:
                    row.add_css_class("error")
                row.connect("activated", lambda r: self.app.show_window("phone"))
                group.add(row)
            box.append(group)

        for thread, ms in threads:
            if not ms:
                continue
            group = Adw.PreferencesGroup(title=_("Messages")
                                         + ((" · " + thread) if several else ""))
            open_button = Gtk.Button(label=_("Open conversation"), valign=Gtk.Align.CENTER)
            open_button.add_css_class("flat")
            open_button.connect("clicked", lambda b, t=thread: self.app.open_sms(t))
            group.set_header_suffix(open_button)
            for m in reversed(ms):
                row = Adw.ActionRow(
                    title=GLib.markup_escape_text(" ".join((m.get("body") or "").split())),
                    subtitle=(_("Sent") if m.get("out") else _("Received"))
                    + (" · " + text.activity(m["time"]) if m.get("time") else ""),
                    activatable=True)
                row.set_title_lines(2)
                row.add_prefix(Gtk.Image(icon_name="mail-send-symbolic" if m.get("out")
                                         else "mail-unread-symbolic"))
                row.connect("activated", lambda r, t=thread: self.app.open_sms(t))
                group.add(row)
            box.append(group)

        if events:
            group = Adw.PreferencesGroup(title=_("Appointments"))
            for ev in events[:ACTIVITY_EVENTS]:
                row = Adw.ActionRow(title=GLib.markup_escape_text(ev["summary"]
                                                                  or _("(no title)")),
                                    subtitle=GLib.markup_escape_text(event_when(ev)),
                                    activatable=True)
                row.add_prefix(Gtk.Image(icon_name="x-office-calendar-symbolic"))
                row.connect("activated", lambda r, e=ev: self._open_event(e))
                group.add(row)
            box.append(group)
        box.set_visible(True)
        self.activity_clamp.set_visible(True)

    def _open_event(self, ev):
        self.app.show_window("calendar")
        self.app.window.calendar.show_event(ev)

    # -- changing ------------------------------------------------------------------
    def edit(self, contact, number=None):
        if self.dev is None or not self.dev.online:
            return
        writable = [s for s in self.sources if s["writable"]]
        if not writable:
            self.app.toast(_("No address book can be changed"))
            return
        ContactEditor(self, contact, writable, number).present(self.get_root())

    def saved(self, source, uid):
        self.current = {"source": source, "uid": uid}
        self.load(force=True)

    def delete(self, c):
        if c is None:
            return
        dialog = Adw.AlertDialog(heading=_("Delete %s?") % (c["name"] or _("No name")),
                                 body=_("The contact is removed from “%s” – on the phone "
                                        "and in the account it syncs with.") % c.get("book", ""))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(d, response):
            if response != "delete":
                return

            def done(result, error):
                if error is not None:
                    self.app.toast(_("Not deleted: %s") % text.error(error))
                    return
                self._show(None)
                self.load(force=True)

            self.dev.request("contacts.delete", {"source": c["source"], "uid": c["uid"]}, done)

        dialog.connect("response", answered)
        dialog.present(self.get_root())


class ListEditor:
    """Rows for several values (phone numbers, email addresses) in a group:
    each one editable and removable, and a row to add one."""

    def __init__(self, group, add_label, types=None):
        self.group = group
        self.types = types
        self.rows = []
        self.add_row = Adw.ButtonRow(title=add_label, start_icon_name="list-add-symbolic")
        self.add_row.connect("activated", lambda *a: self.add("", None, focus=True))
        group.add(self.add_row)

    def add(self, value, kind, focus=False):
        row = Adw.EntryRow(title=self.group.get_title(), text=value)
        dropdown = None
        if self.types:
            dropdown = Gtk.DropDown.new_from_strings([_(l) for _k, l in self.types])
            dropdown.set_valign(Gtk.Align.CENTER)
            keys = [k for k, _l in self.types]
            dropdown.set_selected(keys.index(kind) if kind in keys else 0)
            row.add_suffix(dropdown)
        remove = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Remove"))
        remove.add_css_class("flat")
        row.add_suffix(remove)
        entry = (row, dropdown)
        remove.connect("clicked", lambda b: self._remove(entry))
        self.group.remove(self.add_row)
        self.group.add(row)
        self.group.add(self.add_row)
        self.rows.append(entry)
        if focus:
            row.grab_focus()

    def _remove(self, entry):
        self.rows.remove(entry)
        self.group.remove(entry[0])

    def values(self):
        out = []
        for row, dropdown in self.rows:
            value = row.get_text().strip()
            if value:
                kind = self.types[dropdown.get_selected()][0] if dropdown else None
                out.append((value, kind))
        return out


class ContactEditor(Adw.Dialog):
    def __init__(self, page, contact, sources, number=None):
        super().__init__(title=_("Edit contact") if contact else _("New contact"),
                         content_width=520, content_height=680)
        self.page = page
        self.app = page.app
        self.dev = page.dev
        self.contact = contact
        self.sources = sources
        self.photo = None          # None: unchanged, "": removed, (mime, bytes): new
        c = contact or {"given": "", "family": "", "org": "", "phones": [], "emails": [],
                        "birthday": "", "note": "", "name": ""}

        prefs = Adw.PreferencesPage()
        head = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        self.avatar = Adw.Avatar(size=96, text=c["name"], show_initials=True)
        if contact and contact.get("avatar"):
            self.app.avatars.get(self.dev, contact["avatar"], self.avatar.set_custom_image)
        box.append(self.avatar)
        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        choose = Gtk.Button(label=_("Choose photo …"))
        choose.connect("clicked", self._choose_photo)
        self.remove_photo = Gtk.Button(label=_("Remove photo"))
        self.remove_photo.connect("clicked", self._drop_photo)
        self.remove_photo.set_sensitive(bool(contact and contact.get("avatar")))
        buttons.append(choose)
        buttons.append(self.remove_photo)
        box.append(buttons)
        head.add(box)
        prefs.add(head)

        names = Adw.PreferencesGroup()
        self.given = Adw.EntryRow(title=_("First name"), text=c["given"])
        self.family = Adw.EntryRow(title=_("Last name"), text=c["family"])
        self.org = Adw.EntryRow(title=_("Company"), text=c["org"])
        if not c["given"] and not c["family"] and c["name"] and c["name"] != c["org"]:
            self.given.set_text(c["name"])
        for row in (self.given, self.family, self.org):
            row.connect("changed", self._check)
            names.add(row)
        self.book = Adw.ComboRow(title=_("Address book"))
        self.book.set_model(Gtk.StringList.new([source_label(s) for s in sources]))
        uids = [s["uid"] for s in sources]
        if contact and contact["source"] in uids:
            self.book.set_selected(uids.index(contact["source"]))
        names.add(self.book)
        prefs.add(names)

        phones = Adw.PreferencesGroup(title=_("Phone"))
        self.phones = ListEditor(phones, _("Add number"), PHONE_TYPES)
        for p in c["phones"]:
            self.phones.add(p["value"], p["type"])
        if number:
            self.phones.add(number, "mobile")
        prefs.add(phones)
        emails = Adw.PreferencesGroup(title=_("Email"))
        self.emails = ListEditor(emails, _("Add email address"))
        for e in c["emails"]:
            self.emails.add(e, None)
        prefs.add(emails)

        more = Adw.PreferencesGroup()
        self.birthday = Adw.EntryRow(title=_("Birthday (e.g. 24.12.1990)"),
                                     text=short_date(dt.date.fromisoformat(c["birthday"]))
                                     if c["birthday"] else "")
        self.birthday.connect("changed", self._check)
        self.note = Adw.EntryRow(title=_("Note"), text=c["note"])
        more.add(self.birthday)
        more.add(self.note)
        prefs.add(more)

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *a: self.close())
        self.save = Gtk.Button(label=_("Save"))
        self.save.add_css_class("suggested-action")
        self.save.connect("clicked", self._save)
        header.pack_start(cancel)
        header.pack_end(self.save)
        view = Adw.ToolbarView(content=prefs)
        view.add_top_bar(header)
        self.set_child(view)
        self._check()

    def _check(self, *args):
        named = any(r.get_text().strip() for r in (self.given, self.family, self.org))
        bday = self.birthday.get_text().strip()
        ok_bday = not bday or parse_date(bday) is not None
        if ok_bday:
            self.birthday.remove_css_class("error")
        else:
            self.birthday.add_css_class("error")
        self.save.set_sensitive(named and ok_bday)
        self.avatar.set_text(" ".join(r.get_text().strip() for r in (self.given, self.family)))

    def _choose_photo(self, *args):
        dialog = Gtk.FileDialog(title=_("Choose photo"))
        images = Gtk.FileFilter()
        images.set_name(_("Images"))
        images.add_mime_type("image/*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        dialog.set_filters(filters)

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(f.get_path(), 384, 384, True)
                ok, data = pix.save_to_bufferv("jpeg", ["quality"], ["90"])
            except GLib.Error as e:
                self.app.toast(e.message)
                return
            self.photo = ("image/jpeg", bytes(data))
            from gi.repository import Gdk
            self.avatar.set_custom_image(Gdk.Texture.new_for_pixbuf(pix))
            self.remove_photo.set_sensitive(True)

        dialog.open(self.get_root(), None, done)

    def _drop_photo(self, *args):
        self.photo = ""
        self.avatar.set_custom_image(None)
        self.remove_photo.set_sensitive(False)

    def _save(self, *args):
        bday = parse_date(self.birthday.get_text()) if self.birthday.get_text().strip() else None
        contact = {"given": self.given.get_text().strip(),
                   "family": self.family.get_text().strip(),
                   "org": self.org.get_text().strip(),
                   "phones": [{"value": v, "type": k} for v, k in self.phones.values()],
                   "emails": [v for v, _k in self.emails.values()],
                   "birthday": bday.isoformat() if bday else "",
                   "note": self.note.get_text().strip()}
        source = self.sources[self.book.get_selected()]["uid"]
        args = {"source": source, "contact": contact}
        if self.contact:
            args["uid"] = self.contact["uid"]
            args["old_source"] = self.contact["source"]
        if self.photo == "":
            args["photo"] = None
        elif self.photo:
            args["photo"] = {"mime": self.photo[0],
                             "data": base64.b64encode(self.photo[1]).decode()}
        self.save.set_sensitive(False)

        def done(result, error):
            if error is not None:
                self.save.set_sensitive(True)
                alert = Adw.AlertDialog(heading=_("Not saved"), body=text.error(error))
                alert.add_response("ok", _("OK"))
                alert.present(self)
                return
            self.close()
            self.page.saved(source, result.get("uid", ""))

        self.dev.request("contacts.save", args, done)
