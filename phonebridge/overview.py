# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Overview: how the phone is doing, and what happened and comes next -
the last calls, the last conversations, the next appointments - in two or
three columns as the window allows. Nothing to set here; that is on the
settings page. A click leads to the page behind each card."""

import datetime as dt
import time

from gi.repository import Adw, GLib, Gtk, Pango

from . import text
from .calendar_page import color_dot
from .i18n import _
from .phone import duration
from .widgets import day_title, opens_contact

CALLS = 6
THREADS = 6
EVENTS = 6
EVENT_DAYS = 14


def tile(icon, title):
    """A status tile: icon, title, a line of text (and room for more)."""
    box = Gtk.Box(spacing=12, margin_top=12, margin_bottom=12, margin_start=14,
                  margin_end=14)
    box.append(Gtk.Image(icon_name=icon, pixel_size=24, valign=Gtk.Align.CENTER))
    lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                    valign=Gtk.Align.CENTER)
    head = Gtk.Label(label=title, xalign=0)
    head.add_css_class("dim-label")
    head.add_css_class("caption")
    # a short natural width, so four tiles fit side by side; long text ellipsizes
    value = Gtk.Label(label="–", xalign=0, ellipsize=Pango.EllipsizeMode.END,
                      max_width_chars=16, width_chars=10)
    value.add_css_class("heading")
    lines.append(head)
    lines.append(value)
    box.append(lines)
    frame = Gtk.Frame(child=box)
    frame.add_css_class("card")
    frame.value = value
    frame.lines = lines
    return frame


class Card(Gtk.Box):
    """A card: title, a line under it, a short list, and "Show all"."""

    def __init__(self, title, on_all):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         valign=Gtk.Align.START)
        head = Gtk.Box(spacing=8)
        label = Gtk.Label(label=title, xalign=0, hexpand=True)
        label.add_css_class("heading")
        head.append(label)
        more = Gtk.Button(label=_("Show all"))
        more.add_css_class("flat")
        more.connect("clicked", lambda *a: on_all())
        head.append(more)
        self.append(head)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", lambda lb, row: row.on_click()
                          if hasattr(row, "on_click") else None)
        self.append(self.list)
        self.empty = Gtk.Label(label="", margin_top=8)
        self.empty.add_css_class("dim-label")
        self.append(self.empty)

    def fill(self, rows, empty_text):
        while (r := self.list.get_row_at_index(0)) is not None:
            self.list.remove(r)
        for row in rows:
            self.list.append(row)
        self.list.set_visible(bool(rows))
        self.empty.set_label(empty_text)
        self.empty.set_visible(not rows)


def entry_row(prefix, title, subtitle, on_click, bold=False, red=False, fresh=False):
    """fresh: new or current - a blue edge on the left."""
    row = Adw.ActionRow(title=GLib.markup_escape_text(title or ""), activatable=True,
                        subtitle=GLib.markup_escape_text(subtitle or ""))
    row.set_title_lines(1)
    row.set_subtitle_lines(1)
    if prefix is not None:
        row.add_prefix(prefix)
    if bold:
        row.add_css_class("thread-unread")
    if red:
        row.add_css_class("error")
    if fresh:
        row.add_css_class("fresh")
    row.on_click = on_click
    return row


class OverviewPage(Gtk.ScrolledWindow):
    def __init__(self, app):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.app = app
        self.dev = None
        self.calls = []
        self.events = []
        self._serial = 0
        self._loaded_for = None
        self._stamp = 0

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                         margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        # status: four tiles, side by side as far as they fit
        self.tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                 max_children_per_line=4, min_children_per_line=1,
                                 column_spacing=12, row_spacing=12)
        self.battery = tile("battery-symbolic", _("Battery"))
        self.level = Gtk.LevelBar(min_value=0, max_value=100)
        self.battery.lines.append(self.level)
        self.mobile = tile("network-cellular-symbolic", _("Mobile network"))
        self.wifi = tile("network-wireless-symbolic", _("Wi-Fi"))
        self.conn = tile("phone-symbolic", _("Connection"))
        for t in (self.battery, self.mobile, self.wifi, self.conn):
            self.tiles.append(t)
        column.append(self.tiles)

        # what happened and what comes: three cards, three or two columns
        self.cards = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                 max_children_per_line=3, min_children_per_line=1,
                                 column_spacing=18, row_spacing=18,
                                 valign=Gtk.Align.START)
        self.calls_card = Card(_("Calls"), lambda: app.show_window("phone"))
        self.messages_card = Card(_("Messages"), lambda: app.show_window("messages"))
        self.events_card = Card(_("Appointments"), lambda: app.show_window("calendar"))
        for card in (self.calls_card, self.messages_card, self.events_card):
            card.set_size_request(300, -1)
            self.cards.append(card)
        for child in (self.cards.get_child_at_index(i) for i in range(3)):
            child.set_valign(Gtk.Align.START)
        column.append(self.cards)
        self.set_child(Adw.Clamp(child=column, maximum_size=1400, tightening_threshold=1000))
        self.connect("map", lambda *a: self.load())

    # -- the phone ------------------------------------------------------------------
    def set_device(self, dev):
        if dev is not self.dev:
            self.dev = dev
            self._loaded_for = None
            self._serial += 1           # late answers of the last phone: not here
            self.calls, self.events = [], []
        self.update()
        self.show_threads()             # what the app already has, at once
        self.show_calls()
        self.show_events()
        self.load()

    def device_changed(self):
        self.update()
        self.load()

    def take_calls(self, calls):
        """The call history the phone page has just loaded - the newest first."""
        self.calls = calls
        self.show_calls()

    def update(self):
        dev = self.dev
        status = dev.status if dev is not None and dev.online else None
        s = status or {}
        bat = s.get("battery")
        self.battery.value.set_label(text.battery(status) or "–")
        self.battery.set_tooltip_text(text.battery(status) or None)
        self.level.set_value(bat["percent"] if bat else 0)
        self.mobile.value.set_label(text.network(status) or "–")
        self.wifi.value.set_label(text.wifi(status) or "–")
        if dev is None:
            self.conn.value.set_label("–")
        else:
            self.conn.value.set_label(text.device_state(dev))
            info = dev.info
            where = "%s@%s" % (info["user"], info["host"])
            self.conn.set_tooltip_text("%s (%s)" % (s.get("hostname") or dev.name, where))

    def load(self, force=False):
        """Calls and appointments - once per connection, again after a minute
        when the page comes back, or when asked (a call ended ...)."""
        dev = self.dev
        if dev is None or not dev.online or not self.get_mapped():
            if dev is None or not dev.online:
                self._loaded_for = None
            return
        if self._loaded_for is dev and not force and time.time() - self._stamp < 60:
            return
        if dev is not self._loaded_for:
            self._stamp = 0
        self._loaded_for = dev
        self._stamp = time.time()
        self._serial += 1
        serial = self._serial

        def got_calls(result, error):
            if serial == self._serial and error is None:
                self.calls = result
                self.show_calls()

        def got_events(result, error):
            if serial == self._serial and error is None:
                self.events = result
                self.show_events()

        dev.request("calls.history", {"limit": 30, "country": self.app.cfg["country"]},
                    got_calls)
        now = time.time()
        start = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
        dev.request("calendar.events", {"start": int(start),
                                        "end": int(now + EVENT_DAYS * 86400)}, got_events)

    # -- cards ---------------------------------------------------------------------
    def _avatar(self, name, key, number=None):
        avatar = Adw.Avatar(size=32, text=name or "", show_initials=bool(name))
        if key and self.dev is not None:
            self.app.avatars.get(self.dev, key, avatar.set_custom_image)
        return opens_contact(avatar, self.app, number)

    def show_calls(self):
        dev = self.dev
        phone = self.get_root().phone if self.get_root() is not None else None
        entries = self.calls
        if phone is not None and dev is not None:
            # the same list as the phone page: VoiceBox's messages in their places
            saved = phone.calls
            phone.calls = self.calls
            try:
                entries = phone.entries()
            finally:
                phone.calls = saved
        rows = []
        today = dt.date.today()
        for c in entries[:CALLS]:
            vb = c.get("voicebox")
            missed = (c["inbound"] and not c["answered"]) or bool(vb)
            sub = [text.activity(c["start"])] if c.get("start") else []
            if vb and vb["audio"] and not vb["missed"]:
                sub.append(_("Voicebox: %s") % duration(int(round(vb["duration"]))))
            name = c["name"] or c["number"] or _("Unknown number")
            unheard = bool(vb and vb.get("new") and vb["audio"])
            missed_now = missed and c.get("start") and \
                dt.date.fromtimestamp(c["start"]) == today
            rows.append(entry_row(self._avatar(c["name"], c.get("avatar"), c["number"]), name,
                                  " · ".join(sub), lambda: self.app.show_window("phone"),
                                  bold=unheard, red=missed,
                                  fresh=unheard or bool(missed_now)))
        self.calls_card.fill(rows, _("No calls"))

    def show_threads(self):
        dev = self.dev
        threads = self.app.threads.get(dev.id, []) if dev is not None else []
        rows = []
        for t in threads[:THREADS]:
            last = t.get("last") or {}
            sub = text.activity(last["time"]) if last.get("time") else ""
            rows.append(entry_row(self._avatar(t["title"], t.get("avatar"), t["thread"]),
                                  t["title"], sub,
                                  lambda th=t["thread"]: self._open_thread(th),
                                  bold=bool(t["unread"]), fresh=bool(t["unread"])))
        self.messages_card.fill(rows, _("No messages"))

    def _open_thread(self, thread):
        self.app.show_window("messages")
        self.app.window.messages.open_thread(thread)

    def show_events(self):
        now = time.time()
        upcoming = []
        hidden = set(self.app.cfg.get("hidden_calendars", []))
        for ev in self.events:
            if ev["source"] in hidden:
                continue
            if ev["allday"]:
                end = dt.date.fromisoformat(ev["end"])
                if end <= dt.date.today():
                    continue
            elif max(ev["end"], ev["start"]) < now:
                continue
            upcoming.append(ev)
        rows = []
        for ev in upcoming[:EVENTS]:
            if ev["allday"]:
                day = max(dt.date.fromisoformat(ev["start"]), dt.date.today())
                when = "%s · %s" % (day_title(day), _("All day"))
            else:
                day = dt.date.fromtimestamp(ev["start"])
                when = "%s · %s" % (day_title(day),
                                    time.strftime("%H:%M", time.localtime(ev["start"])))
            if ev.get("location"):
                when += " · " + ev["location"]
            rows.append(entry_row(color_dot(ev.get("color")),
                                  ev["summary"] or _("(no title)"), when,
                                  lambda d=day: self._open_day(d),
                                  fresh=self._is_today(ev)))
        self.events_card.fill(rows, _("No appointments in the next two weeks"))

    @staticmethod
    def _is_today(ev):
        """Today's, or going on right now."""
        if ev["allday"]:
            return dt.date.fromisoformat(ev["start"]) <= dt.date.today()
        return dt.date.fromtimestamp(ev["start"]) <= dt.date.today()

    def _open_day(self, day):
        self.app.show_window("calendar")
        self.app.window.calendar.select(day)
