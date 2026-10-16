# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""What a part of the app needs on the phone and on this PC: a row each -
there, missing (with "How to …") or not known yet - and a dialog with the
command that sets it up. The desktop session (rdp_ui.py) and the screen
sharing (screen_page.py) show it."""

from gi.repository import Adw, Gdk, Gtk

from .i18n import _


def on_the_phone(dev):
    """The line before a command for the phone."""
    if dev is None:
        return ""
    return (_("On the phone - in its terminal, or from here with “ssh %s@%s”:")
            % (dev.info["user"], dev.info["host"]))


class NeedsGroup(Adw.PreferencesGroup):
    def __init__(self, needs, explain, again, **kwargs):
        """needs: ((key, title), ...); explain(key) when "How to …" is
        clicked, again() for "Check again"."""
        super().__init__(**kwargs)
        if not kwargs.get("title"):
            self.set_title(_("What it needs"))
        button = Gtk.Button(label=_("Check again"), valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.connect("clicked", lambda *a: again())
        self.set_header_suffix(button)
        self.rows = {}
        for key, title in needs:
            row = Adw.ActionRow(title=title)
            icon = Gtk.Image()
            how = Gtk.Button(label=_("How to …"), valign=Gtk.Align.CENTER)
            how.connect("clicked", lambda _b, k: explain(k), key)
            row.add_suffix(how)
            row.add_suffix(icon)
            self.rows[key] = (row, icon, how)
            self.add(row)

    def update(self, met, subtitles=None):
        """met: {key: True/False/None (not known yet)}; subtitles: {key:
        (when there, when missing)}."""
        for key, (row, icon, how) in self.rows.items():
            ok = met.get(key)
            icon.set_from_icon_name("emblem-ok-symbolic" if ok else
                                    "dialog-warning-symbolic" if ok is False else
                                    "content-loading-symbolic")
            icon.set_tooltip_text(_("There") if ok else _("Missing") if ok is False else None)
            how.set_visible(ok is False)
            there, missing = (subtitles or {}).get(key, ("", ""))
            row.set_subtitle(there if ok else missing if ok is False else "")


def explain(parent, app, heading, body, command, again):
    """The dialog with what to do - and the command to copy, if there is one."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    if command:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label = Gtk.Label(label=command, selectable=True, wrap=True,
                          wrap_mode=2, xalign=0)      # Pango.WrapMode.WORD_CHAR
        label.add_css_class("monospace")
        label.add_css_class("card")
        copy = Gtk.Button(label=_("Copy"), halign=Gtk.Align.END)
        copy.connect("clicked", lambda *a: (
            Gdk.Display.get_default().get_clipboard().set(command),
            app.toast(_("Copied"))))
        box.append(label)
        box.append(copy)
        dialog.set_extra_child(box)
    dialog.add_response("check", _("Check again"))
    dialog.add_response("ok", _("OK"))
    dialog.set_default_response("ok")
    dialog.connect("response", lambda d, r: r == "check" and again())
    dialog.present(parent)
    return dialog
