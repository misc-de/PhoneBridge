# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Setting up a phone: address and user, a first connection right away, and
- when the phone does not take the PC's SSH key yet - the password once, to
put the key on the phone (as ssh-copy-id does) and/or keep the password in
the keyring. Shown at the first start, and for "Add phone".

The phone is added only once a connection worked."""

from gi.repository import Adw, Gtk

from . import config, secrets, sshkeys, text
from .connection import Device
from .i18n import _


class SetupDialog(Adw.Dialog):
    def __init__(self, app, first=False):
        super().__init__(title=_("Set up a phone"), content_width=520, content_height=600)
        self.app = app
        self.dev = None
        self._handlers = []
        self.password_used = None

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.add_named(self._form(first), "form")
        self.stack.add_named(self._busy(), "busy")
        self.stack.add_named(self._login(), "login")
        self.stack.add_named(self._done(), "done")
        header = Adw.HeaderBar()
        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        self.set_child(view)
        self.connect("closed", lambda *a: self._drop())
        self.set_focus(self.host)           # the address is what matters; the name may stay empty

    # -- pages ------------------------------------------------------------------
    def _page(self, *groups, intro=None, icon=None):
        page = Adw.PreferencesPage()
        if intro:
            head = Adw.PreferencesGroup()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_bottom=6)
            if icon:
                box.append(Gtk.Image(icon_name=icon, pixel_size=72))
            label = Gtk.Label(label=intro, wrap=True, justify=Gtk.Justification.CENTER)
            box.append(label)
            head.add(box)
            page.add(head)
        for g in groups:
            page.add(g)
        return page

    def _form(self, first):
        group = Adw.PreferencesGroup()
        self.name = Adw.EntryRow(title=_("Name (optional)"))
        self.host = Adw.EntryRow(title=_("Address (IP or host name)"))
        self.user = Adw.EntryRow(title=_("User"), text="furios")
        self.port = Adw.EntryRow(title=_("SSH port"), text="22",
                                 input_purpose=Gtk.InputPurpose.DIGITS)
        for row in (self.name, self.host, self.user, self.port):
            row.connect("changed", self._check)
            group.add(row)
        self.host.connect("entry-activated", lambda *a: self.connect_now())
        keys = Adw.PreferencesGroup()
        self.make_key = Adw.SwitchRow(
            title=_("Create an SSH key on this PC"),
            subtitle=_("There is none yet - the phone is reached with it from then on."),
            active=True)
        keys.add(self.make_key)
        keys.set_visible(sshkeys.public_key_path() is None)
        self.error = Gtk.Label(wrap=True, visible=False, margin_top=6)
        self.error.add_css_class("error")
        buttons = Adw.PreferencesGroup()
        self.go = Gtk.Button(label=_("Connect"), halign=Gtk.Align.CENTER)
        self.go.add_css_class("pill")
        self.go.add_css_class("suggested-action")
        self.go.connect("clicked", lambda *a: self.connect_now())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self.error)
        box.append(self.go)
        buttons.add(box)
        intro = (_("Welcome to PhoneBridge. Your phone is reached over SSH - "
                   "it needs to be on the same network, with SSH switched on.")
                 if first else _("PhoneBridge reaches the phone over SSH."))
        self._check()
        return self._page(group, keys, buttons, intro=intro,
                          icon="io.github.miscde.PhoneBridge")

    def _busy(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      valign=Gtk.Align.CENTER)
        box.append(Adw.Spinner(width_request=48, height_request=48))
        self.busy_label = Gtk.Label(label=_("Connecting …"))
        box.append(self.busy_label)
        return box

    def _login(self):
        group = Adw.PreferencesGroup()
        self.password = Adw.PasswordEntryRow(title=_("Password"), activates_default=True)
        self.password.connect("changed", lambda *a: self.login_go.set_sensitive(
            bool(self.password.get_text())))
        self.password.connect("entry-activated", lambda *a: self.login_now())
        group.add(self.password)
        options = Adw.PreferencesGroup()
        self.put_key = Adw.SwitchRow(
            title=_("Put the SSH key on the phone"),
            subtitle=_("Then no password is needed any more (like ssh-copy-id)."),
            active=True)
        self.keep_password = Adw.SwitchRow(title=_("Keep the password in the keyring"),
                                           active=False)
        self.put_key.connect("notify::active", lambda *a: self.keep_password.set_active(
            not self.put_key.get_active()))
        options.add(self.put_key)
        options.add(self.keep_password)
        self.login_error = Gtk.Label(wrap=True, visible=False)
        self.login_error.add_css_class("error")
        self.login_go = Gtk.Button(label=_("Log in"), halign=Gtk.Align.CENTER,
                                   sensitive=False)
        self.login_go.add_css_class("pill")
        self.login_go.add_css_class("suggested-action")
        self.login_go.connect("clicked", lambda *a: self.login_now())
        buttons = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self.login_error)
        box.append(self.login_go)
        buttons.add(box)
        return self._page(group, options, buttons, icon="dialog-password-symbolic",
                          intro=_("The phone does not take this PC's SSH key yet. Log in "
                                  "once with the password of the phone's user."))

    def _done(self):
        self.done_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        group = Adw.PreferencesGroup()
        close = Gtk.Button(label=_("Done"), halign=Gtk.Align.CENTER)
        close.add_css_class("pill")
        close.add_css_class("suggested-action")
        close.connect("clicked", lambda *a: self.close())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self.done_label)
        box.append(close)
        group.add(box)
        return self._page(group, icon="emblem-ok-symbolic", intro=_("Connected"))

    # -- doing --------------------------------------------------------------------
    def _info(self):
        host = self.host.get_text().strip()
        name = self.name.get_text().strip()
        taken = {d["id"] for d in self.app.cfg["devices"]}
        return {"id": config.new_device_id(name or host, taken), "name": name or host,
                "host": host, "user": self.user.get_text().strip(),
                "port": int(self.port.get_text().strip() or 22)}

    def _check(self, *args):
        ok = (config.valid_host(self.host.get_text().strip())
              and config.valid_user(self.user.get_text().strip())
              and config.valid_port(self.port.get_text().strip() or "22"))
        self.go.set_sensitive(ok)

    def connect_now(self):
        if not self.go.get_sensitive():
            return
        self.error.set_visible(False)
        if self.make_key.get_parent() and self.make_key.get_active() \
                and sshkeys.public_key_path() is None:
            try:
                sshkeys.make_key()
            except OSError as e:
                self._fail(_("No SSH key could be made: %s") % e)
                return
        self._drop()
        self.dev = Device(self._info())
        self._handlers = [self.dev.connect("changed", self._on_changed),
                          self.dev.connect("auth-needed", self._on_auth)]
        self.busy_label.set_label(_("Connecting to %s …") % self.dev.info["host"])
        self.stack.set_visible_child_name("busy")
        self.dev.start()

    def _on_changed(self, dev):
        if dev is not self.dev:
            return
        if dev.online:
            self._connected()
        elif dev.state == "offline" and dev.error and not dev.needs_password:
            if self.stack.get_visible_child_name() == "login":
                return
            reason = text.error(dev.error)
            self._drop()
            self._fail(_("No connection: %s") % reason)

    def _on_auth(self, dev, wrong):
        self.stack.set_visible_child_name("login")
        self.login_error.set_label(_("The password was not accepted."))
        self.login_error.set_visible(wrong)
        self.login_go.set_sensitive(bool(self.password.get_text()))
        self.password.grab_focus()

    def login_now(self):
        secret = self.password.get_text()
        if not secret or self.dev is None:
            return
        self.password_used = secret
        self.busy_label.set_label(_("Logging in …"))
        self.stack.set_visible_child_name("busy")
        self.dev.set_password(secret)

    def _connected(self):
        dev = self.dev
        info = dict(dev.info)
        if not self.name.get_text().strip() and (dev.hello or {}).get("hostname"):
            info["name"] = dev.hello["hostname"]
        notes = []

        def finish(*args):
            if self.password_used and self.keep_password.get_active():
                secrets.store(info, self.password_used)
            self._drop()
            self.app.set_devices(self.app.cfg["devices"] + [info])
            self.app.set_active(info["id"])
            self.done_label.set_label(
                _("%s is set up.") % info["name"] + ("\n\n" + "\n".join(notes) if notes else ""))
            self.stack.set_visible_child_name("done")

        if self.password_used and self.put_key.get_active():
            key = sshkeys.public_key()
            if key is None:
                notes.append(_("There is no SSH key on this PC to put on the phone."))
                finish()
                return

            def authorized(result, error):
                if error is not None:
                    notes.append(_("The SSH key could not be put on the phone: %s")
                                 % text.error(error))
                else:
                    notes.append(_("The phone takes this PC's SSH key from now on."))
                finish()

            dev.request("ssh.authorize", {"key": key}, authorized)
        else:
            finish()

    def _fail(self, message):
        self.error.set_label(message)
        self.error.set_visible(True)
        self.stack.set_visible_child_name("form")

    def _drop(self):
        """The trial connection goes - the app makes its own."""
        if self.dev is not None:
            for h in self._handlers:
                self.dev.disconnect(h)
            self.dev.stop()
            self.dev = None
            self._handlers = []
