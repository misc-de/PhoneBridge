# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Phone: dial a number (the phone calls, PhoneBridge only starts it), the
call history of GNOME Calls (read only - Calls keeps it in memory and would
not notice a change), and the call in progress: answer or hang up."""

import time

from gi.repository import Adw, GLib, Gtk, Pango

from . import text
from .i18n import N_, _

KEYS = (("1", ""), ("2", "ABC"), ("3", "DEF"), ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
        ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"), ("*", ""), ("0", "+"), ("#", ""))
CALL_STATES = {"incoming": N_("Incoming call"), "waiting": N_("Call waiting"),
               "dialing": N_("Calling …"), "alerting": N_("Ringing …"),
               "active": N_("In a call"), "held": N_("On hold")}


def call_state(state):
    return _(CALL_STATES[state]) if state in CALL_STATES else state


def duration(seconds):
    if seconds < 60:
        return _("%d s") % seconds
    if seconds < 3600:
        return "%d:%02d" % (seconds // 60, seconds % 60)
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


class CallRow(Gtk.ListBoxRow):
    def __init__(self, page, call):
        super().__init__(activatable=False)
        self.call = call
        app, dev = page.app, page.dev
        name = call["name"] or call["number"] or _("Unknown number")
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=10,
                      margin_end=8)
        avatar = Adw.Avatar(size=36, text=call["name"], show_initials=bool(call["name"]))
        if call.get("avatar"):
            app.avatars.get(dev, call["avatar"], avatar.set_custom_image)
        box.append(avatar)
        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                        valign=Gtk.Align.CENTER)
        title = Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        missed = call["inbound"] and not call["answered"]
        if missed:
            title.add_css_class("error")
        lines.append(title)
        sub = Gtk.Box(spacing=4)
        icon = ("call-missed-symbolic" if missed else
                "call-incoming-symbolic" if call["inbound"] else "call-outgoing-symbolic")
        sub.append(Gtk.Image(icon_name=icon, pixel_size=12))
        parts = [text.activity(call["start"])] if call["start"] else []
        if call["answered"] and call["duration"]:
            parts.append(duration(call["duration"]))
        elif missed:
            parts.append(_("missed"))
        elif not call["inbound"] and not call["answered"]:
            parts.append(_("not answered"))
        info = Gtk.Label(label=" · ".join(parts), xalign=0)
        info.add_css_class("dim-label")
        info.add_css_class("caption")
        sub.append(info)
        lines.append(sub)
        box.append(lines)
        if call["number"]:
            for icon_name, tip, cb in (
                    ("call-start-symbolic", _("Call from the phone"), app.dial),
                    ("mail-send-symbolic", _("Write a message"), app.open_sms)):
                b = Gtk.Button(icon_name=icon_name, valign=Gtk.Align.CENTER, tooltip_text=tip)
                b.add_css_class("flat")
                b.connect("clicked", lambda btn, f=cb: f(call["number"]))
                box.append(b)
            if not call["name"]:
                add = Gtk.Button(icon_name="contact-new-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text=_("Add to contacts"))
                add.add_css_class("flat")
                add.connect("clicked", lambda b: app.new_contact(call["number"]))
                box.append(add)
        self.set_child(box)


class PhonePage(Gtk.Box):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.dev = None
        self._loaded_for = None
        self._serial = 0

        pad = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18,
                      margin_bottom=18, margin_start=18, margin_end=18,
                      valign=Gtk.Align.START)
        self.number = Gtk.Entry(placeholder_text=_("Number"), xalign=0.5,
                                input_purpose=Gtk.InputPurpose.PHONE)
        self.number.add_css_class("title-2")
        self.number.connect("activate", lambda *a: self._dial())
        self.number.connect("changed", lambda *a: self._update())
        pad.append(self.number)
        self.match = Gtk.Label()
        self.match.add_css_class("dim-label")
        pad.append(self.match)
        grid = Gtk.Grid(row_spacing=8, column_spacing=8, halign=Gtk.Align.CENTER)
        for n, (digit, letters) in enumerate(KEYS):
            b = Gtk.Button()
            b.add_css_class("circular")
            b.set_size_request(64, 64)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            d = Gtk.Label(label=digit)
            d.add_css_class("title-2")
            inner.append(d)
            if letters:
                l = Gtk.Label(label=letters)
                l.add_css_class("caption")
                l.add_css_class("dim-label")
                inner.append(l)
            b.set_child(inner)
            b.connect("clicked", lambda btn, k=digit: self._press(k))
            grid.attach(b, n % 3, n // 3, 1, 1)
        pad.append(grid)
        actions = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.sms_button = Gtk.Button(icon_name="mail-send-symbolic",
                                     tooltip_text=_("Write a message"))
        self.sms_button.add_css_class("circular")
        self.sms_button.set_size_request(48, 48)
        self.sms_button.connect("clicked", lambda *a: self.app.open_sms(self.number.get_text()))
        self.call_button = Gtk.Button(icon_name="call-start-symbolic",
                                      tooltip_text=_("Call from the phone"))
        self.call_button.add_css_class("circular")
        self.call_button.add_css_class("suggested-action")
        self.call_button.set_size_request(64, 64)
        self.call_button.connect("clicked", lambda *a: self._dial())
        back = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text=_("Delete"))
        back.add_css_class("circular")
        back.set_size_request(48, 48)
        back.connect("clicked", lambda *a: self._back())
        actions.append(self.sms_button)
        actions.append(self.call_button)
        actions.append(back)
        pad.append(actions)
        side_header = Adw.HeaderBar(show_end_title_buttons=False,
                                    show_start_title_buttons=False)
        side_view = Adw.ToolbarView(content=Gtk.ScrolledWindow(
            child=pad, hscrollbar_policy=Gtk.PolicyType.NEVER))
        side_view.add_top_bar(side_header)
        sidebar = Adw.NavigationPage(title=_("Dial"), child=side_view)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.status = Adw.StatusPage(icon_name="call-start-symbolic")
        self.stack = Gtk.Stack()
        self.stack.add_named(Gtk.ScrolledWindow(
            child=Adw.Clamp(child=self.list, maximum_size=760, margin_top=12,
                            margin_bottom=12, margin_start=12, margin_end=12),
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER), "list")
        self.stack.add_named(self.status, "status")
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Reload"))
        refresh.connect("clicked", lambda *a: self.load(force=True))
        header.pack_end(refresh)
        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        content = Adw.NavigationPage(title=_("Recent calls"), child=view)
        self.split = Adw.NavigationSplitView(sidebar=sidebar, content=content, hexpand=True,
                                             min_sidebar_width=300, max_sidebar_width=340,
                                             show_content=True)
        self.append(self.split)
        self._update()

    def set_device(self, dev):
        if dev is not self.dev:
            self.dev = dev
            self._loaded_for = None
            self._fill([])
        self.device_changed()

    def device_changed(self):
        if self.dev is not None and self.dev.online and self.get_mapped():
            self.load()
        elif self.dev is None or not self.dev.online:
            self._loaded_for = None
        self._update()

    def load(self, force=False):
        dev = self.dev
        if dev is None or not dev.online or (self._loaded_for is dev and not force):
            return
        self._loaded_for = dev
        self._serial += 1
        serial = self._serial

        def done(result, error):
            if serial != self._serial:
                return
            if error is not None:
                self._loaded_for = None
                self.status.set_title(text.error(error))
                self.stack.set_visible_child_name("status")
                return
            self._fill(result)

        dev.request("calls.history", {"limit": 200, "country": self.app.cfg["country"]}, done)

    def _fill(self, calls):
        while (row := self.list.get_row_at_index(0)) is not None:
            self.list.remove(row)
        for c in calls:
            self.list.append(CallRow(self, c))
        self.status.set_title(_("No calls"))
        self.stack.set_visible_child_name("list" if calls else "status")

    def _press(self, key):
        pos = self.number.get_position()
        self.number.insert_text(key, pos)
        self.number.set_position(pos + 1)

    def _back(self):
        t = self.number.get_text()
        self.number.set_text(t[:-1])

    def _update(self):
        number = self.number.get_text().strip()
        online = self.dev is not None and self.dev.online
        calls = bool((self.dev.hello or {}).get("has", {}).get("calls")) if online else False
        self.call_button.set_sensitive(online and calls and bool(number))
        self.sms_button.set_sensitive(online and bool(number))
        contact = self.app.find_contact(number) if len(number) >= 3 else None
        self.match.set_label(contact["name"] if contact else "")

    def _dial(self):
        number = self.number.get_text().strip()
        if number:
            self.app.dial(number)

    def set_number(self, number):
        self.number.set_text(number)


class CallBar(Gtk.Revealer):
    """The call in progress, above every page: who, and answer / hang up."""

    def __init__(self, app):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.app = app
        self.call = None
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=12,
                      margin_end=12)
        box.add_css_class("call-bar")
        self.avatar = Adw.Avatar(size=32)
        box.append(self.avatar)
        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                        valign=Gtk.Align.CENTER)
        self.who = Gtk.Label(xalign=0)
        self.who.add_css_class("heading")
        self.state = Gtk.Label(xalign=0)
        self.state.add_css_class("caption")
        lines.append(self.who)
        lines.append(self.state)
        box.append(lines)
        self.answer = Gtk.Button(label=_("Answer"), valign=Gtk.Align.CENTER)
        self.answer.add_css_class("suggested-action")
        self.answer.connect("clicked", lambda *a: self.app.answer_call(self.dev_id, self.call))
        self.hangup = Gtk.Button(label=_("Hang up"), valign=Gtk.Align.CENTER)
        self.hangup.add_css_class("destructive-action")
        self.hangup.connect("clicked", lambda *a: self.app.hangup_call(self.dev_id, self.call))
        box.append(self.answer)
        box.append(self.hangup)
        self.set_child(box)
        self.dev_id = None
        self._tick = 0

    def show_call(self, dev, call):
        self.call = call
        self.dev_id = dev.id if dev else None
        if call is None:
            self.set_reveal_child(False)
            return
        name = call["name"] or call["number"] or _("Unknown number")
        if call["number"] == "withheld":
            name = _("Withheld number")
        self.who.set_label(name)
        self.avatar.set_text(call["name"])
        self.avatar.set_show_initials(bool(call["name"]))
        self.avatar.set_custom_image(None)
        if call.get("avatar"):
            self.app.avatars.get(dev, call["avatar"], self.avatar.set_custom_image)
        state = call_state(call["state"])
        if call["state"] == "active" and call.get("since"):
            state += " · " + duration(int(time.time() - call["since"]))
        self.state.set_label(state)
        self.answer.set_visible(call["state"] in ("incoming", "waiting"))
        self.set_reveal_child(True)
        if call["state"] == "active" and not self._tick:
            self._tick = GLib.timeout_add_seconds(1, self._update_time)

    def _update_time(self):
        if self.call is None or self.call["state"] != "active":
            self._tick = 0
            return False
        self.state.set_label(call_state("active") + " · "
                             + duration(int(time.time() - self.call.get("since", time.time()))))
        return True
