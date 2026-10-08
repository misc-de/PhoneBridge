# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Phone: dial a number (the phone calls, PhoneBridge only starts it), the
call history of GNOME Calls (read only - Calls keeps it in memory and would
not notice a change), and the call in progress: answer or hang up."""

import time

from gi.repository import Adw, GLib, Gtk, Pango

from . import text
from .i18n import N_, _
from .widgets import opens_contact, section_label

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
    """A call - with VoiceBox's message when VoiceBox answered it, or a
    message alone when the call history has no entry for it."""

    def __init__(self, page, call):
        super().__init__(activatable=False)
        self.call = call
        app, dev = page.app, page.dev
        vb = call.get("voicebox")
        name = call["name"] or call["number"] or (_("Withheld number") if vb
                                                  else _("Unknown number"))
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=10,
                      margin_end=8)
        if call["number"] and not call["name"]:
            # an unknown caller: in the avatar's place, a button to keep them
            add = Gtk.Button(icon_name="contact-new-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text=_("Add to contacts"))
            add.add_css_class("circular")
            add.set_size_request(36, 36)
            add.connect("clicked", lambda b: app.new_contact(call["number"]))
            box.append(add)
        else:
            avatar = Adw.Avatar(size=36, text=call["name"], show_initials=bool(call["name"]))
            if call.get("avatar"):
                app.avatars.get(dev, call["avatar"], avatar.set_custom_image)
            box.append(opens_contact(avatar, app, call["number"]))
        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                        valign=Gtk.Align.CENTER)
        title = Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        # answered by VoiceBox: for the user it is a call they missed
        missed = (call["inbound"] and not call["answered"]) or bool(vb)
        if missed:
            title.add_css_class("error")
        unheard = bool(vb and vb.get("new") and vb["audio"])
        if unheard:
            title.add_css_class("thread-unread")
        lines.append(title)
        sub = Gtk.Box(spacing=4)
        icon = ("call-missed-symbolic" if missed else
                "call-incoming-symbolic" if call["inbound"] else "call-outgoing-symbolic")
        sub.append(Gtk.Image(icon_name=icon, pixel_size=12))
        parts = [text.activity(call["start"])] if call["start"] else []
        if vb and vb["audio"] and not vb["missed"]:
            # only what was recorded is worth a word; the colour says the rest
            parts.append(_("Voicebox: %s") % duration(int(round(vb["duration"]))))
            if vb.get("box_name"):
                parts.append(vb["box_name"])
        elif not vb and call["answered"] and call["duration"]:
            parts.append(duration(call["duration"]))
        info = Gtk.Label(label=" · ".join(parts), xalign=0)
        info.add_css_class("dim-label")
        info.add_css_class("caption")
        sub.append(info)
        lines.append(sub)
        box.append(lines)
        if unheard:
            badge = Gtk.Label(label=_("new"), valign=Gtk.Align.CENTER)
            badge.add_css_class("unread-badge")
            box.append(badge)
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
        if vb and vb["audio"]:
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text=_("Delete the voice message"))
            delete.add_css_class("flat")
            delete.connect("clicked", lambda b: page.delete_voicemail(vb, name))
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
        # the line to call on: SIM 1, SIM 2, SIP accounts
        self.line_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        line_title = Gtk.Label(label=_("Call over"), xalign=0)
        line_title.add_css_class("dim-label")
        line_title.add_css_class("caption")
        self.line_picker = Gtk.DropDown()
        self.line_picker.connect("notify::selected", self._on_line)
        self.line_box.append(line_title)
        self.line_box.append(self.line_picker)
        pad.append(self.line_box)
        self.number = Gtk.Entry(placeholder_text=_("Number"), xalign=0.5,
                                input_purpose=Gtk.InputPurpose.PHONE)
        self.number.add_css_class("title-2")
        self.number.connect("activate", lambda *a: self._dial())
        self.number.connect("changed", lambda *a: self._update())
        pad.append(self.number)
        self.match = Gtk.Label()
        self.match.add_css_class("dim-label")
        pad.append(self.match)
        self._line_ids = []
        self._picking_line = False
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

        # one card per section (today, yesterday, ...), only those with calls
        column = self.column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.calls = []
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
        content = Adw.NavigationPage(title=_("Recent calls"), child=view)
        self.split = Adw.NavigationSplitView(sidebar=sidebar, content=content, hexpand=True,
                                             min_sidebar_width=300, max_sidebar_width=340,
                                             show_content=True)
        self.append(self.split)
        self._update()

    def show_number(self, number):
        """A number in the dial pad, ready to call (a tel: link)."""
        self.number.set_text(number)
        self.split.set_show_content(False)
        self.number.grab_focus()
        self.number.set_position(-1)

    def set_device(self, dev):
        if dev is not self.dev:
            self.stop()
            self.dev = dev
            self._loaded_for = None
            self._serial += 1           # late answers of the last phone: not here
            self._fill([])
            self.lines_changed()
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
        self._refill()
        root = self.get_root()
        if calls and root is not None and hasattr(root, "overview"):
            root.overview.take_calls(calls)     # the overview follows at once

    def entries(self):
        """The calls, with VoiceBox's messages in their places: on the call
        they belong to, or - when the history has none - on their own."""
        dev = self.dev
        current = {m["id"]: m for m in (self.app.voicebox.get(dev.id, {}).get("messages", [])
                                        if dev else [])}
        out, matched = [], set()
        for c in self.calls:
            c = dict(c)
            vb = c.get("voicebox")
            if vb:
                m = current.get(vb["id"])
                if m is None:
                    c.pop("voicebox")           # deleted meanwhile
                else:
                    matched.add(vb["id"])
                    c["voicebox"] = self._vb(dev, m)
            out.append(c)
        for m in current.values():
            if m["id"] in matched or not m["audio"]:
                continue
            out.append({"number": m["number"], "name": m["name"], "inbound": True,
                        "answered": True, "start": m["time"], "duration": 0,
                        "avatar": m.get("avatar"), "voicebox": self._vb(dev, m)})
        out.sort(key=lambda c: c.get("start") or 0, reverse=True)
        return out

    def _vb(self, dev, m):
        vb = {"id": m["id"], "missed": m["missed"], "audio": m["audio"],
              "duration": m["duration"], "new": m["new"]}
        if m.get("box") and len(self.app.voicebox.get(dev.id, {}).get("boxes", [])) > 1:
            vb["box_name"] = self.app.voicebox_box_name(dev.id, m["box"])
        return vb

    def _refill(self):
        self._prune_buttons()
        while (child := self.column.get_first_child()) is not None:
            self.column.remove(child)
        entries = self.entries()
        now, card, group = time.time(), None, None
        for c in entries:
            g = text.day_group(c.get("start"), now)
            if card is None or g != group:
                label = section_label(g)
                label.set_margin_top(0 if card is None else 12)
                self.column.append(label)
                card = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
                card.add_css_class("boxed-list")
                self.column.append(card)
                group = g
            card.append(CallRow(self, c))
        self.status.set_title(_("No calls"))
        self.stack.set_visible_child_name("list" if entries else "status")

    def rows(self):
        """The CallRows, all sections in order."""
        out, card = [], self.column.get_first_child()
        while card is not None:
            if isinstance(card, Gtk.ListBox):
                i = 0
                while (row := card.get_row_at_index(i)) is not None:
                    out.append(row)
                    i += 1
            card = card.get_next_sibling()
        return out

    def voicebox_changed(self, reload_calls=True):
        self._refill()
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

    def delete_voicemail(self, vb, who):
        dialog = Adw.AlertDialog(heading=_("Delete the message from %s?") % who,
                                 body=_("It is deleted on the phone as well."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(d, response):
            if response == "delete":
                if self.playing == vb["id"]:
                    self.stop()
                self.app.voicebox_delete(self.dev, vb["id"])

        dialog.connect("response", answered)
        dialog.present(self.get_root())

    def lines_changed(self):
        dev = self.dev
        lines = self.app.lines.get(dev.id, []) if dev else []
        chosen = self.app.chosen_line(dev)
        self._picking_line = True
        self._line_ids = [l["id"] for l in lines]
        self.line_picker.set_model(Gtk.StringList.new([self.app.line_label(l) for l in lines]))
        if chosen is not None:
            self.line_picker.set_selected(self._line_ids.index(chosen["id"]))
        self._picking_line = False
        self.line_box.set_visible(bool(lines))
        # a single line is shown, not offered
        self.line_picker.set_sensitive(len(lines) > 1)

    def _on_line(self, *args):
        if self._picking_line or self.dev is None:
            return
        n = self.line_picker.get_selected()
        if 0 <= n < len(self._line_ids):
            self.app.choose_line(self.dev, self._line_ids[n])

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
        box.append(opens_contact(self.avatar, app,
                                 lambda: self.call and self.call.get("number")))
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
        # the sound on the PC: switch and both levels
        self.levels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                              valign=Gtk.Align.CENTER, width_request=90)
        self.level_in = Gtk.LevelBar(tooltip_text=_("Caller"))
        self.level_out = Gtk.LevelBar(tooltip_text=_("Your microphone"))
        self.levels.append(self.level_in)
        self.levels.append(self.level_out)
        self.pc = Gtk.ToggleButton(valign=Gtk.Align.CENTER,
                                   tooltip_text=_("Speak and listen at the PC; the phone's "
                                                  "microphone is muted meanwhile"))
        self.pc.set_child(Adw.ButtonContent(icon_name="audio-headset-symbolic",
                                            label=_("Sound on the PC")))
        self.pc.connect("toggled", self._on_pc)
        # the phone's loudspeaker instead of its earpiece, and back
        self.speaker = Gtk.ToggleButton(valign=Gtk.Align.CENTER,
                                        tooltip_text=_("The phone's loudspeaker instead of "
                                                       "its earpiece"))
        self.speaker.set_child(Adw.ButtonContent(icon_name="audio-speakers-symbolic",
                                                 label=_("Loudspeaker")))
        self.speaker.connect("toggled", self._on_speaker)
        box.append(self.levels)
        box.append(self.speaker)
        box.append(self.pc)
        box.append(self.answer)
        box.append(self.hangup)
        self.set_child(box)
        self.dev_id = None
        self._tick = 0
        self._level_tick = 0
        self._syncing = False

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
        possible = self.app.call_audio_possible(dev)
        audio = self.app.pc_audio.get(dev.id)
        on = audio is not None and not getattr(audio, "test", False)
        self.pc.set_visible(possible and call["state"] in ("active", "dialing", "alerting",
                                                           "held"))
        # what the phone reports; None: callaudiod is not there
        self.speaker.set_visible(not on and call.get("speaker") is not None
                                 and call["state"] in ("active", "dialing", "alerting", "held"))
        self._syncing = True
        self.pc.set_active(on)
        self.speaker.set_active(bool(call.get("speaker")))
        self._syncing = False
        self.levels.set_visible(on)
        if on and not self._level_tick:
            self._level_tick = GLib.timeout_add(100, self._update_levels)
        self.set_reveal_child(True)
        if call["state"] == "active" and not self._tick:
            self._tick = GLib.timeout_add_seconds(1, self._update_time)

    def _on_pc(self, button):
        if self._syncing:
            return
        dev = self.app.devices.get(self.dev_id)
        self.app.set_pc_audio(dev, button.get_active())

    def _on_speaker(self, button):
        if self._syncing or self.call is None:
            return
        dev = self.app.devices.get(self.dev_id)
        if dev is None:
            return
        on = button.get_active()

        def done(result, error):
            if error is not None:
                self.app.toast(text.error(error))
            elif self.call is not None:
                self.call["speaker"] = result
            if self.call is not None:       # back to what the phone has
                self._syncing = True
                self.speaker.set_active(bool(self.call.get("speaker")))
                self._syncing = False

        dev.request("call.speaker", {"on": on}, done)

    def _update_levels(self):
        audio = self.app.pc_audio.get(self.dev_id)
        if audio is None or getattr(audio, "test", True):
            self._level_tick = 0
            self.level_in.set_value(0)
            self.level_out.set_value(0)
            return False
        self.level_in.set_value(min(1.0, audio.level_in))
        self.level_out.set_value(min(1.0, audio.level_out))
        return True

    def _update_time(self):
        if self.call is None or self.call["state"] != "active":
            self._tick = 0
            return False
        self.state.set_label(call_state("active") + " · "
                             + duration(int(time.time() - self.call.get("since", time.time()))))
        return True
