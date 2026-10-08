# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Small pieces the pages share: picking a date and a time, names of days
and months in the user's language, a person's picture that opens their
contact, the "today / yesterday / ..." sections of a list."""

import datetime as dt

from gi.repository import Gdk, GLib, GObject, Gtk

from . import text
from .i18n import N_, _

WEEKDAYS = (N_("Monday"), N_("Tuesday"), N_("Wednesday"), N_("Thursday"),
            N_("Friday"), N_("Saturday"), N_("Sunday"))
MONTHS = (N_("January"), N_("February"), N_("March"), N_("April"), N_("May"),
          N_("June"), N_("July"), N_("August"), N_("September"), N_("October"),
          N_("November"), N_("December"))


def day_title(day, today=None):
    """'Today', 'Tomorrow', or 'Wednesday, 7 October 2026'."""
    today = today or dt.date.today()
    if day == today:
        prefix = _("Today")
    elif day == today + dt.timedelta(days=1):
        prefix = _("Tomorrow")
    elif day == today - dt.timedelta(days=1):
        prefix = _("Yesterday")
    else:
        prefix = _(WEEKDAYS[day.weekday()])
    text = _("%(day)d %(month)s") % {"day": day.day, "month": _(MONTHS[day.month - 1])}
    if day.year != today.year:
        text += " %d" % day.year
    return "%s, %s" % (prefix, text)


def short_date(day):
    return day.strftime(_("%Y-%m-%d"))


def parse_date(text):
    """'7.10.2026', '2026-10-07' or '07.10.' (this year) -> date or None."""
    text = text.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        d = dt.datetime.strptime(text.rstrip("."), "%d.%m")
        return d.date().replace(year=dt.date.today().year)
    except ValueError:
        return None


class DateButton(Gtk.MenuButton):
    """A button showing a date; a calendar pops up to change it."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, day=None):
        super().__init__(valign=Gtk.Align.CENTER)
        self.calendar = Gtk.Calendar()
        self.calendar.connect("day-selected", self._on_day)
        pop = Gtk.Popover(child=self.calendar)
        self.set_popover(pop)
        self._day = None
        self.set_date(day or dt.date.today())

    def get_date(self):
        return self._day

    def set_date(self, day):
        self._day = day
        self.calendar.select_day(GLib.DateTime.new_local(day.year, day.month, day.day, 0, 0, 0))
        self.set_label(short_date(day))

    def _on_day(self, cal):
        d = cal.get_date()
        day = dt.date(d.get_year(), d.get_month(), d.get_day_of_month())
        if day != self._day:
            self._day = day
            self.set_label(short_date(day))
            self.get_popover().popdown()
            self.emit("changed")


class TimeEntry(Gtk.Box):
    """Hours and minutes as two spin buttons."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, hour=9, minute=0):
        super().__init__(spacing=2, valign=Gtk.Align.CENTER)
        self.hours = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.minutes = Gtk.SpinButton.new_with_range(0, 59, 5)
        for spin in (self.hours, self.minutes):
            spin.set_wrap(True)
            spin.set_numeric(True)
            spin.set_orientation(Gtk.Orientation.VERTICAL)
            spin.connect("output", self._two_digits)
            spin.connect("value-changed", lambda *a: self.emit("changed"))
        self.append(self.hours)
        self.append(Gtk.Label(label=":"))
        self.append(self.minutes)
        self.set_time(hour, minute)

    @staticmethod
    def _two_digits(spin):
        spin.set_text("%02d" % spin.get_value_as_int())
        return True

    def get_time(self):
        return self.hours.get_value_as_int(), self.minutes.get_value_as_int()

    def set_time(self, hour, minute):
        self.hours.set_value(hour)
        self.minutes.set_value(minute)


def opens_contact(widget, app, number):
    """A click on a person's picture opens their contact - or a new one with
    the number, when there is none. `number` may be a function giving it at
    the time of the click (a picture that shows changing people). The click
    is the picture's alone: the row around it does not take it."""
    def current():
        n = number() if callable(number) else number
        return n if n and "," not in n and n != "withheld" else None

    if not callable(number) and current() is None:
        return widget                   # a group, a withheld number: nobody to open
    click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
    click.connect("pressed", lambda g, n, x, y: current() is not None and g.set_state(
        Gtk.EventSequenceState.CLAIMED))
    click.connect("released", lambda g, n, x, y: current() is not None
                  and app.open_contact(current()))
    widget.add_controller(click)
    widget.set_cursor(Gdk.Cursor.new_from_name("pointer"))
    widget.set_has_tooltip(True)
    widget.connect("query-tooltip", lambda w, x, y, kb, tip: _contact_tip(app, current(), tip))
    return widget


def _contact_tip(app, number, tip):
    if number is None:
        return False
    tip.set_text(_("Open contact") if app.find_contact(number) is not None
                 else _("Add to contacts"))
    return True


def section_label(group):
    """The heading of one of text.DAY_GROUPS."""
    label = Gtk.Label(label=_(text.DAY_GROUPS[group]), xalign=0)
    label.add_css_class("heading")
    label.add_css_class("dim-label")
    return label


def day_sections(listbox):
    """Headings over a list sorted newest first, whose rows carry `day_group`:
    one where a section starts - so only the sections there are shown, and a
    search hides the headings of what it hides."""
    def header(row, before):
        group = getattr(row, "day_group", None)
        if group is None or (before is not None
                             and getattr(before, "day_group", None) == group):
            row.set_header(None)
            return
        label = section_label(group)
        label.set_margin_start(12)
        label.set_margin_top(6 if before is None else 14)
        label.set_margin_bottom(4)
        row.set_header(label)
    listbox.set_header_func(header)
