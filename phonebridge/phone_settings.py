# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The phone's GNOME settings (GSettings): the common ones as switches and
choices, and every other one through a search.

A setting the phone does not have is left out."""

from gi.repository import Adw, GLib, Gtk, Pango

from . import text
from .i18n import N_, _

IDLE_CHOICES = ((30, "30 s"), (60, "1 min"), (120, "2 min"), (300, "5 min"),
                (600, "10 min"), (900, "15 min"), (0, N_("Never")))
TEXT_SIZES = ((0.85, N_("Small")), (1.0, N_("Normal")), (1.15, N_("Large")),
              (1.3, N_("Larger")), (1.5, N_("Largest")))

# (kind, schema, key, title, extra)
#   switch: extra = (value when on, value when off), or None for a boolean
#   choice: extra = ((value, label), ...)
COMMON = (
    (N_("Appearance"), (
        ("switch", "org.gnome.desktop.interface", "color-scheme", N_("Dark style"),
         ("prefer-dark", "default")),
        ("choice", "org.gnome.desktop.interface", "text-scaling-factor",
         N_("Text size"), TEXT_SIZES),
        ("switch", "org.gnome.desktop.interface", "show-battery-percentage",
         N_("Battery percentage in the top bar"), None),
    )),
    (N_("Screen"), (
        ("switch", "org.gnome.settings-daemon.plugins.power", "ambient-enabled",
         N_("Automatic brightness"), None),
        ("switch", "org.gnome.settings-daemon.plugins.color", "night-light-enabled",
         N_("Night light"), None),
        ("choice", "org.gnome.desktop.session", "idle-delay",
         N_("Screen off after"), IDLE_CHOICES),
    )),
    (N_("Notifications and sounds"), (
        ("switch", "org.gnome.desktop.notifications", "show-banners",
         N_("Notification banners"), None),
        ("switch", "org.gnome.desktop.sound", "event-sounds",
         N_("Event sounds"), None),
    )),
    (N_("Input"), (
        ("switch", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled",
         N_("On-screen keyboard"), None),
    )),
)


def key_id(schema, key):
    return "%s %s" % (schema, key)


class PhoneSettingsPage(Adw.PreferencesPage):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.dev = None
        self.rows = {}
        self.values = {}
        self._updating = False
        self._loaded_for = None

        for title, entries in COMMON:
            group = Adw.PreferencesGroup(title=_(title))
            self.add(group)
            for kind, schema, key, label, extra in entries:
                if kind == "switch":
                    row = Adw.SwitchRow(title=_(label))
                    row.connect("notify::active", self._on_switch, schema, key, extra)
                else:
                    row = Adw.ComboRow(title=_(label))
                    row.set_model(Gtk.StringList.new([_(l) for _v, l in extra]))
                    row.connect("notify::selected", self._on_choice, schema, key, extra)
                row.set_visible(False)
                group.add(row)
                self.rows[key_id(schema, key)] = (kind, row, extra)

        more = Adw.PreferencesGroup(title=_("All settings"))
        self.add(more)
        browse = Adw.ButtonRow(title=_("Search all GNOME settings …"),
                               end_icon_name="go-next-symbolic")
        browse.connect("activated", lambda *a: self.browse())
        more.add(browse)
        self.browse_row = browse

    def set_device(self, dev):
        self.dev = dev
        self._loaded_for = None
        self.device_changed()

    def device_changed(self):
        online = self.dev is not None and self.dev.online
        self.browse_row.set_sensitive(online)
        if not online:
            self._loaded_for = None
            for _kind, row, _extra in self.rows.values():
                row.set_sensitive(False)
        elif self.get_mapped():
            self.load()

    def load(self):
        dev = self.dev
        if dev is None or not dev.online or self._loaded_for is dev:
            return
        self._loaded_for = dev
        keys = [k.split(" ", 1) for k in self.rows]

        def done(result, error):
            if dev is not self.dev:
                return
            if error is not None:
                self._loaded_for = None
                self.app.toast(text.error(error))
                return
            self.values = result
            self._show_values()

        dev.request("gsettings.get", {"keys": keys}, done)

    def _show_values(self):
        self._updating = True
        try:
            for kid, (kind, row, extra) in self.rows.items():
                info = self.values.get(kid)
                row.set_visible(info is not None)
                row.set_sensitive(info is not None)
                if info is None:
                    continue
                value = info["value"]
                if kind == "switch":
                    row.set_active(value == extra[0] if extra else bool(value))
                else:
                    values = [v for v, _l in extra]
                    if value in values:
                        best = values.index(value)
                    else:  # set elsewhere: the nearest choice, never "Never"
                        best = min((i for i, v in enumerate(values) if v),
                                   key=lambda i: abs(values[i] - value))
                    row.set_selected(best)
        finally:
            self._updating = False

    def _set(self, schema, key, value):
        dev = self.dev

        def done(result, error):
            if error is not None:
                self.app.toast(text.error(error))
            elif result is not None:
                self.values[key_id(schema, key)] = result
            self._show_values()

        dev.request("gsettings.set", {"schema": schema, "key": key, "value": value},
                    done)

    def _on_switch(self, row, _pspec, schema, key, extra):
        if self._updating:
            return
        on = row.get_active()
        self._set(schema, key, (extra[0] if on else extra[1]) if extra else on)

    def _on_choice(self, row, _pspec, schema, key, extra):
        if not self._updating:
            self._set(schema, key, extra[row.get_selected()][0])

    def browse(self):
        if self.dev is not None and self.dev.online:
            SettingsBrowser(self.app, self.dev).present(self.get_root())


class SettingsBrowser(Adw.Dialog):
    """Every GSettings key of the phone, searchable and editable as text
    (GVariant notation: 'text', 42, true, 1.5, ['a', 'b'])."""

    def __init__(self, app, dev):
        super().__init__(title=_("GNOME settings of %s") % dev.name,
                         content_width=720, content_height=600)
        self.app = app
        self.dev = dev
        self._search_source = 0
        self._serial = 0
        self.search = Gtk.SearchEntry(placeholder_text=_("Search, e.g. “dark” or “interface”"),
                                      hexpand=True)
        self.search.connect("search-changed", self._on_search)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", self._on_row)
        self.hint = Gtk.Label(wrap=True, margin_top=12)
        self.hint.add_css_class("dim-label")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        box.append(self.list)
        box.append(self.hint)
        header = Adw.HeaderBar()
        header.set_title_widget(self.search)
        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(
            child=Adw.Clamp(child=box, maximum_size=800), vexpand=True))
        view.add_top_bar(header)
        self.set_child(view)
        self.set_focus(self.search)
        self.hint.set_label(_("Type to search – at least three letters."))

    def _on_search(self, *args):
        if self._search_source:
            GLib.source_remove(self._search_source)
        self._search_source = GLib.timeout_add(300, self._run_search)

    def _run_search(self):
        self._search_source = 0
        query = self.search.get_text().strip()
        if len(query) < 3:
            self._fill([])
            self.hint.set_label(_("Type to search – at least three letters."))
            return False
        self._serial += 1
        serial = self._serial

        def done(result, error):
            if serial != self._serial:
                return
            if error is not None:
                self.hint.set_label(text.error(error))
                return
            self._fill(result)
            self.hint.set_label(_("Nothing found.") if not result else
                                _("Only the first 200 hits.") if len(result) >= 200 else "")

        self.dev.request("gsettings.search", {"query": query, "limit": 200}, done)
        return False

    def _fill(self, results):
        while (row := self.list.get_row_at_index(0)) is not None:
            self.list.remove(row)
        for info in results:
            row = Adw.ActionRow(title=info["key"], activatable=True,
                                subtitle=GLib.markup_escape_text(
                                    "%s\n%s" % (info["schema"], info["summary"]))
                                if info["summary"] else info["schema"],
                                subtitle_lines=2)
            row.info = info
            value = Gtk.Label(label=info["text"], ellipsize=Pango.EllipsizeMode.END,
                              max_width_chars=24, valign=Gtk.Align.CENTER)
            if info["default"]:
                value.add_css_class("dim-label")
            row.add_suffix(value)
            self.list.append(row)
        self.list.set_visible(bool(results))

    def _on_row(self, listbox, row):
        info = row.info
        dialog = Adw.AlertDialog(heading=info["key"])
        lines = [info["schema"]]
        if info["description"] or info["summary"]:
            lines.append(info["description"] or info["summary"])
        rng = info.get("range") or []
        if rng and rng[0] == "enum":
            lines.append(_("Allowed: %s") % ", ".join("'%s'" % v for v in rng[1]))
        elif rng and rng[0] == "range":
            lines.append(_("From %s to %s") % tuple(rng[1]))
        lines.append(_("Type: %s") % info["type"])
        dialog.set_body("\n\n".join(lines))
        entry = Gtk.Entry(text=info["text"], activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reset", _("Default"))
        dialog.add_response("save", _("Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_response_enabled("reset", not info["default"])

        def answered(d, response):
            if response == "save":
                args = {"schema": info["schema"], "key": info["key"],
                        "text": entry.get_text()}
                self.dev.request("gsettings.set", args, self._changed)
            elif response == "reset":
                self.dev.request("gsettings.reset", {"schema": info["schema"],
                                                     "key": info["key"]}, self._changed)

        dialog.connect("response", answered)
        dialog.present(self)

    def _changed(self, result, error):
        if error is not None:
            toast = Adw.AlertDialog(heading=_("Not saved"), body=text.error(error))
            toast.add_response("ok", _("OK"))
            toast.present(self)
            return
        self._run_search()
