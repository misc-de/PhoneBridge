# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Overview: how the phone is doing, and the switches used most."""

from gi.repository import Adw, GLib, Gtk

from . import text
from .i18n import N_, _

POWER_PROFILES = (("power-saver", N_("Power saver")), ("balanced", N_("Balanced")),
                  ("performance", N_("Performance")))
FEEDBACK_PROFILES = (("full", N_("Sound and vibration")), ("quiet", N_("Vibration only")),
                     ("silent", N_("Silent")))


class OverviewPage(Adw.PreferencesPage):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.dev = None
        self._updating = False
        self._volume_source = 0

        state = Adw.PreferencesGroup(title=_("Status"))
        self.add(state)
        self.battery = Adw.ActionRow(title=_("Battery"))
        self.battery.add_prefix(Gtk.Image(icon_name="battery-symbolic"))
        self.level = Gtk.LevelBar(min_value=0, max_value=100, valign=Gtk.Align.CENTER,
                                  width_request=140)
        self.battery.add_suffix(self.level)
        self.mobile = Adw.ActionRow(title=_("Mobile network"))
        self.mobile.add_prefix(Gtk.Image(icon_name="network-cellular-symbolic"))
        self.wifi = Adw.ActionRow(title=_("Wi-Fi"))
        self.wifi.add_prefix(Gtk.Image(icon_name="network-wireless-symbolic"))
        self.conn = Adw.ActionRow(title=_("Connection"))
        self.conn.add_prefix(Gtk.Image(icon_name="network-transmit-receive-symbolic"))
        for row in (self.battery, self.mobile, self.wifi, self.conn):
            row.set_subtitle_selectable(True)
            state.add(row)

        quick = Adw.PreferencesGroup(title=_("Quick settings"))
        self.add(quick)
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

    def set_device(self, dev):
        self.dev = dev
        self.update()

    def update(self):
        dev = self.dev
        status = dev.status if dev is not None and dev.online else None
        s = status or {}
        self._updating = True
        try:
            bat = s.get("battery")
            self.battery.set_subtitle(text.battery(status) or "–")
            self.level.set_value(bat["percent"] if bat else 0)
            self.mobile.set_subtitle(text.network(status) or "–")
            self.wifi.set_subtitle(text.wifi(status) or "–")
            if dev is None:
                self.conn.set_subtitle("–")
            else:
                info = dev.info
                where = "%s@%s" % (info["user"], info["host"])
                if s.get("hostname"):
                    where = "%s (%s)" % (s["hostname"], where)
                self.conn.set_subtitle("%s · %s" % (text.device_state(dev), where))

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
        dialog.present(self.get_root())

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
