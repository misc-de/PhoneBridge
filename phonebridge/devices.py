# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Phones: add, change, remove. PhoneBridge logs in with your SSH key -
`ssh-copy-id user@phone` once, and it works without a password."""

from gi.repository import Adw, GLib, Gtk

from . import config, text
from .i18n import _


class DevicesDialog(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__(title=_("Phones"), content_width=560)
        self.app = app
        page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(
            description=_("PhoneBridge logs in over SSH with your key. Set it up "
                          "once with “ssh-copy-id user@address”."))
        page.add(self.group)
        add = Adw.PreferencesGroup()
        add_row = Adw.ButtonRow(title=_("Add phone"), start_icon_name="list-add-symbolic")
        add_row.connect("activated", lambda *a: self.edit(None))
        add.add(add_row)
        page.add(add)
        self.add(page)
        self.rows = []
        self.fill()
        self._handlers = [(d, d.connect("changed", lambda *a: self.fill()))
                          for d in app.devices.values()]
        self.connect("closed", self._on_closed)

    def _on_closed(self, *args):
        for dev, hid in self._handlers:
            dev.disconnect(hid)
        self._handlers = []

    def fill(self):
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        for info in self.app.cfg["devices"]:
            dev = self.app.devices.get(info["id"])
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(info.get("name") or info["host"]),
                subtitle=GLib.markup_escape_text("%s@%s:%s · %s" % (
                    info["user"], info["host"], info.get("port") or 22,
                    text.device_state(dev) if dev else "")))
            edit = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text=_("Change"))
            edit.add_css_class("flat")
            edit.connect("clicked", lambda b, i=info: self.edit(i))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text=_("Remove"))
            remove.add_css_class("flat")
            remove.connect("clicked", lambda b, i=info: self.remove(i))
            key = Gtk.Button(icon_name="dialog-password-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text=_("Remove the password from the keyring"),
                             visible=False)
            key.add_css_class("flat")
            if dev is not None:
                key.connect("clicked", lambda b, d=dev: self.forget(d))
            row.add_suffix(key)
            row.add_suffix(edit)
            row.add_suffix(remove)
            self.group.add(row)
            self.rows.append(row)
            from . import secrets
            secrets.lookup(info, lambda pw, b=key: pw is not None and b.set_visible(True))

    def edit(self, info):
        dialog = Adw.AlertDialog(heading=_("Change phone") if info else _("Add phone"))
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        name = Adw.EntryRow(title=_("Name"), text=(info or {}).get("name", ""))
        host = Adw.EntryRow(title=_("Address (IP or host name)"),
                            text=(info or {}).get("host", ""))
        user = Adw.EntryRow(title=_("User"), text=(info or {}).get("user", "furios"))
        port = Adw.EntryRow(title=_("SSH port"),
                            text=str((info or {}).get("port") or 22),
                            input_purpose=Gtk.InputPurpose.DIGITS)
        for row in (name, host, user, port):
            box.append(row)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def check(*args):
            fields = ((host, config.valid_host(host.get_text().strip())),
                      (user, config.valid_user(user.get_text().strip())),
                      (port, config.valid_port(port.get_text().strip())))
            for row, ok in fields:
                if ok or not row.get_text().strip():
                    row.remove_css_class("error")
                else:
                    row.add_css_class("error")
            dialog.set_response_enabled("save", all(ok for _r, ok in fields))

        for row in (host, user, port):
            row.connect("changed", check)
        check()

        def answered(d, response):
            if response != "save":
                return
            devices = [dict(x) for x in self.app.cfg["devices"]]
            entry = {"name": name.get_text().strip() or host.get_text().strip(),
                     "host": host.get_text().strip(), "user": user.get_text().strip(),
                     "port": int(port.get_text().strip())}
            if info is None:
                entry["id"] = config.new_device_id(entry["name"],
                                                   {x["id"] for x in devices})
                devices.append(entry)
            else:
                entry["id"] = info["id"]
                devices = [entry if x["id"] == info["id"] else x for x in devices]
            self.app.set_devices(devices)
            self._rewire()

        dialog.connect("response", answered)
        dialog.present(self)

    def forget(self, dev):
        dialog = Adw.AlertDialog(
            heading=_("Remove the password of %s?") % dev.name,
            body=_("It is removed from the keyring. Without a working SSH key, "
                   "PhoneBridge then asks for it again."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(d, response):
            if response == "remove":
                self.app.forget_password(dev)
                self.fill()

        dialog.connect("response", answered)
        dialog.present(self)

    def remove(self, info):
        dialog = Adw.AlertDialog(
            heading=_("Remove %s?") % (info.get("name") or info["host"]),
            body=_("Nothing on the phone changes."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(d, response):
            if response == "remove":
                self.app.set_devices([x for x in self.app.cfg["devices"]
                                      if x["id"] != info["id"]])
                self._rewire()

        dialog.connect("response", answered)
        dialog.present(self)

    def _rewire(self):
        self._on_closed()
        self._handlers = [(d, d.connect("changed", lambda *a: self.fill()))
                          for d in self.app.devices.values()]
        self.fill()
