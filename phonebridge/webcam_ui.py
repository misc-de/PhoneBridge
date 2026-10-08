# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""In the phone's settings: the phone as the PC's webcam - on/off, which
camera, how sharp, mirrored; where it shows, and setting it up for every
program."""

import time

from gi.repository import Adw, Gdk, GLib, Gtk

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
        self.test_row = Adw.ActionRow(title=_("Test"), subtitle=_(
            "The live picture, as programs get it"))
        test = Gtk.Button(label=_("Test …"), valign=Gtk.Align.CENTER)
        test.connect("clicked", lambda *a: self.test())
        self.test_row.add_suffix(test)
        self.where = Adw.ActionRow(title=_("Shown as"))
        self.setup_button = Gtk.Button(label=_("For every program …"), valign=Gtk.Align.CENTER)
        self.setup_button.connect("clicked", lambda *a: self.ask_setup())
        self.where.add_suffix(self.setup_button)
        for row in (self.switch, self.camera, self.quality, self.mirror, self.test_row,
                    self.where):
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
            self.test_row.set_sensitive(dev is not None and dev.online)
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

    def test(self):
        if self.dev is None or not self.dev.online:
            return None
        dialog = TestDialog(self.app, self.dev)
        dialog.present(self.get_root())
        return dialog


class TestDialog(Adw.Dialog):
    """The webcam's live picture - switched on for the test if it was off,
    off again afterwards."""

    def __init__(self, app, dev):
        from . import webcam
        super().__init__(title=_("Webcam test"), content_width=700, content_height=500)
        self.app, self.dev = app, dev
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
                                   vexpand=True, margin_top=12, margin_start=12, margin_end=12)
        self.state = Gtk.Label(label=_("Waiting for the first picture …"), wrap=True,
                               justify=Gtk.Justification.CENTER, margin_top=6,
                               margin_bottom=12, margin_start=12, margin_end=12)
        self.state.add_css_class("dim-label")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.picture)
        box.append(self.state)
        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.started_here = dev.id not in app.webcams
        self.size = None
        self._last = (0, 0.0)
        if self.started_here:
            app.set_webcam(dev, True)
        cam = app.webcams.get(dev.id)
        if cam is None:
            self.state.set_label(_("The webcam did not start"))
        else:
            cam.preview = self._show
            self._last = (cam.frames, time.monotonic())
        self._timer = GLib.timeout_add(1000, self._tick)
        self.connect("closed", lambda *a: self._end())
        self.where = webcam.LABEL

    def _show(self, data, width, height):
        texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8A8,
                                        GLib.Bytes.new(data), width * 4)
        self.picture.set_paintable(texture)
        self.size = (width, height)
        return False

    def _tick(self):
        cam = self.app.webcams.get(self.dev.id)
        if cam is None:
            self.state.set_label(_("The webcam stopped"))
            self._timer = 0
            return False
        frames, then = self._last
        now = time.monotonic()
        fps = (cam.frames - frames) / max(now - then, 0.001)
        self._last = (cam.frames, now)
        if cam.frames == 0:
            return True
        w, h = cam.params["width"], cam.params["height"]
        where = (_("Programs see it as “%(name)s” (%(device)s)")
                 % {"name": self.where, "device": cam.target}
                 if cam.target.startswith("/dev/") else
                 _("Programs that take PipeWire cameras see it as “%s”") % self.where)
        self.state.set_label("%d×%d · %.0f %s\n%s" % (w, h, fps, _("pictures a second"), where))
        return True

    def _end(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        cam = self.app.webcams.get(self.dev.id)
        if cam is not None:
            cam.preview = None
        if self.started_here:
            self.app.set_webcam(self.dev, False)
