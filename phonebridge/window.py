# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The window: overview, messages and the phone's settings for the phone
chosen at the top left. Closing it only hides it - the panel icon stays."""

from gi.repository import Adw, Gio, GLib, Gtk

from . import i18n, text
from .i18n import _
from .calendar_page import CalendarPage
from .contacts import ContactsPage
from .messages import MessagesPage
from .overview import OverviewPage
from .phone import CallBar, PhonePage
from .phone_settings import PhoneSettingsPage


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="PhoneBridge",
                         default_width=1180, default_height=760)
        self.set_hide_on_close(True)
        self.set_size_request(360, 400)
        self.app = app

        self.overview = OverviewPage(app)
        self.phone = PhonePage(app)
        self.messages = MessagesPage(app)
        self.contacts = ContactsPage(app)
        self.calendar = CalendarPage(app)
        self.settings = PhoneSettingsPage(app)
        self.pages = (self.overview, self.phone, self.messages, self.contacts,
                      self.calendar, self.settings)
        self.stack = Adw.ViewStack()
        for page, name, title, icon in (
                (self.overview, "overview", _("Overview"), "phone-symbolic"),
                (self.phone, "phone", _("Telephone"), "call-start-symbolic"),
                (self.messages, "messages", _("Messages"), "mail-unread-symbolic"),
                (self.contacts, "contacts", _("Contacts"), "x-office-address-book-symbolic"),
                (self.calendar, "calendar", _("Appointments"), "x-office-calendar-symbolic"),
                (self.settings, "settings", _("Settings"), "emblem-system-symbolic")):
            self.stack.add_titled_with_icon(page, name, title, icon)
        self.stack.connect("notify::visible-child", self._on_page)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.ViewSwitcher(
            stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        self.picker = Gtk.DropDown(tooltip_text=_("Phone"))
        self.picker.connect("notify::selected", self._on_pick)
        header.pack_start(self.picker)
        self.update_button = Gtk.Button(visible=False)
        self.update_button.add_css_class("suggested-action")
        self.update_button.connect("clicked", lambda *a: app.ask_update())
        header.pack_start(self.update_button)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic",
                                       menu_model=self._menu(), primary=True,
                                       tooltip_text=_("Menu")))

        self.banner = Adw.Banner(button_label=_("Connect now"), use_markup=False)
        self.banner.connect("button-clicked", self._on_reconnect)

        self.empty = Adw.StatusPage(
            icon_name="phone-symbolic", title=_("No phone set up"),
            description=_("PhoneBridge reaches your phone over SSH with your key."))
        add = Gtk.Button(label=_("Add phone"), halign=Gtk.Align.CENTER)
        add.add_css_class("pill")
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda *a: app.show_setup())
        self.empty.set_child(add)

        self.body = Gtk.Stack()
        self.body.add_named(self.stack, "pages")
        self.body.add_named(self.empty, "empty")
        self.toasts = Adw.ToastOverlay(child=self.body)

        self.callbar = CallBar(app)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        view.add_top_bar(self.callbar)
        view.set_content(self.toasts)
        bar = Adw.ViewSwitcherBar(stack=self.stack)
        view.add_bottom_bar(bar)
        self.set_content(view)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        bp.add_setter(bar, "reveal", True)
        bp.add_setter(header, "title-widget", Adw.WindowTitle(title="PhoneBridge"))
        # narrow: list and content one after the other instead of side by side
        for page in (self.phone, self.messages, self.contacts, self.calendar, self.settings):
            bp.add_setter(page.split, "collapsed", True)
        self.add_breakpoint(bp)

        self.connect("notify::is-active", lambda *a: self.messages.window_focus())
        self._picking = False
        self.devices_changed()
        self.update_changed()

    def _menu(self):
        menu = Gio.Menu()
        main = Gio.Menu()
        main.append(_("Phones …"), "app.devices")
        menu.append_section(None, main)
        prefs = Gio.Menu()
        prefs.append(_("Notify about new messages"), "app.notify")
        prefs.append(_("Start at login"), "app.autostart")
        prefs.append(_("Look for updates"), "app.updates")
        lang = Gio.Menu()
        for code, label in i18n.LANGUAGES:
            item = Gio.MenuItem.new(_(label) if code == "system" else label, None)
            item.set_action_and_target_value("app.language", GLib.Variant("s", code))
            lang.append_item(item)
        prefs.append_submenu(_("Language"), lang)
        menu.append_section(None, prefs)
        end = Gio.Menu()
        end.append(_("About PhoneBridge"), "app.about")
        end.append(_("Quit"), "app.quit")
        menu.append_section(None, end)
        return menu

    def update_changed(self):
        """The update hint at the top left: there while an update waits."""
        b = self.update_button
        b.set_visible(self.app.update_available is not None)
        b.set_sensitive(not self.app.updating)
        b.set_label(_("Updating …") if self.app.updating else _("Update available"))
        b.set_tooltip_text(None if self.app.updating else _("Install the new version"))

    # -- phones -----------------------------------------------------------
    def devices_changed(self):
        self._picking = True
        names = [d.name for d in self.app.devices.values()]
        self.picker.set_model(Gtk.StringList.new(names))
        self.picker.set_visible(bool(names))
        self._picking = False
        self.body.set_visible_child_name("pages" if names else "empty")
        self.active_changed()

    def active_changed(self):
        ids = list(self.app.devices)
        dev = self.app.active_device()
        if dev is not None and self.picker.get_selected() != ids.index(dev.id):
            self._picking = True
            self.picker.set_selected(ids.index(dev.id))
            self._picking = False
        for page in self.pages:
            page.set_device(dev)
        self._update_banner(dev)
        self.calls_changed(dev)
        self.update_badges()

    def _on_pick(self, *args):
        if self._picking:
            return
        ids = list(self.app.devices)
        n = self.picker.get_selected()
        if 0 <= n < len(ids):
            self.app.set_active(ids[n])

    def device_changed(self, dev):
        if dev is self.app.active_device():
            # the overview loads what it shows as well - a phone coming online
            # after the window was built (the first connection) must fill it
            self.overview.device_changed()
            for page in self.pages[1:]:
                page.device_changed()
            self._update_banner(dev)

    def threads_changed(self, dev):
        if dev is self.app.active_device():
            self.messages.threads_changed()
            self.overview.show_threads()
        self.update_badges()

    def sms_arrived(self, dev, new):
        if dev is self.app.active_device():
            self.messages.sms_arrived(new)

    def lines_changed(self, dev):
        if dev is self.app.active_device():
            self.phone.lines_changed()

    def voicebox_changed(self, dev, reload_calls=True):
        if dev is self.app.active_device():
            self.phone.voicebox_changed(reload_calls)
            self.overview.show_calls()
        self.update_badges()

    def update_badges(self):
        dev = self.app.active_device()
        for page, count in ((self.messages, self.app.unread(dev.id) if dev else 0),
                            (self.phone, self.app.new_voicemails(dev.id) if dev else 0)):
            p = self.stack.get_page(page)
            p.set_badge_number(count)
            p.set_needs_attention(count > 0)

    def calls_changed(self, dev):
        if dev is not None and dev is self.app.active_device():
            self.callbar.show_call(dev, self.app.current_call(dev.id))
        elif dev is None:
            self.callbar.show_call(None, None)

    def _update_banner(self, dev):
        if dev is None or dev.online:
            self.banner.set_revealed(False)
            return
        if dev.needs_password:
            self.banner.set_title(_("%s: login needed - the SSH key is not accepted")
                                  % dev.name)
            self.banner.set_button_label(_("Log in"))
        else:
            self.banner.set_title("%s: %s" % (dev.name, text.device_state(dev)))
            self.banner.set_button_label(_("Connect now"))
        self.banner.set_revealed(True)

    def _on_reconnect(self, *args):
        dev = self.app.active_device()
        if dev is None:
            return
        if dev.needs_password:
            self.app.ask_password(dev)
        else:
            dev.reconnect()

    # -- pages ------------------------------------------------------------
    def show_page(self, name):
        self.stack.set_visible_child_name(name)

    def current_page(self):
        return self.stack.get_visible_child_name()

    def _on_page(self, *args):
        page = self.stack.get_visible_child()
        if hasattr(page, "load"):
            page.load()
        self.messages.window_focus()

    def showing_thread(self):
        """(device id, thread) the user is looking at right now, or None."""
        dev = self.app.active_device()
        if (dev is None or not self.is_visible() or not self.is_active()
                or self.stack.get_visible_child() is not self.messages
                or self.messages.thread is None):
            return None
        return dev.id, self.messages.thread

    def toast(self, message):
        # plain text: names, SMS senders and error messages are not markup
        self.toasts.add_toast(Adw.Toast(title=message, use_markup=False))

