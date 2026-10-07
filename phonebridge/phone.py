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
        vb = call.get("voicebox")
        # answered by VoiceBox: for the user it is a call they missed
        missed = (call["inbound"] and not call["answered"]) or bool(vb)
        if missed:
            title.add_css_class("error")
        lines.append(title)
        sub = Gtk.Box(spacing=4)
        icon = ("call-missed-symbolic" if missed else
                "call-incoming-symbolic" if call["inbound"] else "call-outgoing-symbolic")
        sub.append(Gtk.Image(icon_name=icon, pixel_size=12))
        parts = [text.activity(call["start"])] if call["start"] else []
        if vb:
            parts.append(_("Voicebox: no message") if vb["missed"] or not vb["audio"]
                         else _("Voicebox: %s") % duration(int(round(vb["duration"]))))
        elif call["answered"] and call["duration"]:
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
        if vb and vb["audio"]:
            box.append(page.play_button(vb["id"]))
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


class VoicemailRow(Gtk.ListBoxRow):
    """One message VoiceBox recorded."""

    def __init__(self, page, msg):
        super().__init__(activatable=False)
        self.msg = msg
        app, dev = page.app, page.dev
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=10,
                      margin_end=8)
        avatar = Adw.Avatar(size=36, text=msg["name"], show_initials=bool(msg["name"]))
        if msg.get("avatar"):
            app.avatars.get(dev, msg["avatar"], avatar.set_custom_image)
        box.append(avatar)
        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                        valign=Gtk.Align.CENTER)
        who = msg["name"] or msg["number"] or _("Withheld number")
        title = Gtk.Label(label=who, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        if msg["new"]:
            title.add_css_class("thread-unread")
        lines.append(title)
        parts = [text.activity(msg["time"]), duration(int(round(msg["duration"])))]
        if len(app.voicebox.get(dev.id, {}).get("boxes", [])) > 1:
            parts.append(app.voicebox_box_name(dev.id, msg["box"]))
        info = Gtk.Label(label=" · ".join(p for p in parts if p), xalign=0)
        info.add_css_class("dim-label")
        info.add_css_class("caption")
        lines.append(info)
        box.append(lines)
        if msg["new"]:
            badge = Gtk.Label(label=_("new"), valign=Gtk.Align.CENTER)
            badge.add_css_class("unread-badge")
            box.append(badge)
        box.append(page.play_button(msg["id"]))
        if msg["number"]:
            for icon_name, tip, cb in (
                    ("call-start-symbolic", _("Call back from the phone"), app.dial),
                    ("mail-send-symbolic", _("Write a message"), app.open_sms)):
                b = Gtk.Button(icon_name=icon_name, valign=Gtk.Align.CENTER, tooltip_text=tip)
                b.add_css_class("flat")
                b.connect("clicked", lambda btn, f=cb: f(msg["number"]))
                box.append(b)
        delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Delete"))
        delete.add_css_class("flat")
        delete.connect("clicked", lambda b: page.delete_voicemail(msg))
        box.append(delete)
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
        # VoiceBox's messages, above the calls - only when there are any
        self.vb_title = Gtk.Label(label=_("Voicebox"), xalign=0, margin_start=6)
        self.vb_title.add_css_class("heading")
        self.vb_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.vb_list.add_css_class("boxed-list")
        self.calls_title = Gtk.Label(label=_("Recent calls"), xalign=0, margin_start=6)
        self.calls_title.add_css_class("heading")
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        column.append(self.vb_title)
        column.append(self.vb_list)
        column.append(self.calls_title)
        column.append(self.list)
        self.calls_box = column
        self.player = None
        self.playing = None
        self.play_buttons = {}
        self.status = Adw.StatusPage(icon_name="call-start-symbolic")
        self.stack = Gtk.Stack()
        self.stack.add_named(Gtk.ScrolledWindow(
            child=Adw.Clamp(child=column, maximum_size=760, margin_top=12,
                            margin_bottom=12, margin_start=12, margin_end=12),
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER), "list")
        self.stack.add_named(self.status, "status")
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Reload"))
        refresh.connect("clicked", lambda *a: self.load(force=True))
        header.pack_end(refresh)
        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        content = Adw.NavigationPage(title=_("Calls"), child=view)
        self.split = Adw.NavigationSplitView(sidebar=sidebar, content=content, hexpand=True,
                                             min_sidebar_width=300, max_sidebar_width=340,
                                             show_content=True)
        self.append(self.split)
        self._update()

    def set_device(self, dev):
        if dev is not self.dev:
            self.stop()
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
        self.calls = calls
        self._prune_buttons()
        while (row := self.list.get_row_at_index(0)) is not None:
            self.list.remove(row)
        for c in calls:
            self.list.append(CallRow(self, c))
        self.calls_title.set_visible(bool(calls))
        self.list.set_visible(bool(calls))
        self._fill_voicebox()

    def _fill_voicebox(self):
        msgs = self.app.voicemails(self.dev.id) if self.dev else []
        self._prune_buttons()
        while (row := self.vb_list.get_row_at_index(0)) is not None:
            self.vb_list.remove(row)
        for m in msgs:
            self.vb_list.append(VoicemailRow(self, m))
        new = sum(1 for m in msgs if m["new"])
        self.vb_title.set_label(_("Voicebox") + (" · " + text.n_voicemails(new) if new else ""))
        self.vb_title.set_visible(bool(msgs))
        self.vb_list.set_visible(bool(msgs))
        self.status.set_title(_("No calls"))
        self.stack.set_visible_child_name(
            "list" if msgs or getattr(self, "calls", None) else "status")

    def voicebox_changed(self, reload_calls=True):
        self._fill_voicebox()
        if reload_calls and self._loaded_for is not None:
            self.load(force=True)

    # -- playing VoiceBox's recordings here, on the PC ---------------------------
    def _prune_buttons(self):
        self.play_buttons = {k: [b for b in v if b.get_root() is not None]
                             for k, v in self.play_buttons.items()}

    def play_button(self, mid):
        b = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text=_("Listen"))
        b.add_css_class("flat")
        b.set_icon_name("media-playback-stop-symbolic" if self.playing == mid
                        else "media-playback-start-symbolic")
        b.connect("clicked", lambda btn: self.toggle_play(mid))
        self.play_buttons.setdefault(mid, []).append(b)
        return b

    def _set_icons(self):
        for mid, buttons in self.play_buttons.items():
            for b in buttons:
                b.set_icon_name("media-playback-stop-symbolic" if mid == self.playing
                                else "media-playback-start-symbolic")

    def toggle_play(self, mid):
        if self.playing == mid:
            self.stop()
            return
        self.stop()
        dev = self.dev
        self.playing = mid
        self._set_icons()

        def got(path, error):
            if self.playing != mid:
                return
            if error is not None:
                self.playing = None
                self._set_icons()
                self.app.toast(text.error(error))
                return
            self.player = Gtk.MediaFile.new_for_filename(path)
            self.player.connect("notify::ended", self._on_ended)
            self.player.play()
            self.app.voicebox_read(dev, mid)

        self.app.voicebox_audio(dev, mid, got)

    def _on_ended(self, media, _pspec):
        if media.get_ended() and media is self.player:
            self.stop()

    def stop(self):
        if self.player is not None:
            self.player.pause()
            self.player = None
        self.playing = None
        self._set_icons()

    def delete_voicemail(self, msg):
        who = msg["name"] or msg["number"] or _("Withheld number")
        dialog = Adw.AlertDialog(heading=_("Delete the message from %s?") % who,
                                 body=_("It is deleted on the phone as well."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(d, response):
            if response == "delete":
                if self.playing == msg["id"]:
                    self.stop()
                self.app.voicebox_delete(self.dev, msg["id"])

        dialog.connect("response", answered)
        dialog.present(self.get_root())

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
