# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The page with the phone's screen, live: a click is a tap, dragging is a
swipe (Phosh's swipes from the edges too), holding is a long press, the
mouse wheel scrolls, and keys typed here are typed on the phone. The
picture runs only while the page is shown. Full screen shows nothing but
the picture (Esc goes back); a button turns the phone between portrait
and landscape.

What it needs on the phone and on this PC is checked: without the
recorder or the decoder the page lists it, with the commands that set it
up; without what is only for taps or turning the picture comes, and a
button in the bar lists it.

The other way, chosen at the top: a GNOME desktop of the phone's own in
an RDP window (rdp_ui.py)."""

from gi.repository import Adw, Gdk, GLib, Gtk

from . import config, text
from .i18n import N_, _
from .needs import NeedsGroup, explain, on_the_phone

QUALITY_NAMES = (("fast", N_("Fast")), ("normal", N_("Normal")), ("sharp", N_("Sharp")))
KEY_POWER, KEY_VOLUMEDOWN, KEY_VOLUMEUP, KEY_LEFTMETA = 116, 114, 115, 125
WHEEL = 900                     # how far one notch of the wheel swipes (of 10000)
UNMAP_GRACE = 600               # ms the page may be away before the picture stops
WHEEL_GATHER = 80               # ms the wheel's notches are gathered into one swipe


def settings(app):
    return app.cfg.setdefault("screen", {"quality": "normal"})


def pc_missing():
    """What this PC lacks for the picture: GStreamer elements - or
    ["GStreamer"] without GStreamer at all."""
    try:
        from .screen import missing
    except (ImportError, ValueError):
        return ["GStreamer"]
    return missing()


def gstreamer_hint(release="/etc/os-release"):
    """The command that installs what the picture needs here, or None (the
    Flatpak has it all)."""
    from . import deps
    return {"arch": "sudo pacman -S gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav",
            "debian": "sudo apt install gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base "
                      "gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav",
            "fedora": "sudo dnf install gstreamer1-plugins-base gstreamer1-plugins-good "
                      "gstreamer1-plugins-bad-free gstreamer1-plugin-libav",
            "suse": "sudo zypper install typelib-1_0-Gst-1_0 gstreamer-plugins-base "
                    "gstreamer-plugins-good gstreamer-plugins-bad gstreamer-plugins-libav",
            }.get(deps.distro(release))


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
        self._online = None

        # how: the phone's screen, or a desktop of its own
        self.switch = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=6)
        self.switch.add_css_class("linked")
        self.mirror_mode = Gtk.ToggleButton(label=_("Screen sharing"))
        self.desktop_mode = Gtk.ToggleButton(label=_("Desktop session (RDP)"),
                                             group=self.mirror_mode)
        for b in (self.mirror_mode, self.desktop_mode):
            b.connect("toggled", self._on_mode)
            self.switch.append(b)
        self.append(self.switch)
        self.mirror_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

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
        self.rotate_button = Gtk.Button(icon_name="object-rotate-right-symbolic",
                                        tooltip_text=_("Portrait or landscape"))
        self.rotate_button.add_css_class("flat")
        self.rotate_button.connect("clicked", lambda *a: self.rotate())
        bar.append(self.rotate_button)
        full = Gtk.Button(icon_name="view-fullscreen-symbolic",
                          tooltip_text=_("Full screen (Esc goes back)"))
        full.add_css_class("flat")
        full.connect("clicked", lambda *a: self.set_fullscreen(True))
        bar.append(full)
        self.quality = Gtk.DropDown(model=Gtk.StringList.new([_(n) for _k, n in QUALITY_NAMES]),
                                    tooltip_text=_("Picture quality"))
        self.quality.connect("notify::selected", self._on_quality)
        bar.append(self.quality)
        # only taps or turning missing: the picture comes, this says what
        self.missing_list = self._needs_group()
        self.missing_list.set_size_request(420, -1)
        self.missing_button = Gtk.MenuButton(
            icon_name="dialog-warning-symbolic", visible=False,
            tooltip_text=_("Something is missing on the phone"),
            popover=Gtk.Popover(child=self.missing_list))
        self.missing_button.add_css_class("flat")
        bar.append(self.missing_button)
        self.bar = bar
        self.mirror_box.append(bar)

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
        # the recorder or the decoder missing: no picture, but what to do
        self.needs = self._needs_group(description=_(
            "Screen sharing needs these on the phone and on this PC. The picture comes once "
            "the first two are there; the others are for taps and turning."))
        needs_page = Adw.PreferencesPage()
        needs_page.add(self.needs)
        self.view.add_named(needs_page, "needs")
        self.mirror_box.append(self.view)
        self.hint = _("Click to tap, drag to swipe, hold for a long press. Keys typed here go "
                      "to the phone.")
        self.state = Gtk.Label(label=self.hint, wrap=True, justify=Gtk.Justification.CENTER,
                               margin_bottom=6, margin_start=12, margin_end=12)
        self.state.add_css_class("dim-label")
        self.state.add_css_class("caption")
        self.mirror_box.append(self.state)

        from .rdp_ui import DesktopPanel
        self.desktop = DesktopPanel(app)
        self.modes = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.modes.add_named(self.mirror_box, "mirror")
        self.modes.add_named(self.desktop, "desktop")
        self.append(self.modes)

        # Esc ends full screen - before the picture would send it to the phone
        esc = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        esc.connect("key-pressed", self._on_escape)
        self.add_controller(esc)
        self._window = None
        self._window_handler = 0
        self._full = False      # our own word: a window manager may confirm it late

        self.connect("map", lambda *a: self._on_map())
        self.connect("unmap", lambda *a: self._on_unmap())

    def _on_map(self):
        if self._unmap_timer:
            GLib.source_remove(self._unmap_timer)
            self._unmap_timer = 0
        root = self.get_root()
        if isinstance(root, Gtk.Window) and root is not self._window:
            self._forget_window()
            self._window = root
            self._window_handler = root.connect("notify::fullscreened",
                                                lambda *a: self._on_window_fullscreen())
        self.load()

    _unmap_timer = 0

    def _on_unmap(self):
        """Gone - or only for a moment: X11 window managers (xfwm) take the
        window away and back when it goes full screen."""
        if not self._unmap_timer:
            self._unmap_timer = GLib.timeout_add(UNMAP_GRACE, self._gone)

    def _gone(self):
        self._unmap_timer = 0
        if not self.get_mapped():
            self.set_fullscreen(False)
            self.stop()
        return False

    def _forget_window(self):
        if self._window is not None and self._window_handler:
            self._window.disconnect(self._window_handler)
        self._window, self._window_handler = None, 0

    # -- full screen and turning ---------------------------------------------------------
    def is_fullscreen(self):
        return self._full

    def set_fullscreen(self, on):
        win = self._window
        if win is None or on == self._full:
            return
        self._full = on
        if on:
            win.fullscreen()
            self.app.toast(_("Esc ends full screen"))
        else:
            win.unfullscreen()
        self._fullscreen_changed(on)

    def _on_window_fullscreen(self):
        """The window manager ended full screen (F11, a keyboard shortcut ...)."""
        if self._full and not self._window.is_fullscreen() and self._was_full:
            self._full = False
            self._fullscreen_changed(False)
        self._was_full = self._window.is_fullscreen()

    _was_full = False

    def _fullscreen_changed(self, on):
        """Full screen: only the picture - the window's bars and the page's
        own go."""
        self.bar.set_visible(not on)
        self.state.set_visible(not on)
        self.switch.set_visible(not on)
        if hasattr(self._window, "set_chrome"):
            self._window.set_chrome(not on)
        if on:
            self.picture.grab_focus()

    def _on_escape(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape and self.is_fullscreen():
            self.set_fullscreen(False)
            return True
        return False

    def rotate(self):
        """Portrait to landscape and back. The picture comes anew."""
        if self.mirror is not None:
            self.mirror.rotate("toggle")

    # -- the phone ---------------------------------------------------------------------
    def set_device(self, dev):
        if dev is not self.dev:
            self.stop()
            self.dev = dev
            self.check = None
            self.picture.set_paintable(None)
            self._online = None
        self.desktop.set_device(dev)
        self.device_changed()

    def device_changed(self):
        self._filling = True
        try:
            q = settings(self.app).get("quality", "normal")
            self.quality.set_selected(next((n for n, (k, _l) in enumerate(QUALITY_NAMES)
                                            if k == q), 1))
            desktop = self.mode() == "desktop"
            self.desktop_mode.set_active(desktop)
            self.mirror_mode.set_active(not desktop)
            self.modes.set_visible_child_name(self.mode())
        finally:
            self._filling = False
        online = self.dev is not None and self.dev.online
        if online != self._online:
            self._online = online
            self.desktop.refresh()          # what the phone has: asked when it comes
            self.refresh_check()

        if self.dev is None or not self.dev.online:
            self.stop()
            self._show_status(_("Not connected"), self.dev and text.device_state(self.dev))
        elif self.get_mapped():
            self.load()

    def mode(self):
        return settings(self.app).get("mode", "mirror")

    def _on_mode(self, button):
        if self._filling or not button.get_active():
            return
        mode = "desktop" if button is self.desktop_mode else "mirror"
        settings(self.app)["mode"] = mode
        config.save(self.app.cfg)
        self.modes.set_visible_child_name(mode)
        if mode == "desktop":
            self.stop()                 # the picture only runs while it is shown
            self.desktop.refresh()
        else:
            self.load()

    def load(self, force=False):
        """Starts the picture - only while the page is shown, and showing it."""
        dev = self.dev
        if self.mode() != "mirror":
            return
        if not self.get_mapped() or dev is None or not dev.online:
            if dev is None or not dev.online:
                self._show_status(_("Not connected"), dev and text.device_state(dev))
            return
        if not self.ready():
            self.stop()
            self.view.set_visible_child_name("needs")
            return
        if self.mirror is not None and not force:
            return
        self.stop()
        from .screen import Mirror
        self.mirror = Mirror(dev, settings(self.app).get("quality", "normal"), picture=self._show,
                             **self.app._screen_extra)
        self.mirror.connect("state", self._on_state)
        self.mirror.connect("stopped", self._on_stopped)
        self.state.set_label(self.hint)
        self._show_status(_("Connecting …"), None, spinner=True)
        if self.mirror.start():
            self.picture.grab_focus()

    # -- what it needs ---------------------------------------------------------------
    check = None                        # the phone's answer to screen.check

    def _needs_group(self, **kwargs):
        return NeedsGroup((("recorder", _("wf-recorder on the phone")),
                           ("decoder", _("GStreamer with H.264 on this PC")),
                           ("touch", _("Taps and keys: python3-evdev on the phone")),
                           ("turn", _("Turning: wlr-randr on the phone"))),
                          self.explain, self.refresh_check, **kwargs)

    def refresh_check(self):
        """Asks the phone what is there (once it is online), and looks here."""
        dev = self.dev
        self.update_needs()
        if dev is None or not dev.online:
            return

        def got(result, error):
            if dev is not self.dev:
                return
            # an agent too old to know the question: tried as before
            self.check = result if error is None else None
            self.update_needs()
            if not self.ready() or self.view.get_visible_child_name() == "needs":
                self.load()

        dev.request("screen.check", {}, got)

    def met(self):
        """{need: True/False/None (not known yet)}."""
        c = self.check
        known = c is not None
        return {"recorder": c.get("wf_recorder") if known else None,
                "decoder": not pc_missing(),
                "touch": bool(c.get("evdev") and c.get("uinput")) if known else None,
                "turn": c.get("wlr_randr") if known else None}

    def ready(self):
        """The picture can come: not known to miss the recorder or the decoder."""
        m = self.met()
        return m["recorder"] is not False and m["decoder"]

    def update_needs(self):
        m = self.met()
        subtitles = {"touch": ("", _("Optional - without it only the picture comes")),
                     "turn": ("", _("Optional - without it the phone is not turned from here"))}
        for group in (self.needs, self.missing_list):
            group.update(m, subtitles)
        self.missing_button.set_visible(self.ready() and False in m.values())
        self.rotate_button.set_sensitive(m["turn"] is not False)

    def instructions(self, key):
        """(heading, body, command or None) for what is missing."""
        dev = self.dev
        c = self.check or {}
        if key == "recorder":
            return on_the_phone(dev, _("Install wf-recorder on the phone"), _(
                "It records the phone's screen for the PC."), "sudo apt install wf-recorder")
        if key == "turn":
            return on_the_phone(dev, _("Install wlr-randr on the phone"), _(
                "It turns the phone's screen between portrait and landscape."),
                "sudo apt install wlr-randr")
        if key == "touch" and not c.get("evdev"):
            return on_the_phone(dev, _("Install python3-evdev on the phone"), _(
                "Taps, swipes and keys from the PC reach the phone through it - without it "
                "only the picture comes."), "sudo apt install python3-evdev")
        if key == "touch":
            user = dev.info["user"] if dev is not None else "furios"
            group = c.get("uinput_group")
            if group in (None, "root"):
                # no group of its own: one that may write it, from the next start on
                command = ("echo 'KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\"' | sudo tee "
                           "/etc/udev/rules.d/60-phonebridge-uinput.rules && "
                           "sudo usermod -aG input %s" % user)
            else:
                command = "sudo usermod -aG %s %s" % (group, user)
            return on_the_phone(dev, _("Allow taps from the PC on the phone"), _(
                "Taps and keys from the PC come in through /dev/uinput, which the phone's user "
                "may not write. This allows it - it takes effect once the phone has started "
                "anew."), command)
        hint = gstreamer_hint()
        return (_("Install GStreamer"), _(
            "The picture comes as H.264 and is shown with GStreamer and its plugins. On this "
            "PC:") if hint else _(
            "The picture comes as H.264 and is shown with GStreamer - install its base, good, "
            "bad and libav plugins with your distribution's package manager."), hint)

    def explain(self, key):
        self.missing_button.popdown()
        return explain(self.get_root(), self.app, *self.instructions(key), self.refresh_check)

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
            reason = state[len("error no touch: "):]
            if "evdev" in reason:           # "No module named 'evdev'" - but where?
                reason = _("python3-evdev is missing on the phone (not on this PC)")
            self.state.set_label(_("Only the picture - the phone takes no taps: %s") % reason)
            self.refresh_check()            # the bar's button says what to do
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
