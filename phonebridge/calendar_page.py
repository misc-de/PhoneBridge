# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Appointments: the phone's calendars (evolution-data-server) as a month
and an agenda from the chosen day on; add, change and delete appointments.
Changes sync to the calendar's account (CalDAV, Google ...) like on the
phone.

A recurring appointment changed through one of its days changes the whole
series (moved by as much as that day moved); deleting asks whether only
that day or the whole series goes."""

import datetime as dt
import time

from gi.repository import Adw, Gdk, GLib, Gtk

from . import text
from .contacts import source_label
from .i18n import N_, _
from .widgets import DateButton, TimeEntry, day_title

AGENDA_DAYS = 31
ALARMS = ((None, N_("None")), (0, N_("At the start")), (5, N_("5 minutes before")),
          (15, N_("15 minutes before")), (30, N_("30 minutes before")),
          (60, N_("1 hour before")), (120, N_("2 hours before")),
          (1440, N_("1 day before")), (2880, N_("2 days before")))
CALENDAR_NAMES = {"Personal": N_("Personal"), "Contact birthdays": N_("Contact birthdays"),
                  "Birthdays & Anniversaries": N_("Birthdays & Anniversaries")}


def calendar_label(source):
    if source["name"] in CALENDAR_NAMES:
        source = dict(source, name=_(CALENDAR_NAMES[source["name"]]))
    return source_label(source)


def ev_days(ev):
    """The days an appointment covers (end exclusive for all-day ones)."""
    if ev["allday"]:
        first = dt.date.fromisoformat(ev["start"])
        last = dt.date.fromisoformat(ev["end"]) - dt.timedelta(days=1)
    else:
        first = dt.date.fromtimestamp(ev["start"])
        end = ev["end"] if ev["end"] > ev["start"] else ev["start"]
        last = dt.date.fromtimestamp(end - 1 if end > ev["start"] else end)
    last = max(first, last)
    return first, last


def time_range(ev):
    if ev["allday"]:
        return _("All day")
    s = time.strftime("%H:%M", time.localtime(ev["start"]))
    if ev["end"] <= ev["start"]:
        return s
    e = time.strftime("%H:%M", time.localtime(ev["end"]))
    first, last = ev_days(ev)
    if first != last:
        return "%s – %s %s" % (s, text.when_long(ev["end"]).split(",")[0], e)
    return "%s – %s" % (s, e)


def color_dot(color):
    dot = Gtk.DrawingArea(content_width=10, content_height=10, valign=Gtk.Align.CENTER)
    rgba = Gdk.RGBA()
    if not color or not rgba.parse(color):
        rgba.parse("#3584e4")

    def draw(area, cr, w, h):
        cr.arc(w / 2, h / 2, min(w, h) / 2, 0, 6.2832)
        Gdk.cairo_set_source_rgba(cr, rgba)
        cr.fill()

    dot.set_draw_func(draw)
    return dot


class CalendarPage(Gtk.Box):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.dev = None
        self.sources = []
        self.events = []
        self._loaded_for = None
        self._serial = 0
        self._range = None

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        self.calendar = Gtk.Calendar()
        self.calendar.connect("day-selected", lambda *a: self._on_day())
        self.calendar.connect("notify::month", lambda *a: self._on_day())
        self.calendar.connect("notify::year", lambda *a: self._on_day())
        side.append(self.calendar)
        today = Gtk.Button(label=_("Today"))
        today.connect("clicked", lambda *a: self.select(dt.date.today()))
        side.append(today)
        self.cal_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.cal_list.add_css_class("boxed-list")
        side.append(self.cal_list)
        side_header = Adw.HeaderBar(show_end_title_buttons=False,
                                    show_start_title_buttons=False)
        self.add_button = Gtk.Button(icon_name="list-add-symbolic",
                                     tooltip_text=_("New appointment"))
        self.add_button.connect("clicked", lambda *a: self.edit(None))
        side_header.pack_start(self.add_button)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Reload"))
        refresh.connect("clicked", lambda *a: self.load(force=True))
        side_header.pack_end(refresh)
        side_view = Adw.ToolbarView(content=Gtk.ScrolledWindow(
            child=side, hscrollbar_policy=Gtk.PolicyType.NEVER))
        side_view.add_top_bar(side_header)
        sidebar = Adw.NavigationPage(title=_("Calendar"), child=side_view)

        self.agenda = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.agenda.add_css_class("boxed-list")
        self.agenda.connect("row-activated", self._on_event)
        self.status = Adw.StatusPage(icon_name="x-office-calendar-symbolic")
        self.agenda_stack = Gtk.Stack()
        self.agenda_stack.add_named(Gtk.ScrolledWindow(
            child=Adw.Clamp(child=self.agenda, maximum_size=760, margin_top=12,
                            margin_bottom=12, margin_start=12, margin_end=12),
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER), "agenda")
        self.agenda_stack.add_named(self.status, "status")
        content_view = Adw.ToolbarView(content=self.agenda_stack)
        content_view.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False,
                                               show_start_title_buttons=False))
        self.content = Adw.NavigationPage(title=_("Appointments"), child=content_view)
        self.split = Adw.NavigationSplitView(sidebar=sidebar, content=self.content,
                                             hexpand=True, min_sidebar_width=300,
                                             max_sidebar_width=340)
        self.append(self.split)

    def selected(self):
        d = self.calendar.get_date()
        return dt.date(d.get_year(), d.get_month(), d.get_day_of_month())

    def select(self, day):
        self.calendar.select_day(GLib.DateTime.new_local(day.year, day.month, day.day, 0, 0, 0))

    def hidden(self):
        return set(self.app.cfg.setdefault("hidden_calendars", []))

    # -- loading ----------------------------------------------------------------
    def set_device(self, dev):
        if dev is not self.dev:
            self.dev = dev
            self._loaded_for = None
            self.events = []
            self.sources = []
            self._fill()
        self.device_changed()

    def device_changed(self):
        online = self.dev is not None and self.dev.online
        self.add_button.set_sensitive(online and any(s["writable"] for s in self.sources))
        if online and self.get_mapped():
            self.load()
        elif not online:
            self._loaded_for = None

    def _wanted_range(self):
        sel = self.selected()
        month = dt.date(sel.year, sel.month, 1)
        start = min(month - dt.timedelta(days=7), sel)
        end = max(month + dt.timedelta(days=45), sel + dt.timedelta(days=AGENDA_DAYS + 1))
        return start, end

    def _on_day(self):
        if self.dev is None or not self.dev.online:
            return
        start, end = self._wanted_range()
        if self._range is None or start < self._range[0] or end > self._range[1]:
            self.load(force=True)
        else:
            self._fill()

    def load(self, force=False):
        dev = self.dev
        if dev is None or not dev.online or (self._loaded_for is dev and not force):
            return
        self._loaded_for = dev
        self._serial += 1
        serial = self._serial
        start, end = self._wanted_range()
        start = start - dt.timedelta(days=14)
        end = end + dt.timedelta(days=31)
        if not self.events:
            self.status.set_title(_("Loading appointments …"))
            self.agenda_stack.set_visible_child_name("status")

        def got_sources(result, error):
            if serial == self._serial and error is None:
                self.sources = [s for s in result if s["kind"] == "calendar"]
                self._fill_calendars()
                self.device_changed()

        def got(result, error):
            if serial != self._serial:
                return
            if error is not None:
                self._loaded_for = None
                self.status.set_title(text.error(error))
                self.agenda_stack.set_visible_child_name("status")
                return
            self._range = (start, end)
            self.events = result
            self._fill()

        dev.request("pim.sources", {}, got_sources)
        dev.request("calendar.events", {
            "start": int(time.mktime(start.timetuple())),
            "end": int(time.mktime(end.timetuple()))}, got)

    def _fill_calendars(self):
        while (row := self.cal_list.get_row_at_index(0)) is not None:
            self.cal_list.remove(row)
        hidden = self.hidden()
        for s in self.sources:
            row = Adw.SwitchRow(title=GLib.markup_escape_text(calendar_label(s)),
                                active=s["uid"] not in hidden)
            row.add_prefix(color_dot(s.get("color")))
            row.connect("notify::active", self._on_toggle, s["uid"])
            self.cal_list.append(row)
        self.cal_list.set_visible(bool(self.sources))

    def _on_toggle(self, row, _pspec, uid):
        hidden = self.hidden()
        if row.get_active():
            hidden.discard(uid)
        else:
            hidden.add(uid)
        self.app.cfg["hidden_calendars"] = sorted(hidden)
        from . import config
        config.save(self.app.cfg)
        self._fill()
        self.get_root().overview.show_events()

    def visible_events(self):
        hidden = self.hidden()
        return [e for e in self.events if e["source"] not in hidden]

    def _fill(self):
        while (row := self.agenda.get_row_at_index(0)) is not None:
            self.agenda.remove(row)
        events = self.visible_events()
        self._mark_month(events)
        sel = self.selected()
        last_day = sel + dt.timedelta(days=AGENDA_DAYS)
        by_day = {}
        for ev in events:
            first, last = ev_days(ev)
            day = max(first, sel)
            while day <= min(last, last_day):
                by_day.setdefault(day, []).append(ev)
                day += dt.timedelta(days=1)
        if not by_day:
            self.status.set_title(_("No appointments"))
            self.status.set_description(_("Nothing in the %d days from %s on.")
                                        % (AGENDA_DAYS, day_title(sel)))
            self.agenda_stack.set_visible_child_name("status")
            self.content.set_title(day_title(sel))
            return
        for day in sorted(by_day):
            header = Gtk.ListBoxRow(activatable=False, selectable=False)
            label = Gtk.Label(label=day_title(day), xalign=0, margin_top=10, margin_bottom=4,
                              margin_start=12)
            label.add_css_class("heading")
            header.set_child(label)
            self.agenda.append(header)
            for ev in sorted(by_day[day], key=lambda e: (not e["allday"],
                                                         e["start"] if not e["allday"] else 0)):
                self.agenda.append(self._event_row(ev))
        self.agenda_stack.set_visible_child_name("agenda")
        self.content.set_title(day_title(sel))

    def _event_row(self, ev):
        source = next((s for s in self.sources if s["uid"] == ev["source"]), None)
        row = Adw.ActionRow(title=GLib.markup_escape_text(ev["summary"] or _("(no title)")),
                            activatable=True)
        sub = [time_range(ev)]
        if ev["location"]:
            sub.append(ev["location"])
        row.set_subtitle(GLib.markup_escape_text(" · ".join(sub)))
        row.add_prefix(color_dot(ev.get("color") or (source or {}).get("color")))
        if ev["recurring"]:
            row.add_suffix(Gtk.Image(icon_name="media-playlist-repeat-symbolic",
                                     tooltip_text=_("Repeats")))
        if ev["alarm"] is not None:
            row.add_suffix(Gtk.Image(icon_name="alarm-symbolic", tooltip_text=_("Reminder")))
        row.event = ev
        return row

    def _mark_month(self, events):
        self.calendar.clear_marks()
        d = self.calendar.get_date()
        year, month = d.get_year(), d.get_month()
        for ev in events:
            first, last = ev_days(ev)
            day = first
            while day <= last:
                if day.year == year and day.month == month:
                    self.calendar.mark_day(day.day)
                day += dt.timedelta(days=1)

    # -- one appointment ---------------------------------------------------------------
    def _on_event(self, listbox, row):
        ev = getattr(row, "event", None)
        if ev is not None:
            self.show_event(ev)

    def show_event(self, ev):
        lines = [time_range(ev) if not ev["allday"] else _("All day")]
        first, last = ev_days(ev)
        lines[0] = day_title(first) + ", " + lines[0] if first == last else \
            "%s – %s" % (day_title(first), day_title(last))
        source = next((s for s in self.sources if s["uid"] == ev["source"]), None)
        lines.append(_("Calendar: %s") % (calendar_label(source) if source else ev["calendar"]))
        if ev["location"]:
            lines.append(_("Place: %s") % ev["location"])
        if ev["recurring"]:
            lines.append(_("Repeats"))
        if ev["alarm"] is not None:
            lines.append(_("Reminder: %s") % alarm_label(ev["alarm"]))
        if ev["description"]:
            lines.append("")
            lines.append(ev["description"])
        dialog = Adw.AlertDialog(heading=ev["summary"] or _("(no title)"),
                                 body="\n".join(lines))
        dialog.add_response("close", _("Close"))
        online = self.dev is not None and self.dev.online
        if ev["writable"] and online:
            dialog.add_response("delete", _("Delete"))
            dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.add_response("edit", _("Edit"))
            dialog.set_response_appearance("edit", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("close")

        def answered(d, response):
            if response == "edit":
                self.edit(ev)
            elif response == "delete":
                self.delete(ev)

        dialog.connect("response", answered)
        dialog.present(self.get_root())

    def edit(self, ev):
        writable = [s for s in self.sources if s["writable"]]
        if not writable or self.dev is None or not self.dev.online:
            self.app.toast(_("No calendar can be changed"))
            return
        EventEditor(self, ev, writable, self.selected()).present(self.get_root())

    def delete(self, ev):
        if ev["recurring"]:
            dialog = Adw.AlertDialog(heading=_("Delete “%s”?") % (ev["summary"] or _("(no title)")),
                                     body=_("This appointment repeats."))
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("all", _("Whole series"))
            dialog.add_response("this", _("Only this day"))
            dialog.set_response_appearance("all", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_response_appearance("this", Adw.ResponseAppearance.DESTRUCTIVE)
        else:
            dialog = Adw.AlertDialog(heading=_("Delete “%s”?") % (ev["summary"] or _("(no title)")),
                                     body=_("It is removed from the phone and from the "
                                            "account the calendar syncs with."))
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("all", _("Delete"))
            dialog.set_response_appearance("all", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(d, response):
            if response not in ("all", "this"):
                return

            def done(result, error):
                if error is not None:
                    self.app.toast(_("Not deleted: %s") % text.error(error))
                self.load(force=True)
                self.get_root().overview.load(force=True)

            self.dev.request("calendar.delete", {
                "source": ev["source"], "uid": ev["uid"], "rid": ev["rid"],
                "override": ev.get("override", False), "scope": response,
                "start": ev["start"]}, done)

        dialog.connect("response", answered)
        dialog.present(self.get_root())

    def saved(self, day):
        self.select(day)
        self.load(force=True)
        self.get_root().overview.load(force=True)


def alarm_label(minutes):
    for value, label in ALARMS:
        if value == minutes:
            return _(label)
    if minutes % 1440 == 0:
        return _("%d days before") % (minutes // 1440)
    if minutes % 60 == 0:
        return _("%d hours before") % (minutes // 60)
    return _("%d minutes before") % minutes


class EventEditor(Adw.Dialog):
    def __init__(self, page, ev, sources, day):
        super().__init__(title=_("Edit appointment") if ev else _("New appointment"),
                         content_width=520, content_height=640)
        self.page = page
        self.ev = ev
        self.sources = sources
        prefs = Adw.PreferencesPage()

        main = Adw.PreferencesGroup()
        if ev and ev["recurring"]:
            main.set_description(_("This appointment repeats – changes apply to the whole "
                                   "series."))
        self.summary = Adw.EntryRow(title=_("Title"), text=ev["summary"] if ev else "")
        self.summary.connect("changed", self._check)
        main.add(self.summary)
        self.calendar = Adw.ComboRow(title=_("Calendar"))
        self.calendar.set_model(Gtk.StringList.new([calendar_label(s) for s in sources]))
        uids = [s["uid"] for s in sources]
        last = page.app.cfg.get("last_calendar")
        if ev and ev["source"] in uids:
            self.calendar.set_selected(uids.index(ev["source"]))
        elif last in uids:
            self.calendar.set_selected(uids.index(last))
        main.add(self.calendar)
        prefs.add(main)

        when = Adw.PreferencesGroup()
        self.allday = Adw.SwitchRow(title=_("All day"), active=bool(ev and ev["allday"]))
        self.allday.connect("notify::active", self._on_allday)
        when.add(self.allday)
        if ev and ev["allday"]:
            s_day = dt.date.fromisoformat(ev["start"])
            e_day = dt.date.fromisoformat(ev["end"]) - dt.timedelta(days=1)
            s_time, e_time = (9, 0), (10, 0)
        elif ev:
            s = dt.datetime.fromtimestamp(ev["start"])
            e = dt.datetime.fromtimestamp(max(ev["end"], ev["start"]))
            s_day, e_day = s.date(), e.date()
            s_time, e_time = (s.hour, s.minute), (e.hour, e.minute)
        else:
            now = dt.datetime.now()
            hour = min(now.hour + 1, 22) if day == now.date() else 9
            s_day = e_day = day
            s_time, e_time = (hour, 0), (hour + 1, 0)
        self.start_row = Adw.ActionRow(title=_("Starts"))
        self.start_date = DateButton(s_day)
        self.start_time = TimeEntry(*s_time)
        self.start_row.add_suffix(self.start_date)
        self.start_row.add_suffix(self.start_time)
        self.end_row = Adw.ActionRow(title=_("Ends"))
        self.end_date = DateButton(e_day)
        self.end_time = TimeEntry(*e_time)
        self.end_row.add_suffix(self.end_date)
        self.end_row.add_suffix(self.end_time)
        self._length = self._end() - self._start()
        self.start_date.connect("changed", self._on_start)
        self.start_time.connect("changed", self._on_start)
        self.end_date.connect("changed", self._check)
        self.end_time.connect("changed", self._check)
        when.add(self.start_row)
        when.add(self.end_row)
        self.alarm = Adw.ComboRow(title=_("Reminder"))
        labels = [_(l) for _v, l in ALARMS]
        values = [v for v, _l in ALARMS]
        current = ev["alarm"] if ev else 15
        if current not in values:
            labels.append(alarm_label(current))
            values.append(current)
        self.alarm_values = values
        self.alarm.set_model(Gtk.StringList.new(labels))
        self.alarm.set_selected(values.index(current))
        self._alarm_start = self.alarm.get_selected()
        when.add(self.alarm)
        prefs.add(when)

        more = Adw.PreferencesGroup()
        self.location = Adw.EntryRow(title=_("Place"), text=ev["location"] if ev else "")
        more.add(self.location)
        self.notes = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8,
                                  bottom_margin=8, left_margin=10, right_margin=10)
        self.notes.get_buffer().set_text(ev["description"] if ev else "")
        frame = Gtk.Frame(child=Gtk.ScrolledWindow(child=self.notes, min_content_height=90,
                                                   hscrollbar_policy=Gtk.PolicyType.NEVER))
        notes_group = Adw.PreferencesGroup(title=_("Notes"))
        notes_group.add(frame)
        prefs.add(more)
        prefs.add(notes_group)

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
        self._on_allday()

    def _start(self):
        d = self.start_date.get_date()
        if self.allday.get_active():
            return dt.datetime.combine(d, dt.time())
        return dt.datetime.combine(d, dt.time(*self.start_time.get_time()))

    def _end(self):
        d = self.end_date.get_date()
        if self.allday.get_active():
            return dt.datetime.combine(d, dt.time())
        return dt.datetime.combine(d, dt.time(*self.end_time.get_time()))

    def _on_allday(self, *args):
        allday = self.allday.get_active()
        self.start_time.set_visible(not allday)
        self.end_time.set_visible(not allday)
        self._check()

    def _on_start(self, *args):
        # the end follows the start, keeping the length
        new_end = self._start() + max(self._length, dt.timedelta(0))
        self.end_date.set_date(new_end.date())
        if not self.allday.get_active():
            self.end_time.set_time(new_end.hour, new_end.minute)
        self._check()

    def _check(self, *args):
        ok_time = self._end() >= self._start()
        self._length = self._end() - self._start() if ok_time else self._length
        for row in (self.end_row,):
            if ok_time:
                row.remove_css_class("error")
            else:
                row.add_css_class("error")
        self.save.set_sensitive(ok_time and bool(self.summary.get_text().strip()))

    def _save(self, *args):
        allday = self.allday.get_active()
        buf = self.notes.get_buffer()
        fields = {"summary": self.summary.get_text().strip(),
                  "location": self.location.get_text().strip(),
                  "description": buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False),
                  "allday": allday}
        if allday:
            fields["start"] = self.start_date.get_date().isoformat()
            fields["end"] = (self.end_date.get_date() + dt.timedelta(days=1)).isoformat()
        else:
            fields["start"] = int(time.mktime(self._start().timetuple()))
            fields["end"] = int(time.mktime(self._end().timetuple()))
        if self.ev is None or self.alarm.get_selected() != self._alarm_start:
            fields["alarm"] = self.alarm_values[self.alarm.get_selected()]
        else:
            fields["alarm"] = "keep"
        source = self.sources[self.calendar.get_selected()]["uid"]
        args = {"source": source, "event": fields}
        if self.ev:
            args["uid"] = self.ev["uid"]
            args["old_source"] = self.ev["source"]
            args["rid"] = self.ev["rid"]
            args["override"] = self.ev.get("override", False)
            if self.ev["recurring"] and not self.ev.get("override"):
                if self.ev["allday"] == allday:
                    args["occurrence_start"] = self.ev["start"]
        self.page.app.cfg["last_calendar"] = source
        self.save.set_sensitive(False)
        dev = self.page.dev

        def done(result, error):
            if error is not None:
                self.save.set_sensitive(True)
                alert = Adw.AlertDialog(heading=_("Not saved"), body=text.error(error))
                alert.add_response("ok", _("OK"))
                alert.present(self)
                return
            from . import config
            config.save(self.page.app.cfg)
            self.close()
            self.page.saved(self.start_date.get_date())

        dev.request("calendar.save", args, done)
