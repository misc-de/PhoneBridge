# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""In the phone's settings: the phone as the PC's webcam - on/off, which
camera, how sharp, mirrored; where it shows, and setting it up for every
program."""

from gi.repository import Adw, Gtk

from . import config
from .i18n import N_, _

CAMERAS = ((0, N_("Back camera")), (1, N_("Front camera")))
QUALITY_NAMES = (("480p", "480p"), ("720p", "720p"), ("1080p", "1080p"))


def settings(app):
    return app.cfg.setdefault("webcam", {"camera": 1, "quality": "720p", "mirror": True})


class WebcamGroup(Adw.PreferencesGroup):
    def __init__(self, app):
        super().__init__(title=_("Webcam"), description=_(
            "The phone's camera as a webcam of this PC - for video calls in the browser, "
            "Zoom, Teams, OBS … The camera is on only while this is switched on."))
        self.app = app
        self.dev = None
        self._filling = False
        self.switch = Adw.SwitchRow(title=_("Use the phone as webcam"))
        self.switch.connect("notify::active", self._on_switch)
        self.camera = Adw.ComboRow(title=_("Camera"),
                                   model=Gtk.StringList.new([_(n) for _i, n in CAMERAS]))
        self.camera.connect("notify::selected", self._on_choice)
        self.quality = Adw.ComboRow(title=_("Quality"),
                                    model=Gtk.StringList.new([n for _k, n in QUALITY_NAMES]))
        self.quality.connect("notify::selected", self._on_choice)
        self.mirror = Adw.SwitchRow(title=_("Mirror the picture"))
        self.mirror.connect("notify::active", self._on_choice)
        self.where = Adw.ActionRow(title=_("Shown as"))
        self.setup_button = Gtk.Button(label=_("For every program …"), valign=Gtk.Align.CENTER)
        self.setup_button.connect("clicked", lambda *a: self.ask_setup())
        self.where.add_suffix(self.setup_button)
        for row in (self.switch, self.camera, self.quality, self.mirror, self.where):
            self.add(row)
        app.webcam_listeners.append(lambda dev: dev is self.dev and self.update())

    def set_device(self, dev):
        self.dev = dev
        self.update()

    def update(self):
        try:
            from . import webcam
        except (ImportError, ValueError):           # no GStreamer on this PC
            self.set_sensitive(False)
            self.where.set_subtitle(_("GStreamer is missing on this PC"))
            self.setup_button.set_visible(False)
            return
        dev = self.dev
        s = settings(self.app)
        self._filling = True
        try:
            running = dev is not None and dev.id in self.app.webcams
            self.switch.set_active(running)
            self.switch.set_sensitive(dev is not None and dev.online)
            self.camera.set_selected(next((n for n, (i, _l) in enumerate(CAMERAS)
                                           if i == s["camera"]), 0))
            self.quality.set_selected(next((n for n, (k, _l) in enumerate(QUALITY_NAMES)
                                            if k == s["quality"]), 1))
            self.mirror.set_active(bool(s["mirror"]))
        finally:
            self._filling = False
        device = webcam.loopback_device()
        if device:
            self.where.set_subtitle(_("“%(name)s” in every program (%(device)s)")
                                    % {"name": webcam.LABEL, "device": device})
        else:
            self.where.set_subtitle(_("A PipeWire camera - seen by OBS, GNOME Snapshot and "
                                      "browsers that take PipeWire cameras"))
        self.setup_button.set_visible(device is None)

    def _on_switch(self, *args):
        if not self._filling and self.dev is not None:
            self.app.set_webcam(self.dev, self.switch.get_active())

    def _on_choice(self, *args):
        if self._filling:
            return
        s = settings(self.app)
        s["camera"] = CAMERAS[self.camera.get_selected()][0]
        s["quality"] = QUALITY_NAMES[self.quality.get_selected()][0]
        s["mirror"] = self.mirror.get_active()
        config.save(self.app.cfg)
        if self.dev is not None and self.dev.id in self.app.webcams:
            self.app.set_webcam(self.dev, True)      # again, with the new settings

    def ask_setup(self):
        from . import webcam
        if not webcam.loopback_installed():
            hint = webcam.install_hint()
            dialog = Adw.AlertDialog(
                heading=_("A webcam for every program"),
                body=_("For that this PC needs the kernel module v4l2loopback, which is not "
                       "installed. Until then the phone shows as a PipeWire camera."))
            if hint:
                label = Gtk.Label(label=hint, selectable=True, wrap=True)
                label.add_css_class("monospace")
                dialog.set_extra_child(label)
            dialog.add_response("ok", _("OK"))
            dialog.present(self.get_root())
            return dialog
        dialog = Adw.AlertDialog(
            heading=_("A webcam for every program"),
            body=_("PhoneBridge makes a virtual camera “%s” with v4l2loopback. That needs "
                   "the administrator's rights once - the system asks for the password.")
            % webcam.LABEL)
        persistent = Gtk.CheckButton(label=_("Also at every start of the PC"), active=True)
        dialog.set_extra_child(persistent)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("go", _("Set up"))
        dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def done(error):
            if error is not None:
                self.app.toast(_("Not set up: %s") % error)
            else:
                self.app.toast(_("“%s” is there now") % webcam.LABEL)
                if self.dev is not None and self.dev.id in self.app.webcams:
                    self.app.set_webcam(self.dev, True)  # over to the new device
            self.update()

        dialog.connect("response", lambda d, r: r == "go" and webcam.set_up(
            persistent.get_active(), done))
        dialog.present(self.get_root())
        return dialog
