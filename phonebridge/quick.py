# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The switches used most - mobile data, Wi-Fi, volume, profiles, finding
the phone - and calls at the PC: groups at the top of the settings page."""

from gi.repository import Adw, GLib, Gtk

from . import text
from .i18n import N_, _

POWER_PROFILES = (("power-saver", N_("Power saver")), ("balanced", N_("Balanced")),
                  ("performance", N_("Performance")))
FEEDBACK_PROFILES = (("full", N_("Sound and vibration")), ("quiet", N_("Vibration only")),
                     ("silent", N_("Silent")))


class QuickSettings:
    """Not a page: its groups go into one (self.groups)."""

    def __init__(self, app):
        self.groups = []
        self.app = app
        self.dev = None
        self._updating = False
        self._volume_source = 0

        quick = Adw.PreferencesGroup(title=_("Quick settings"))
        self.groups.append(quick)
        self.data = Adw.SwitchRow(title=_("Mobile data"))
        self.data.connect("notify::active", self._on_data)
        self.wifi_switch = Adw.SwitchRow(title=_("Wi-Fi"))
        self.wifi_switch.connect("notify::active", self._on_wifi)

        self.volume = Adw.ActionRow(title=_("Volume"))
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale.set_hexpand(True)
        self.scale.set_draw_value(False)
        self.scale.set_valign(Gtk.Align.CENTER)
        self.scale.set_size_request(180, -1)
        self.scale.connect("value-changed", self._on_volume)
        self.mute = Gtk.ToggleButton(icon_name="audio-volume-muted-symbolic",
                                     valign=Gtk.Align.CENTER, tooltip_text=_("Mute"))
        self.mute.add_css_class("flat")
        self.mute.connect("toggled", self._on_mute)
        self.volume.add_suffix(self.scale)
        self.volume.add_suffix(self.mute)

        self.feedback = Adw.ComboRow(title=_("Ring profile"))
        self.feedback.set_model(Gtk.StringList.new([_(label) for _k, label in FEEDBACK_PROFILES]))
        self.feedback.connect("notify::selected", self._on_feedback)
        self.power = Adw.ComboRow(title=_("Power profile"))
        self.power.set_model(Gtk.StringList.new([_(label) for _k, label in POWER_PROFILES]))
        self.power.connect("notify::selected", self._on_power)

        self.hotspot = Adw.SwitchRow(title=_("Hotspot"))
        self.hotspot.connect("notify::active", self._on_hotspot)
        self.hotspot_pc = Adw.ActionRow(title=_("This PC in the hotspot"))
        self.hotspot_join = Gtk.Button(label=_("Set up …"), valign=Gtk.Align.CENTER)
        self.hotspot_join.connect("clicked", lambda *a: self.ask_join())
        self.hotspot_pc.add_suffix(self.hotspot_join)
        self._hotspot = None            # the phone's hotspot (hotspot.state)
        self._hotspot_for = None
        self._new_password = {}         # device id -> the password of a hotspot just made

        self.find = Adw.ActionRow(title=_("Find phone"),
                                  subtitle=_("Rings like a call, even when silent"))
        self.ring = Gtk.Button(valign=Gtk.Align.CENTER)
        self.ring.connect("clicked", lambda *a: self.app.ring(self.dev))
        self.find.add_suffix(self.ring)

        for row in (self.data, self.wifi_switch, self.hotspot, self.hotspot_pc, self.volume,
                    self.feedback, self.power, self.find):
            quick.add(row)

        # the call's sound on the PC - only where the phone has the nodes for it
        self.audio_intro = GLib.markup_escape_text(_(
            "During a call, “Sound on the PC” in the call bar puts the caller on the "
            "PC's speakers and the PC's microphone on the line; the phone's microphone "
            "is muted meanwhile."))
        self.audio_group = Adw.PreferencesGroup(title=_("Calls at the PC"),
                                                description=self.audio_intro)
        self.groups.append(self.audio_group)
        self.echo = Adw.SwitchRow(title=_("Echo cancellation"),
                                  subtitle=_("Needed with speakers, not with a headset"))
        self.echo.connect("notify::active", self._on_audio_setting)
        self.gain = Adw.SpinRow.new_with_range(1.0, 8.0, 0.5)
        self.gain.set_title(_("Caller's volume"))
        self.gain.set_subtitle(_("The phone delivers the caller quietly"))
        self.gain.set_digits(1)
        self.gain.connect("notify::value", self._on_audio_setting)
        self.auto = Adw.SwitchRow(title=_("Always take calls to the PC"),
                                  subtitle=_("As soon as a call is connected"))
        self.auto.connect("notify::active", self._on_audio_setting)
        self.test = Adw.ActionRow(
            title=_("Test the sound path"),
            subtitle=_("Without a call: the PC's microphone on the phone's speaker, the "
                       "phone's microphone on the PC. Keep them apart, or it whistles."))
        self.test_levels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                                   valign=Gtk.Align.CENTER, width_request=90)
        self.test_in = Gtk.LevelBar(tooltip_text=_("Phone's microphone"))
        self.test_out = Gtk.LevelBar(tooltip_text=_("Your microphone"))
        self.test_levels.append(self.test_in)
        self.test_levels.append(self.test_out)
        self.test_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.test_button.connect("clicked", self._on_test)
        self.test.add_suffix(self.test_levels)
        self.test.add_suffix(self.test_button)
        for row in (self.echo, self.gain, self.auto, self.test):
            self.audio_group.add(row)
        self._test_tick = 0

    def set_device(self, dev):
        self.dev = dev
        self.update()

    def update(self):
        dev = self.dev
        status = dev.status if dev is not None and dev.online else None
        s = status or {}
        self._updating = True
        try:
            self._show(self.data, s.get("mobile_data") is not None)
            self.data.set_active(bool(s.get("mobile_data")))
            wifi = s.get("wifi")
            self._show(self.wifi_switch, wifi is not None)
            self.wifi_switch.set_active(bool(wifi and wifi.get("enabled")))
            vol = s.get("volume")
            self._show(self.volume, vol is not None)
            if vol and not self._volume_source:
                self.scale.set_value(round(vol["level"] * 100))
                self.mute.set_active(vol["muted"])
            self._select(self.feedback, FEEDBACK_PROFILES, s.get("feedback_profile"))
            self._select(self.power, POWER_PROFILES, s.get("power_profile"))
            cfg = self.app.cfg
            # shown while connected; without PipeWire on the phone (or its
            # call audio nodes) it says why, and nothing can be switched
            problem = self.app.call_audio_problem(dev)
            self.audio_group.set_visible(dev is not None and dev.online)
            self.audio_group.set_description(
                self.audio_intro if problem is None else
                GLib.markup_escape_text(_("Not possible right now: %s") % problem))
            for row in (self.echo, self.gain, self.auto, self.test):
                row.set_sensitive(problem is None)
            self.echo.set_active(bool(cfg["call_audio_echo"]))
            self.gain.set_value(float(cfg["call_audio_gain"]))
            self.auto.set_active(bool(cfg["call_audio_auto"]))
            audio = self.app.pc_audio.get(dev.id) if dev is not None else None
            testing = audio is not None and getattr(audio, "test", False)
            in_call = bool(dev is not None and self.app.calls.get(dev.id))
            self.test_button.set_label(_("Stop") if testing else _("Test"))
            self.test_button.set_sensitive(testing or (audio is None and not in_call))
            self.test_levels.set_visible(testing)
            if testing and not self._test_tick:
                self._test_tick = GLib.timeout_add(100, self._test_levels)
            self._update_hotspot()
            ringing = dev is not None and dev.id in self.app.ringing
            self.ring.set_label(_("Stop") if ringing else _("Ring"))
            self.find.set_sensitive(status is not None)
        finally:
            self._updating = False

    # -- the hotspot ------------------------------------------------------------
    def _update_hotspot(self):
        dev = self.dev
        online = dev is not None and dev.online
        if online and self._hotspot_for is not dev:
            self._hotspot_for = dev
            dev.request("hotspot.state", {}, lambda r, e, d=dev: self._got_hotspot(d, r, e))
        if not online:
            self._hotspot_for = None
        h = self._hotspot if online else None
        self.hotspot.set_visible(h is not None)
        self.hotspot_pc.set_visible(h is not None and h["exists"])
        if h is None:
            return
        self.hotspot.set_active(h["active"])
        self.hotspot.set_subtitle(GLib.markup_escape_text(h["ssid"]) if h["exists"] else
                                  _("Not set up yet - switching it on makes one"))
        joined = dev.info.get("hotspot_ssid") == h["ssid"] and h["ssid"]
        self.hotspot_pc.set_subtitle(
            _("Joins by itself when no other known Wi-Fi is there") if joined
            else _("Not yet - PhoneBridge needs the hotspot's password once"))
        self.hotspot_join.set_label(_("Change …") if joined else _("Set up …"))

    def _got_hotspot(self, dev, result, error):
        if dev is not self.dev:
            return
        self._hotspot = result if error is None else None
        self.update()

    def _on_hotspot(self, *args):
        if self._updating or self._hotspot is None:
            return
        on = self.hotspot.get_active()
        dialog = Adw.AlertDialog(
            heading=_("Switch on the hotspot?") if on else _("Switch off the hotspot?"),
            body=_("The phone shares its mobile data. Some phones leave their Wi-Fi "
                   "network meanwhile - PhoneBridge then reaches the phone only through "
                   "the hotspot.") if on else
            _("Devices in the hotspot lose their connection - this PC too, if it is in it."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("go", _("Switch on") if on else _("Switch off"))
        dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED if on
                                       else Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dev = self.dev

        def answered(d, response):
            if response != "go":
                self.update()
                return

            def done(result, error):
                if error is not None:
                    self.app.toast(text.error(error))
                else:
                    if result.get("password"):
                        self._new_password[dev.id] = result.pop("password")
                    self._hotspot = result
                self.update()

            dev.request("hotspot.set", {"on": on}, done)

        dialog.connect("response", answered)
        dialog.present(self.groups[0].get_root())

    def ask_join(self):
        """The hotspot among this PC's networks - the password once."""
        from . import hotspot
        dev, h = self.dev, self._hotspot
        if dev is None or not h or not h["exists"]:
            return
        dialog = Adw.AlertDialog(
            heading=_("This PC in “%s”") % h["ssid"],
            body=_("PhoneBridge keeps the hotspot among this PC's networks with a low "
                   "priority: the PC joins it by itself whenever it is on and no other "
                   "known Wi-Fi is there, and reaches the phone through it. The password "
                   "is in the phone's settings under Wi-Fi → Hotspot."))
        entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True,
                                  text=self._new_password.get(dev.id, ""))
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        entry.connect("changed", lambda *a: dialog.set_response_enabled(
            "save", len(entry.get_text()) >= 8))
        dialog.set_response_enabled("save", len(entry.get_text()) >= 8)

        def saved(error, ssid=h["ssid"]):
            if error is not None:
                self.app.toast(_("Not saved: %s") % error)
                return
            self._new_password.pop(dev.id, None)
            devices = [dict(d, hotspot_ssid=ssid) if d["id"] == dev.id else d
                       for d in self.app.cfg["devices"]]
            self.app.toast(_("This PC joins “%s” by itself from now on") % ssid)
            self.app.set_devices(devices)

        dialog.connect("response", lambda d, r: r == "save" and hotspot.pc_add_profile(
            h["ssid"], entry.get_text(), saved))
        dialog.present(self.groups[0].get_root())

    def _show(self, row, available):
        row.set_visible(self.dev is None or not self.dev.online or available)
        row.set_sensitive(available)

    def _select(self, row, choices, value):
        keys = [k for k, _l in choices]
        row.set_visible(value is None and (self.dev is None or not self.dev.online)
                        or value in keys)
        row.set_sensitive(value in keys)
        if value in keys:
            row.set_selected(keys.index(value))

    def _test_levels(self):
        audio = self.app.pc_audio.get(self.dev.id) if self.dev is not None else None
        if audio is None or not getattr(audio, "test", False):
            self._test_tick = 0
            return False
        self.test_in.set_value(min(1.0, audio.level_in))
        self.test_out.set_value(min(1.0, audio.level_out))
        return True

    def _on_test(self, *args):
        audio = self.app.pc_audio.get(self.dev.id) if self.dev is not None else None
        self.app.set_pc_audio(self.dev, audio is None, test=True)

    def _on_audio_setting(self, *args):
        if self._updating:
            return
        from . import config
        cfg = self.app.cfg
        cfg["call_audio_echo"] = self.echo.get_active()
        cfg["call_audio_gain"] = self.gain.get_value()
        cfg["call_audio_auto"] = self.auto.get_active()
        config.save(cfg)
        for audio in self.app.pc_audio.values():
            audio.gain = cfg["call_audio_gain"]     # takes effect at once

    # -- changes ----------------------------------------------------------
    def _request(self, cmd, args):
        if self.dev is None:
            return

        def done(result, error):
            if error is not None:
                self.app.toast(text.error(error))
                self.update()

        self.dev.request(cmd, args, done)

    def _on_data(self, *args):
        if not self._updating:
            self._request("data.set", {"on": self.data.get_active()})

    def _on_wifi(self, *args):
        if self._updating:
            return
        if self.wifi_switch.get_active():
            self._request("wifi.set", {"on": True})
            return
        dialog = Adw.AlertDialog(
            heading=_("Switch off Wi-Fi?"),
            body=_("If PhoneBridge reaches the phone over Wi-Fi, the connection "
                   "drops and only comes back once Wi-Fi is on again – on the phone."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("off", _("Switch off"))
        dialog.set_response_appearance("off", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(d, response):
            if response == "off":
                self._request("wifi.set", {"on": False})
            else:
                self.update()

        dialog.connect("response", answered)
        dialog.present(self.groups[0].get_root())

    def _on_volume(self, *args):
        if self._updating:
            return
        if self._volume_source:
            GLib.source_remove(self._volume_source)
        self._volume_source = GLib.timeout_add(250, self._send_volume)

    def _send_volume(self):
        self._volume_source = 0
        self._request("volume.set", {"level": self.scale.get_value() / 100})
        return False

    def _on_mute(self, *args):
        if not self._updating:
            self._request("volume.set", {"muted": self.mute.get_active()})

    def _on_feedback(self, *args):
        if not self._updating:
            key = FEEDBACK_PROFILES[self.feedback.get_selected()][0]
            self._request("feedback.set", {"profile": key})

    def _on_power(self, *args):
        if not self._updating:
            key = POWER_PROFILES[self.power.get_selected()][0]
            self._request("power.set", {"profile": key})
