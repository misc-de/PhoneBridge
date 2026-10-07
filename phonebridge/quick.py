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
        self.feedback.set_model(Gtk.StringList.new([_(l) for _k, l in FEEDBACK_PROFILES]))
        self.feedback.connect("notify::selected", self._on_feedback)
        self.power = Adw.ComboRow(title=_("Power profile"))
        self.power.set_model(Gtk.StringList.new([_(l) for _k, l in POWER_PROFILES]))
        self.power.connect("notify::selected", self._on_power)

        self.find = Adw.ActionRow(title=_("Find phone"),
                                  subtitle=_("Rings like a call, even when silent"))
        self.ring = Gtk.Button(valign=Gtk.Align.CENTER)
        self.ring.connect("clicked", lambda *a: self.app.ring(self.dev))
        self.find.add_suffix(self.ring)

        for row in (self.data, self.wifi_switch, self.volume, self.feedback,
                    self.power, self.find):
            quick.add(row)

        # the call's sound on the PC - only where the phone has the nodes for it
        self.audio_group = Adw.PreferencesGroup(
            title=_("Calls at the PC"),
            description=_("During a call, “Sound on the PC” in the call bar puts the "
                          "caller on the PC's speakers and the PC's microphone on the "
                          "line; the phone's microphone is muted meanwhile."))
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
            self.audio_group.set_visible(dev is not None and self.app.call_audio_possible(dev))
            self.echo.set_active(bool(cfg["call_audio_echo"]))
            self.gain.set_value(float(cfg["call_audio_gain"]))
            self.auto.set_active(bool(cfg["call_audio_auto"]))
            audio = self.app.pc_audio.get(dev.id) if dev is not None else None
            testing = audio is not None and audio.test
            in_call = bool(dev is not None and self.app.calls.get(dev.id))
            self.test_button.set_label(_("Stop") if testing else _("Test"))
            self.test_button.set_sensitive(testing or (audio is None and not in_call))
            self.test_levels.set_visible(testing)
            if testing and not self._test_tick:
                self._test_tick = GLib.timeout_add(100, self._test_levels)
            ringing = dev is not None and dev.id in self.app.ringing
            self.ring.set_label(_("Stop") if ringing else _("Ring"))
            self.find.set_sensitive(status is not None)
        finally:
            self._updating = False

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
        if audio is None or not audio.test:
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
