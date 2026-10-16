# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""On the screen page: a GNOME desktop of the phone's own, in an RDP window
here - its size, start and end, and what it needs on the phone and on
this PC, with the commands that set it up."""

from gi.repository import Adw, Gdk, Gtk

from . import config, text
from .i18n import _
from .needs import NeedsGroup, explain, on_the_phone

DEFAULT_SIZE = (1280, 800)
SLOW = 1920 * 1200          # more pixels than this: told it gets slow


def settings(app):
    return app.cfg.setdefault("screen", {"quality": "normal"})


class DesktopPanel(Adw.PreferencesPage):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.dev = None
        self.check = None               # the phone's answer to desktop.check
        self._filling = False

        group = Adw.PreferencesGroup(title=_("Desktop session"), description=_(
            "A GNOME desktop of the phone's own, shown only here, in a window of its own. "
            "Phosh goes on as it is on the phone. It is drawn without the graphics chip - "
            "fine for files, settings and simple apps; videos and animations stutter."))
        self.sizes = sizes()
        self.size = Adw.ComboRow(title=_("Resolution"), subtitle=_(
            "Of the desktop on the phone - the window scales it to its own size"),
            model=Gtk.StringList.new([size_name(s) for s in self.sizes]))
        self.size.connect("notify::selected", self._on_size)
        self.state_row = Adw.ActionRow(title=_("Not running"))
        self.button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.button.connect("clicked", lambda *a: self._on_button())
        self.state_row.add_suffix(self.button)
        group.add(self.size)
        group.add(self.state_row)
        self.add(group)

        self.needs = NeedsGroup((("gnome_shell", _("GNOME Shell on the phone")),
                                 ("remote_desktop", _("GNOME Remote Desktop on the phone")),
                                 ("tools", _("OpenSSL and D-Bus on the phone")),
                                 ("firewall", _("Firewall rule without a password")),
                                 ("client", _("RDP client on this PC"))),
                                self.explain, self.refresh)
        self.add(self.needs)
        app.rdp_listeners.append(lambda dev: dev is self.dev and self.update())

    # -- the phone -----------------------------------------------------------------------
    def set_device(self, dev):
        if dev is not self.dev:
            self.dev = dev
            self.check = None
        self.refresh()

    def refresh(self):
        """Asks the phone what is there (once it is online)."""
        dev = self.dev
        self.update()
        if dev is None or not dev.online:
            return

        def got(result, error):
            if dev is self.dev:
                self.check = result if error is None else None
                self.update()

        dev.request("desktop.check", {}, got)

    def met(self):
        """{need: True/False/None (not known yet)}."""
        from .rdp import client
        c = self.check
        known = c is not None
        return {"gnome_shell": c.get("gnome_shell") if known else None,
                "remote_desktop": c.get("remote_desktop") if known else None,
                "tools": (c.get("openssl") and c.get("dbus_daemon")) if known else None,
                "firewall": c.get("firewall") if known else None,
                "client": client() is not None}

    def ready(self):
        m = self.met()
        return all(m[k] for k in ("gnome_shell", "remote_desktop", "tools", "client"))

    def update(self):
        dev = self.dev
        self.needs.update(self.met(), {"firewall": (
            _("Only the phone itself reaches the desktop's port"),
            _("Optional - without it the port can be reached in the network, guarded by a "
              "password for this session only"))})
        self._filling = True
        try:
            size = tuple(settings(self.app).get("desktop_size", DEFAULT_SIZE))
            if size not in self.sizes:
                size = DEFAULT_SIZE
            self.size.set_selected(self.sizes.index(size))
        finally:
            self._filling = False
        session = self.app.rdp_sessions.get(dev.id) if dev is not None else None
        online = dev is not None and dev.online
        if session is not None:
            self.state_row.set_title(session.state_text)
            self.state_row.set_subtitle(_("Port reachable in the network (no firewall rule)")
                                        if session.open else "")
            self.button.set_label(_("End"))
            self.button.remove_css_class("suggested-action")
            self.button.add_css_class("destructive-action")
            self.button.set_sensitive(True)
        else:
            self.state_row.set_title(_("Not running") if online else _("Not connected"))
            self.state_row.set_subtitle(self.app.rdp_last_error.get(dev.id, "")
                                        if dev is not None else "")
            self.button.set_label(_("Start"))
            self.button.remove_css_class("destructive-action")
            self.button.add_css_class("suggested-action")
            self.button.set_sensitive(online and self.ready())
        self.size.set_sensitive(session is None)

    def _on_size(self, *args):
        if self._filling:
            return
        settings(self.app)["desktop_size"] = list(self.sizes[self.size.get_selected()])
        config.save(self.app.cfg)

    def _on_button(self):
        dev = self.dev
        if dev is None:
            return
        if dev.id in self.app.rdp_sessions:
            self.app.set_rdp(dev, False)
            return
        if self.met()["firewall"]:
            self.app.set_rdp(dev, True)
            return
        dialog = Adw.AlertDialog(heading=_("Without a firewall rule"), body=_(
            "The phone cannot close the desktop's port to the network without a password. "
            "The desktop can still start: then devices in the same network reach its port - "
            "guarded by TLS and a password that is made for this session only."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("how", _("How to set it up …"))
        dialog.add_response("go", _("Start anyway"))
        dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda d, r: self.app.set_rdp(dev, True, allow_open=True)
                       if r == "go" else self.explain("firewall") if r == "how" else None)
        dialog.present(self.get_root())
        return dialog

    # -- how to set it up ------------------------------------------------------------------
    def instructions(self, key):
        """(heading, body, command or None) for what is missing."""
        dev = self.dev
        if key == "gnome_shell":
            return on_the_phone(dev, _("Install GNOME Shell on the phone"), _(
                "The desktop is GNOME's own shell, started without a screen."),
                "sudo apt install gnome-shell")
        if key == "remote_desktop":
            return on_the_phone(dev, _("Install GNOME Remote Desktop on the phone"), _(
                "It shows the desktop over RDP. Installed, it does nothing by itself - "
                "PhoneBridge starts it only for a desktop session."),
                "sudo apt install gnome-remote-desktop")
        if key == "tools":
            return on_the_phone(dev, _("Install OpenSSL and D-Bus on the phone"), _(
                "For the session's certificate and its own D-Bus."),
                "sudo apt install openssl dbus-daemon")
        if key == "firewall":
            user = dev.info["user"] if dev is not None else "furios"
            iptables = (self.check or {}).get("iptables") or "/usr/sbin/iptables"
            ip6tables = iptables.replace("iptables", "ip6tables")
            return on_the_phone(dev, _("Set up the firewall rule on the phone"), _(
                "While the desktop runs, PhoneBridge closes its port to everything but the "
                "phone itself (the PC comes in through SSH). For that the phone's user may "
                "run iptables without a password - this allows exactly that, nothing "
                "else."),
                "echo '%s ALL=(root) NOPASSWD: %s, %s' | sudo tee "
                "/etc/sudoers.d/phonebridge-desktop" % (user, iptables, ip6tables))
        from .rdp import install_hint
        hint = install_hint()
        return (_("Install an RDP client"), _("The desktop opens in FreeRDP 3 (xfreerdp3). On this "
                                   "PC:") if hint else
                _("The desktop opens in FreeRDP 3 (xfreerdp3 or sdl-freerdp3) - install it "
                  "with your distribution's package manager."), hint)

    def explain(self, key):
        return explain(self.get_root(), self.app, *self.instructions(key), self.refresh)


def screen_size():
    """This PC's (largest) screen, in pixels - or None."""
    display = Gdk.Display.get_default()
    best = None
    monitors = display.get_monitors() if display is not None else None
    for n in range(monitors.get_n_items() if monitors is not None else 0):
        m = monitors.get_item(n)
        g, scale = m.get_geometry(), m.get_scale_factor()
        size = (g.width * scale, g.height * scale)
        if best is None or size[0] * size[1] > best[0] * best[1]:
            best = size
    return best


def sizes():
    """720p upwards - and this PC's screen, when it is none of them."""
    from .rdp import SIZES
    found = list(SIZES)
    own = screen_size()
    if own and own[1] >= 720 and own not in found:
        found.append(own)
        found.sort(key=lambda s: (s[0] * s[1], s[0]))
    return found


def size_name(size):
    from .rdp import NAMES
    name = "%d × %d" % size
    tags = [NAMES[size]] if size in NAMES else []
    if size == screen_size():
        tags.append(_("this screen"))
    if size[0] * size[1] > SLOW:
        tags.append(_("slow"))
    return "%s (%s)" % (name, ", ".join(tags)) if tags else name


def state_text(state):
    return {"starting": _("Starting GNOME on the phone … (10-30 s)"),
            "open": _("Starting GNOME on the phone … (10-30 s)"),
            "ready": _("Opening the window …"),
            "client": _("Running - closing its window ends it")}.get(state, state)


def stopped_text(reason):
    return text.error(reason) if reason else ""
