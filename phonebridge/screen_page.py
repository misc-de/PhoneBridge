# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The page with the phone's screen, live: a click is a tap, dragging is a
swipe (Phosh's swipes from the edges too), holding is a long press, the
mouse wheel scrolls, and keys typed here are typed on the phone. The
picture runs only while the page is shown."""

from gi.repository import Adw, Gdk, GLib, Gtk

from . import config, text
from .i18n import N_, _

QUALITY_NAMES = (("fast", N_("Fast")), ("normal", N_("Normal")), ("sharp", N_("Sharp")))
KEY_POWER, KEY_VOLUMEDOWN, KEY_VOLUMEUP, KEY_LEFTMETA = 116, 114, 115, 125
WHEEL = 900                     # how far one notch of the wheel swipes (of 10000)
WHEEL_GATHER = 80               # ms the wheel's notches are gathered into one swipe


def settings(app):
    return app.cfg.setdefault("screen", {"quality": "normal"})


class ScreenPage(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.dev = None
        self.mirror = None
        self._pressed = set()           # keys held down on the phone
        self._wheel = [0.0, None]       # notches gathered, where the pointer was
        self._wheel_timer = 0
        self._dragging = False
        self._filling = False

        bar = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        for icon, tip, code in (
                ("system-shutdown-symbolic", _("Screen on or off (power button)"), KEY_POWER),
                ("view-app-grid-symbolic", _("Overview of the apps"), KEY_LEFTMETA),
                ("audio-volume-low-symbolic", _("Quieter"), KEY_VOLUMEDOWN),
                ("audio-volume-high-symbolic", _("Louder"), KEY_VOLUMEUP)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, c: self.key(c), code)
            bar.append(b)
        self.quality = Gtk.DropDown(model=Gtk.StringList.new([_(n) for _k, n in QUALITY_NAMES]),
                                    tooltip_text=_("Picture quality"))
        self.quality.connect("notify::selected", self._on_quality)
        bar.append(self.quality)
        self.append(bar)

        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
                                   hexpand=True, vexpand=True, focusable=True,
                                   cursor=Gdk.Cursor.new_from_name("pointer"))
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.picture.add_controller(drag)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.picture.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: setattr(self, "_pointer", (x, y)))
        self.picture.add_controller(motion)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        keys.connect("key-released", self._on_key_released)
        self.picture.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *a: self._release_keys())
        self.picture.add_controller(focus)
        self._pointer = (0.0, 0.0)

        self.status = Adw.StatusPage(icon_name="video-display-symbolic", vexpand=True)
        self.retry = Gtk.Button(halign=Gtk.Align.CENTER)
        self.retry.add_css_class("pill")
        self.retry.connect("clicked", lambda *a: self._on_retry())
        self.status.set_child(self.retry)
        self.view = Gtk.Stack(vexpand=True)
        self.view.add_named(self.picture, "picture")
        self.view.add_named(self.status, "status")
        self.append(self.view)
        self.hint = _("Click to tap, drag to swipe, hold for a long press. Keys typed here go "
                      "to the phone.")
        self.state = Gtk.Label(label=self.hint, wrap=True, justify=Gtk.Justification.CENTER,
                               margin_bottom=6, margin_start=12, margin_end=12)
        self.state.add_css_class("dim-label")
        self.state.add_css_class("caption")
        self.append(self.state)

        self.connect("map", lambda *a: self.load())
        self.connect("unmap", lambda *a: self.stop())

    # -- the phone ---------------------------------------------------------------------
    def set_device(self, dev):
        if dev is not self.dev:
            self.stop()
            self.dev = dev
            self.picture.set_paintable(None)
        self.device_changed()

    def device_changed(self):
        self._filling = True
        try:
            q = settings(self.app)["quality"]
            self.quality.set_selected(next((n for n, (k, _l) in enumerate(QUALITY_NAMES)
                                            if k == q), 1))
        finally:
            self._filling = False
        if self.dev is None or not self.dev.online:
            self.stop()
            self._show_status(_("Not connected"), self.dev and text.device_state(self.dev))
        elif self.get_mapped():
            self.load()

    def load(self, force=False):
        """Starts the picture - only while the page is shown."""
        dev = self.dev
        if not self.get_mapped() or dev is None or not dev.online:
            if dev is None or not dev.online:
                self._show_status(_("Not connected"), dev and text.device_state(dev))
            return
        if self.mirror is not None and not force:
            return
        self.stop()
        try:
            from .screen import Mirror
        except (ImportError, ValueError):           # no GStreamer on this PC
            self._show_status(_("GStreamer is missing on this PC"), None)
            return
        self.mirror = Mirror(dev, settings(self.app)["quality"], picture=self._show,
                             **self.app._screen_extra)
        self.mirror.connect("state", self._on_state)
        self.mirror.connect("stopped", self._on_stopped)
        self.state.set_label(self.hint)
        self._show_status(_("Connecting …"), None, spinner=True)
        if self.mirror.start():
            self.picture.grab_focus()

    def stop(self):
        if self._wheel_timer:
            GLib.source_remove(self._wheel_timer)
            self._wheel_timer = 0
        mirror, self.mirror = self.mirror, None
        if mirror is not None:
            self._release_keys(mirror)
            mirror.stop()

    def _on_quality(self, *args):
        if self._filling:
            return
        settings(self.app)["quality"] = QUALITY_NAMES[self.quality.get_selected()][0]
        config.save(self.app.cfg)
        if self.mirror is not None:
            self.load(force=True)       # again, with the new size

    # -- what the phone says -----------------------------------------------------------
    def _show(self, data, width, height):
        texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8A8,
                                        GLib.Bytes.new(data), width * 4)
        self.picture.set_paintable(texture)
        if self.view.get_visible_child_name() != "picture":
            self.view.set_visible_child_name("picture")
            self.picture.grab_focus()

    def _on_state(self, mirror, state):
        if mirror is not self.mirror:
            return
        if state == "off":
            self._show_status(_("The phone's screen is off"), None, button=_("Switch on"))
        elif state == "on":
            if self.picture.get_paintable() is None:
                self._show_status(_("Waiting for the first picture …"), None, spinner=True)
        elif state.startswith("error no touch"):
            self.state.set_label(_("Only the picture - the phone takes no taps: %s")
                                 % state[len("error no touch: "):])
        elif state.startswith("error "):
            self.state.set_label(text.error(state[6:]))

    def _on_stopped(self, mirror, reason):
        if mirror is not self.mirror:
            return
        self.mirror = None
        if reason == "again" and (mirror.frames or mirror.state == "off"):
            self.load()                 # the screen came back on: a new stream
        elif reason is not None:
            self._show_status(_("The picture stopped"), text.error(reason),
                              button=_("Try again"))

    def _show_status(self, title, description, button=None, spinner=False):
        self.status.set_title(title)
        self.status.set_description(description)
        if spinner and hasattr(Adw, "SpinnerPaintable"):        # libadwaita 1.6
            self.status.set_paintable(Adw.SpinnerPaintable(widget=self.status))
        else:
            self.status.set_paintable(None)
            self.status.set_icon_name("video-display-symbolic")
        self.retry.set_visible(button is not None)
        if button is not None:
            self.retry.set_label(button)
        self.view.set_visible_child_name("status")

    def _on_retry(self):
        if self.mirror is not None and self.mirror.state == "off":
            self.key(KEY_POWER)         # the screen on: the picture comes by itself
        else:
            self.load(force=True)

    # -- taps, swipes and keys ---------------------------------------------------------
    def _point(self, x, y, clamp=False):
        from .screen import to_phone
        texture = self.picture.get_paintable()
        if texture is None:
            return None
        return to_phone(x, y, (self.picture.get_width(), self.picture.get_height()),
                        (texture.get_intrinsic_width(), texture.get_intrinsic_height()), clamp)

    def _on_drag_begin(self, gesture, x, y):
        self.picture.grab_focus()
        p = self._point(x, y)
        if self.mirror is None or p is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._dragging = True
        self.mirror.down(*p)

    def _on_drag_update(self, gesture, dx, dy):
        ok, x, y = gesture.get_start_point()
        p = self._point(x + dx, y + dy, clamp=True) if ok else None
        if self._dragging and self.mirror is not None and p is not None:
            self.mirror.move(*p)

    def _on_drag_end(self, gesture, dx, dy):
        if self._dragging:
            self._dragging = False
            self._on_drag_update(gesture, dx, dy)
            if self.mirror is not None:
                self.mirror.up()

    def _on_scroll(self, controller, dx, dy):
        """The wheel scrolls: its notches, gathered for a moment, are one
        swipe at the pointer - the content moves as the wheel says."""
        if self.mirror is None or self._dragging:
            return False
        self._wheel[0] += dy
        self._wheel[1] = self._pointer
        if not self._wheel_timer:
            self._wheel_timer = GLib.timeout_add(WHEEL_GATHER, self._wheel_swipe)
        return True

    def _wheel_swipe(self):
        self._wheel_timer = 0
        notches, (x, y) = self._wheel[0], self._wheel[1]
        self._wheel[0] = 0.0
        p = self._point(x, y)
        if self.mirror is None or p is None or not notches:
            return False
        dy = max(-6000, min(6000, -notches * WHEEL))
        start = max(0, min(10000, p[1] - dy // 2))     # around the pointer
        self.mirror.swipe(p[0], start, 0, dy, 250)
        return False

    def key(self, code):
        if self.mirror is not None:
            self.mirror.key(code)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if self.mirror is None or keycode < 9:
            return False
        code = keycode - 8              # X / Wayland keycodes are the kernel's + 8
        self._pressed.add(code)
        self.mirror.key(code, "press")
        return True

    def _on_key_released(self, controller, keyval, keycode, state):
        code = keycode - 8
        if self.mirror is not None and code in self._pressed:
            self._pressed.discard(code)
            self.mirror.key(code, "release")

    def _release_keys(self, mirror=None):
        """No key stays down on the phone when the focus or the page goes."""
        mirror = mirror or self.mirror
        for code in sorted(self._pressed):
            if mirror is not None:
                mirror.key(code, "release")
        self._pressed.clear()
