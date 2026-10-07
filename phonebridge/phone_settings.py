# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The phone's settings: the quick switches, calls at the PC, and the
settings of GNOME, Phosh, feedbackd, FuriOS and the apps (settings_spec),
section by section - and every other GSettings key through a search.

A setting the phone does not have is left out; a section is read when it
is opened, and a change goes to the phone at once."""

from gi.repository import Adw, GLib, Gtk, Pango

from . import text
from .i18n import N_, _
from .quick import QuickSettings
from .settings_spec import SECTIONS, keys_of

NOTIFY_APP_KEYS = (("show-banners", N_("Banners")), ("enable-sound-alerts", N_("Sound")),
                   ("show-in-lock-screen", N_("On the lock screen")),
                   ("details-in-lock-screen", N_("Details on the lock screen")))


def key_id(schema, key, path=None):
    return " ".join([schema, key] + ([path] if path else []))


class SettingRow:
    """One row of settings_spec: its widget, and how a value shows in it
    and gets back from it."""

    def __init__(self, page, kind, schema, key, label, extra, opts):
        self.page = page
        self.kind, self.schema, self.key = kind, schema, key
        self.extra, self.opts = extra, opts
        self.path = opts.get("path")
        self.id = key_id(schema, key, self.path)
        self.values = []
        if kind in ("switch", "flag"):
            self.widget = Adw.SwitchRow(title=_(label))
            self.widget.connect("notify::active", self._changed)
        elif kind in ("choice", "enum"):
            self.widget = Adw.ComboRow(title=_(label))
            self.widget.connect("notify::selected", self._changed)
            if kind == "choice":
                self._set_choices([(v, _(l)) for v, l in extra])
        elif kind == "spin":
            low, high, step, digits = extra
            self.widget = Adw.SpinRow.new_with_range(low, high, step)
            self.widget.set_title(_(label))
            self.widget.set_digits(digits)
            if opts.get("unit"):
                self.widget.set_subtitle(_(opts["unit"]))
            self.widget.connect("notify::value", self._changed_later)
        elif kind == "scale":
            self.widget = Adw.ActionRow(title=_(label))
            self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,
                                                  extra[0], extra[1], (extra[1] - extra[0]) / 20)
            self.scale.set_hexpand(True)
            self.scale.set_size_request(180, -1)
            self.scale.set_valign(Gtk.Align.CENTER)
            self.scale.connect("value-changed", self._changed_later)
            self.widget.add_suffix(self.scale)
        else:
            self.widget = Adw.EntryRow(title=_(label), show_apply_button=True)
            self.widget.connect("apply", self._changed)
        if opts.get("subtitle") and kind != "spin":
            self.widget.set_subtitle(_(opts["subtitle"]))
        self.widget.set_visible(False)
        self._source = 0

    def _set_choices(self, pairs):
        self.values = [v for v, _l in pairs]
        self.widget.set_model(Gtk.StringList.new([l for _v, l in pairs]))

    def show(self, info, required=True):
        """info: what gsettings.get said about the key (None: not there)."""
        self.widget.set_visible(info is not None and required)
        if info is None:
            return
        v = info["value"]
        if self.kind == "switch":
            on = (v == self.extra[0]) if self.extra else bool(v)
            self.widget.set_active(not on if self.opts.get("invert") else on)
        elif self.kind == "flag":
            self.widget.set_active(self.extra in (v or []))
        elif self.kind == "enum":
            options = info["range"][1] if info["range"][0] == "enum" else [v]
            self._set_choices([(o, _(self.extra[o]) if o in self.extra else o)
                               for o in options])
            if v in self.values:
                self.widget.set_selected(self.values.index(v))
        elif self.kind == "choice":
            if v not in self.values:
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    pairs = [(x, _(l)) for x, l in self.extra] + [(v, str(v))]
                    self._set_choices(sorted(pairs, key=lambda p: (p[0] == 0, p[0])))
                else:
                    self._set_choices([(x, _(l)) for x, l in self.extra] + [(v, str(v))])
            self.widget.set_selected(self.values.index(v))
        elif self.kind == "spin":
            self.widget.set_value(v)
        elif self.kind == "scale":
            self.scale.set_value(v)
        else:
            self.widget.set_text(str(v))

    def value(self, info):
        if self.kind == "switch":
            on = self.widget.get_active()
            if self.opts.get("invert"):
                on = not on
            return (self.extra[0] if on else self.extra[1]) if self.extra else on
        if self.kind == "flag":
            current = list((info or {}).get("value") or [])
            if self.widget.get_active() and self.extra not in current:
                current.append(self.extra)
            elif not self.widget.get_active():
                current = [x for x in current if x != self.extra]
            return current
        if self.kind in ("choice", "enum"):
            return self.values[self.widget.get_selected()]
        if self.kind == "spin":
            return self.widget.get_value()
        if self.kind == "scale":
            return round(self.scale.get_value(), 3)
        return self.widget.get_text()

    def _changed(self, *args):
        if not self.page.updating:
            self.page.set_value(self)

    def _changed_later(self, *args):
        if self.page.updating:
            return
        if self._source:
            GLib.source_remove(self._source)
        self._source = GLib.timeout_add(400, self._fire)

    def _fire(self):
        self._source = 0
        self.page.set_value(self)
        return False


class PhoneSettingsPage(Gtk.Box):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.dev = None
        self.values = {}
        self.rows = {}           # section id -> [SettingRow]
        self.pages = {}
        self.updating = False
        self._loaded = {}        # section id -> device it was read from
        self.notify_apps = []

        self.quick = QuickSettings(app)
        self.section_list = Gtk.ListBox()
        self.section_list.add_css_class("navigation-sidebar")
        self.section_list.connect("row-selected", self._on_section)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        for section in SECTIONS:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=6)
            box.append(Gtk.Image(icon_name=section["icon"]))
            box.append(Gtk.Label(label=_(section["title"]), xalign=0))
            row.set_child(box)
            row.section = section
            self.section_list.append(row)
            self.stack.add_named(self._build(section), section["id"])
        side_view = Adw.ToolbarView(content=Gtk.ScrolledWindow(
            child=self.section_list, hscrollbar_policy=Gtk.PolicyType.NEVER))
        side_view.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False,
                                            show_start_title_buttons=False))
        sidebar = Adw.NavigationPage(title=_("Settings"), child=side_view)
        content_view = Adw.ToolbarView(content=self.stack)
        content_view.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False,
                                               show_start_title_buttons=False))
        self.content = Adw.NavigationPage(title=_(SECTIONS[0]["title"]), child=content_view)
        self.split = Adw.NavigationSplitView(sidebar=sidebar, content=self.content,
                                             hexpand=True, min_sidebar_width=240,
                                             max_sidebar_width=300)
        self.append(self.split)
        self.section_list.select_row(self.section_list.get_row_at_index(0))

    # -- building -------------------------------------------------------------------
    def _build(self, section):
        page = Adw.PreferencesPage()
        special = section.get("special")
        if special == "quick":
            page.add(self.quick.groups[0])
        elif special == "pc":
            page.add(self.quick.groups[1])
        elif special == "browse":
            group = Adw.PreferencesGroup(
                description=_("Every GSettings key of the phone, searchable and "
                              "changeable - for what the other sections leave out."))
            self.browse_row = Adw.ButtonRow(title=_("Search all GNOME settings …"),
                                            end_icon_name="go-next-symbolic")
            self.browse_row.connect("activated", lambda *a: self.browse())
            group.add(self.browse_row)
            page.add(group)
        rows = []
        for title, desc, specs in section.get("groups", ()):
            # titles are markup to libadwaita: "Verlauf & Aufräumen" must be escaped
            group = Adw.PreferencesGroup(
                title=GLib.markup_escape_text(_(title)) if title else "",
                description=GLib.markup_escape_text(_(desc)) if desc else "")
            group.rows = []
            for spec in specs:
                row = SettingRow(self, *spec)
                group.add(row.widget)
                group.rows.append(row)
                rows.append(row)
            page.add(group)
        if special == "sip":
            self.sip_group = Adw.PreferencesGroup(
                title=GLib.markup_escape_text(_("SIP accounts")),
                description=GLib.markup_escape_text(_(
                    "Internet telephony with GNOME Calls. Saving restarts Calls on the "
                    "phone for a moment - not possible during a call.")))
            self.sip_add = Adw.ButtonRow(title=_("Add SIP account …"),
                                         start_icon_name="list-add-symbolic")
            self.sip_add.connect("activated", lambda *a: self.edit_sip(None))
            self.sip_group.add(self.sip_add)
            self.sip_group.rows_ = []
            page.add(self.sip_group)
        if special == "notify_apps":
            self.apps_group = Adw.PreferencesGroup(
                title=_("Per app"),
                description=_("The apps that have shown notifications on the phone."))
            page.add(self.apps_group)
        self.rows[section["id"]] = rows
        self.pages[section["id"]] = page
        return page

    # -- the phone ------------------------------------------------------------------
    def set_device(self, dev):
        self.dev = dev
        self._loaded = {}
        self.quick.set_device(dev)
        self.device_changed()

    def device_changed(self):
        self.quick.update()
        online = self.dev is not None and self.dev.online
        if hasattr(self, "browse_row"):
            self.browse_row.set_sensitive(online)
        if not online:
            self._loaded = {}
        for rows in self.rows.values():
            for r in rows:
                r.widget.set_sensitive(online)
        if online and self.get_mapped():
            self.load()

    def current_section(self):
        row = self.section_list.get_selected_row()
        return row.section if row is not None else SECTIONS[0]

    def _on_section(self, listbox, row):
        if row is None:
            return
        self.stack.set_visible_child_name(row.section["id"])
        self.content.set_title(_(row.section["title"]))
        self.split.set_show_content(True)
        self.load()

    def load(self, section=None):
        section = section or self.current_section()
        dev = self.dev
        if dev is None or not dev.online or self._loaded.get(section["id"]) is dev:
            return
        self._loaded[section["id"]] = dev
        keys = keys_of(section)
        if keys:
            def done(result, error):
                if dev is not self.dev:
                    return
                if error is not None:
                    self._loaded.pop(section["id"], None)
                    self.app.toast(text.error(error))
                    return
                self.values.update(result)
                self._show(section)

            dev.request("gsettings.get", {"keys": keys}, done)
        if section.get("special") == "sip":
            self.load_sip()
        if section.get("special") == "notify_apps":
            dev.request("notifications.apps", {},
                        lambda r, e: self._got_apps(r, e, dev))

    def _required(self, row):
        req = row.opts.get("requires")
        if not req:
            return True
        info = self.values.get(key_id(*req))
        return bool(info and info["value"])

    def _show(self, section):
        self.updating = True
        try:
            for row in self.rows[section["id"]]:
                row.show(self.values.get(row.id), self._required(row))
            # groups without a row the phone has are left out
            page = self.pages[section["id"]]
            for group in _groups(page):
                if getattr(group, "rows", None):
                    group.set_visible(any(r.widget.get_visible() for r in group.rows))
        finally:
            self.updating = False

    def set_value(self, row):
        dev = self.dev
        if dev is None or not dev.online:
            return
        args = {"schema": row.schema, "key": row.key,
                "value": row.value(self.values.get(row.id))}
        if row.path:
            args["path"] = row.path

        def done(result, error):
            if dev is not self.dev:
                return
            if error is not None:
                self.app.toast(text.error(error))
            elif result is not None:
                self.values[row.id] = result
            self.updating = True
            try:
                row.show(self.values.get(row.id), self._required(row))
            finally:
                self.updating = False

        dev.request("gsettings.set", args, done)

    # -- notifications per app -------------------------------------------------------
    def _got_apps(self, result, error, dev=None):
        if error is not None or (dev is not None and dev is not self.dev):
            return
        self.notify_apps = result
        for row in list(getattr(self.apps_group, "app_rows", [])):
            self.apps_group.remove(row)
        self.apps_group.app_rows = []
        for app in result:
            if not app["installed"]:
                continue
            exp = Adw.ExpanderRow(title=GLib.markup_escape_text(app["name"]),
                                  show_enable_switch=True,
                                  enable_expansion=app["values"]["enable"])
            exp.connect("notify::enable-expansion",
                        lambda e, _p, a=app: self._set_app(a, "enable", e.get_enable_expansion()))
            for key, label in NOTIFY_APP_KEYS:
                sw = Adw.SwitchRow(title=_(label), active=app["values"][key])
                sw.connect("notify::active",
                           lambda w, _p, a=app, k=key: self._set_app(a, k, w.get_active()))
                exp.add_row(sw)
            self.apps_group.add(exp)
            self.apps_group.app_rows.append(exp)
        self.apps_group.set_visible(bool(self.apps_group.app_rows))

    def _set_app(self, app, key, value):
        if app["values"].get(key) == value or self.dev is None:
            return
        app["values"][key] = value
        self.dev.request("gsettings.set", {
            "schema": "org.gnome.desktop.notifications.application", "key": key,
            "path": app["path"], "value": value},
            lambda r, e: e is not None and self.app.toast(text.error(e)))

    # -- SIP accounts --------------------------------------------------------------
    def load_sip(self):
        dev = self.dev
        if dev is None or not dev.online:
            return

        def done(result, error):
            if dev is not self.dev or error is not None:
                return
            for row in self.sip_group.rows_:
                self.sip_group.remove(row)
            self.sip_group.rows_ = []
            for acc in result:
                where = "%s@%s" % (acc["user"], acc["host"])
                row = Adw.ActionRow(
                    title=GLib.markup_escape_text(acc["display_name"] or where),
                    subtitle=GLib.markup_escape_text("%s · %s" % (where, acc["protocol"])))
                for icon_name, tip, cb in (
                        ("document-edit-symbolic", _("Change"), self.edit_sip),
                        ("user-trash-symbolic", _("Remove"), self.delete_sip)):
                    b = Gtk.Button(icon_name=icon_name, valign=Gtk.Align.CENTER, tooltip_text=tip)
                    b.add_css_class("flat")
                    b.connect("clicked", lambda btn, f=cb, a=acc: f(a))
                    row.add_suffix(b)
                self.sip_group.remove(self.sip_add)
                self.sip_group.add(row)
                self.sip_group.add(self.sip_add)
                self.sip_group.rows_.append(row)

        dev.request("sip.list", {}, done)

    def edit_sip(self, account):
        if self.dev is not None and self.dev.online:
            SipEditor(self, account).present(self.get_root())

    def sip_changed(self):
        self.load_sip()
        if self.dev is not None:
            self.app.refresh_lines(self.dev)

    def delete_sip(self, account):
        dialog = Adw.AlertDialog(
            heading=_("Remove the SIP account %s?") % (
                account["display_name"] or "%s@%s" % (account["user"], account["host"])),
            body=_("It is removed from GNOME Calls on the phone, with its password. "
                   "Calls restarts for a moment."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dev = self.dev

        def answered(d, response):
            if response == "remove":
                dev.request("sip.delete", {"id": account["id"]}, lambda r, e: (
                    self.app.toast(_("Not removed: %s") % text.error(e)) if e is not None
                    else self.sip_changed()))

        dialog.connect("response", answered)
        dialog.present(self.get_root())

    def browse(self):
        if self.dev is not None and self.dev.online:
            SettingsBrowser(self.app, self.dev).present(self.get_root())


def _groups(page):
    out, stack = [], [page]
    while stack:
        w = stack.pop()
        if isinstance(w, Adw.PreferencesGroup):
            out.append(w)
            continue
        c = w.get_first_child()
        while c is not None:
            stack.append(c)
            c = c.get_next_sibling()
    return out


PROTOCOLS = ("UDP", "TCP", "TLS")
ENCRYPTION = (N_("None"), N_("When possible"), N_("Always"))


class SipEditor(Adw.Dialog):
    """A SIP account of GNOME Calls - the fields Calls' own dialog has."""

    def __init__(self, page, account):
        super().__init__(title=_("Change SIP account") if account else _("New SIP account"),
                         content_width=500, content_height=620)
        self.page = page
        self.account = account
        a = account or {"display_name": "", "host": "", "user": "", "protocol": "UDP",
                        "port": 0, "auto_connect": True, "can_tel": False,
                        "media_encryption": 0}
        prefs = Adw.PreferencesPage()
        main = Adw.PreferencesGroup()
        self.name = Adw.EntryRow(title=_("Display name"), text=a["display_name"])
        self.host = Adw.EntryRow(title=_("Server"), text=a["host"])
        self.user = Adw.EntryRow(title=_("User"), text=a["user"])
        self.password = Adw.PasswordEntryRow(
            title=_("Password") if not account else _("Password (empty: unchanged)"))
        for row in (self.name, self.host, self.user, self.password):
            row.connect("changed", self._check)
            main.add(row)
        prefs.add(main)
        more = Adw.PreferencesGroup(title=_("Connection"))
        self.protocol = Adw.ComboRow(title=_("Transport"))
        self.protocol.set_model(Gtk.StringList.new(list(PROTOCOLS)))
        self.protocol.set_selected(PROTOCOLS.index(a["protocol"])
                                   if a["protocol"] in PROTOCOLS else 0)
        self.port = Adw.SpinRow.new_with_range(0, 65535, 1)
        self.port.set_title(_("Port"))
        self.port.set_subtitle(_("0: the standard port"))
        self.port.set_value(a["port"] or 0)
        self.auto = Adw.SwitchRow(title=_("Connect by itself"), active=a["auto_connect"])
        self.tel = Adw.SwitchRow(title=_("Use for phone numbers"), active=a["can_tel"])
        self.encryption = Adw.ComboRow(title=_("Encrypt calls"))
        self.encryption.set_model(Gtk.StringList.new([_(e) for e in ENCRYPTION]))
        self.encryption.set_selected(min(2, max(0, a["media_encryption"] or 0)))
        for row in (self.protocol, self.port, self.auto, self.tel, self.encryption):
            more.add(row)
        prefs.add(more)

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *a: self.close())
        self.save = Gtk.Button(label=_("Save"))
        self.save.add_css_class("suggested-action")
        self.save.connect("clicked", self._save)
        header.pack_start(cancel)
        header.pack_end(self.save)
        view = Adw.ToolbarView(content=prefs)
        view.add_top_bar(header)
        self.set_child(view)
        self._check()

    def _check(self, *args):
        ok = bool(self.host.get_text().strip() and self.user.get_text().strip()
                  and (self.account or self.password.get_text()))
        self.save.set_sensitive(ok)

    def _save(self, *args):
        account = {"id": (self.account or {}).get("id"),
                   "display_name": self.name.get_text().strip(),
                   "host": self.host.get_text().strip(), "user": self.user.get_text().strip(),
                   "protocol": PROTOCOLS[self.protocol.get_selected()],
                   "port": int(self.port.get_value()), "auto_connect": self.auto.get_active(),
                   "can_tel": self.tel.get_active(),
                   "media_encryption": self.encryption.get_selected()}
        args = {"account": account}
        if self.password.get_text():
            args["password"] = self.password.get_text()
        self.save.set_sensitive(False)

        def done(result, error):
            if error is not None:
                self.save.set_sensitive(True)
                alert = Adw.AlertDialog(heading=_("Not saved"), body=text.error(error))
                alert.add_response("ok", _("OK"))
                alert.present(self)
                return
            self.close()
            self.page.sip_changed()

        self.page.dev.request("sip.save", args, done)


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
