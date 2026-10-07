# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Messages: the conversations on the phone, one of them open, and a line
to answer or write a new one.

The history is chatty's on the phone (read only); what PhoneBridge sends
goes out through ModemManager like chatty's own messages and is kept in a
log next to it, so it shows up here even where chatty does not list it."""

import re

from gi.repository import Adw, GLib, Gtk, Pango

from . import text
from .i18n import _


def is_number(title):
    return bool(re.fullmatch(r"[+\d\s/()-]+", title or ""))


def person_avatar(app, dev, thread, size):
    """Adw.Avatar: the picture from the phone, else coloured initials -
    or a plain silhouette for a bare number."""
    title = thread["title"] if thread else ""
    avatar = Adw.Avatar(size=size, text=title, show_initials=not is_number(title))
    if thread and thread.get("avatar") and dev is not None:
        app.avatars.get(dev, thread["avatar"], avatar.set_custom_image)
    return avatar


class ThreadRow(Gtk.ListBoxRow):
    def __init__(self, app, dev, thread):
        super().__init__()
        self.thread = thread
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=8)
        box.append(person_avatar(app, dev, thread, 40))
        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                        valign=Gtk.Align.CENTER, hexpand=True)
        title = Gtk.Label(label=thread["title"], xalign=0,
                          ellipsize=Pango.EllipsizeMode.END)
        if thread["unread"]:
            title.add_css_class("thread-unread")
        lines.append(title)
        last = thread.get("last")
        if last:
            when = Gtk.Label(label=text.activity(last["time"]), xalign=0)
            when.add_css_class("dim-label")
            when.add_css_class("caption")
            lines.append(when)
        box.append(lines)
        if thread["unread"]:
            badge = Gtk.Label(label=str(thread["unread"]), valign=Gtk.Align.CENTER)
            badge.add_css_class("unread-badge")
            box.append(badge)
        self.set_child(box)

    def matches(self, query):
        hay = " ".join((self.thread["title"], self.thread["thread"])).lower()
        return all(w in hay for w in query.lower().split())


def bubble(msg):
    row = Gtk.ListBoxRow(activatable=False, selectable=False)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                  halign=Gtk.Align.END if msg["out"] else Gtk.Align.START,
                  margin_start=60 if msg["out"] else 10,
                  margin_end=10 if msg["out"] else 60,
                  margin_top=3, margin_bottom=3)
    box.add_css_class("bubble")
    box.add_css_class("bubble-out" if msg["out"] else "bubble-in")
    body = Gtk.Label(label=msg["body"], wrap=True, xalign=0, selectable=True,
                     wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=60)
    box.append(body)
    meta = text.when_long(msg["time"])
    if msg.get("status") == "failed":
        meta += " · " + _("not sent")
    when = Gtk.Label(label=meta, xalign=1 if msg["out"] else 0)
    when.add_css_class("dim-label")
    when.add_css_class("caption")
    box.append(when)
    row.set_child(box)
    return row


class MessagesPage(Gtk.Box):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.dev = None
        self.thread = None
        self.messages = []
        self._selecting = False
        self._load_serial = 0

        # conversations
        self.search = Gtk.SearchEntry(placeholder_text=_("Search conversations"),
                                      margin_start=8, margin_end=8, margin_top=6,
                                      margin_bottom=6)
        self.search.connect("search-changed", lambda *a: self.list.invalidate_filter())
        self.list = Gtk.ListBox()
        self.list.add_css_class("navigation-sidebar")
        self.list.set_filter_func(lambda row: row.matches(self.search.get_text()))
        self.list.connect("row-selected", self._on_row)
        self.list_empty = Adw.StatusPage(icon_name="mail-unread-symbolic",
                                         title=_("No messages"))
        self.list_empty.add_css_class("compact")
        self.list_stack = Gtk.Stack()
        self.list_stack.add_named(Gtk.ScrolledWindow(
            child=self.list, vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER), "list")
        self.list_stack.add_named(self.list_empty, "empty")
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.append(self.search)
        side.append(self.list_stack)
        side_header = Adw.HeaderBar(show_end_title_buttons=False,
                                    show_start_title_buttons=False)
        self.new_button = Gtk.Button(icon_name="list-add-symbolic",
                                     tooltip_text=_("New message"))
        self.new_button.connect("clicked", lambda *a: self.compose())
        side_header.pack_start(self.new_button)
        side_view = Adw.ToolbarView(content=side)
        side_view.add_top_bar(side_header)
        self.sidebar = Adw.NavigationPage(title=_("Conversations"), child=side_view)

        # one conversation
        self.title = Adw.WindowTitle()
        self.call_button = Gtk.Button(icon_name="call-start-symbolic",
                                      tooltip_text=_("Call from the phone"))
        self.call_button.connect("clicked", self._on_call)
        self.head_avatar = Gtk.Box(valign=Gtk.Align.CENTER)
        head = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        head.append(self.head_avatar)
        head.append(self.title)
        conv_header = Adw.HeaderBar(show_end_title_buttons=False,
                                    show_start_title_buttons=False,
                                    title_widget=head)
        conv_header.pack_end(self.call_button)
        self.bubbles = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.bubbles.add_css_class("background")
        self.scroller = Gtk.ScrolledWindow(child=self.bubbles, vexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.no_thread = Adw.StatusPage(icon_name="mail-send-receive-symbolic",
                                        title=_("Choose a conversation"),
                                        description=_("or write a new message"))
        self.conv_stack = Gtk.Stack()
        self.conv_stack.add_named(self.no_thread, "none")
        self.conv_stack.add_named(self.scroller, "thread")

        self.entry = Gtk.Entry(hexpand=True, placeholder_text=_("Write a message"))
        self.entry.connect("activate", lambda *a: self._send())
        self.entry.connect("changed", self._on_typing)
        self.counter = Gtk.Label()
        self.counter.add_css_class("dim-label")
        self.counter.add_css_class("caption")
        self.send_button = Gtk.Button(icon_name="paper-plane-symbolic",
                                      tooltip_text=_("Send"))
        self.send_button.add_css_class("suggested-action")
        self.send_button.connect("clicked", lambda *a: self._send())
        self.compose_bar = Gtk.Box(spacing=6, margin_start=8, margin_end=8,
                                   margin_top=6, margin_bottom=6)
        self.compose_bar.append(self.entry)
        self.compose_bar.append(self.counter)
        self.compose_bar.append(self.send_button)

        conv_view = Adw.ToolbarView(content=self.conv_stack)
        conv_view.add_top_bar(conv_header)
        conv_view.add_bottom_bar(self.compose_bar)
        self.content = Adw.NavigationPage(title=_("Conversation"), child=conv_view)

        self.split = Adw.NavigationSplitView(sidebar=self.sidebar, content=self.content,
                                             hexpand=True, min_sidebar_width=260,
                                             max_sidebar_width=360)
        self.append(self.split)
        self._show_thread(None)

    # -- the phone --------------------------------------------------------
    def set_device(self, dev):
        changed = dev is not self.dev
        self.dev = dev
        if changed:
            self._show_thread(None)
        self.threads_changed()
        self.device_changed()

    def device_changed(self):
        online = self.dev is not None and self.dev.online
        self.new_button.set_sensitive(online)
        self._update_compose()

    def threads(self):
        return self.app.threads.get(self.dev.id, []) if self.dev else []

    def threads_changed(self):
        self._selecting = True
        while (row := self.list.get_row_at_index(0)) is not None:
            self.list.remove(row)
        threads = self.threads()
        for t in threads:
            row = ThreadRow(self.app, self.dev, t)
            self.list.append(row)
            if t["thread"] == self.thread:
                self.list.select_row(row)
        self._selecting = False
        self.list_stack.set_visible_child_name("list" if threads else "empty")
        current = self._thread_info(self.thread)
        if current is not None:
            self.title.set_title(current["title"])
            self._set_head_avatar(current)

    def sms_arrived(self, new):
        if self.thread is not None and (
                not new or any(m["thread"] == self.thread for m in new)):
            self._load(self.thread)

    def _set_head_avatar(self, info):
        key = (info or {}).get("avatar"), (info or {}).get("title")
        if getattr(self, "_head_key", None) == key:
            return
        self._head_key = key
        while (child := self.head_avatar.get_first_child()) is not None:
            self.head_avatar.remove(child)
        if info is not None:
            self.head_avatar.append(person_avatar(self.app, self.dev, info, 28))

    def _thread_info(self, thread):
        return next((t for t in self.threads() if t["thread"] == thread), None)

    # -- choosing ---------------------------------------------------------
    def _on_row(self, listbox, row):
        if self._selecting or row is None:
            return
        self._show_thread(row.thread["thread"])
        self.split.set_show_content(True)

    def open_thread(self, thread):
        """Opens a conversation - an existing one when the number matches."""
        country = self.app.cfg["country"]
        wanted = text.normalize(thread, country)
        for t in self.threads():
            if t["thread"] == thread or text.normalize(t["thread"], country) == wanted:
                thread = t["thread"]
                break
        else:
            thread = wanted
        self._show_thread(thread)
        self.threads_changed()
        self.split.set_show_content(True)
        self.entry.grab_focus()

    def compose(self):
        dialog = Adw.AlertDialog(heading=_("New message"),
                                 body=_("To which number?"))
        entry = Gtk.Entry(placeholder_text="+49 …", activates_default=True,
                          input_purpose=Gtk.InputPurpose.PHONE)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("open", _("Write"))
        dialog.set_default_response("open")
        dialog.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_enabled("open", False)
        entry.connect("changed", lambda e: dialog.set_response_enabled(
            "open", bool(e.get_text().strip())))

        def answered(d, response):
            if response == "open" and entry.get_text().strip():
                self.open_thread(entry.get_text().strip())

        dialog.connect("response", answered)
        dialog.present(self.get_root())

    def _show_thread(self, thread):
        self.thread = thread
        self.messages = []
        self._fill([])
        if thread is None:
            self.conv_stack.set_visible_child_name("none")
            self._set_head_avatar(None)
            self.title.set_title("")
            self.title.set_subtitle("")
            self.content.set_title(_("Conversation"))
        else:
            info = self._thread_info(thread)
            title = info["title"] if info else thread
            self._set_head_avatar(info or {"title": title, "thread": thread})
            self.title.set_title(title)
            self.title.set_subtitle(thread if title != thread else "")
            self.content.set_title(title)
            self.conv_stack.set_visible_child_name("thread")
            self._load(thread)
        self._update_compose()

    def _load(self, thread):
        if self.dev is None:
            return
        self._load_serial += 1
        serial = self._load_serial

        def done(result, error):
            if serial != self._load_serial or thread != self.thread:
                return
            if error is not None:
                self.app.toast(text.error(error))
                return
            self.messages = result
            self._fill(result)
            self.window_focus()

        self.dev.request("sms.messages", {"thread": thread,
                                          "country": self.app.cfg["country"]}, done)

    def _fill(self, messages):
        while (row := self.bubbles.get_row_at_index(0)) is not None:
            self.bubbles.remove(row)
        for msg in messages:
            self.bubbles.append(bubble(msg))
        GLib.timeout_add(60, self._scroll_down)

    def _scroll_down(self):
        adj = self.scroller.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def window_focus(self):
        """Marks the open conversation as read while the user can see it."""
        root = self.get_root()
        if (self.dev is None or self.thread is None or root is None
                or root.showing_thread() != (self.dev.id, self.thread)):
            return
        ids = [m["id"] for m in self.messages
               if not m["out"] and isinstance(m["id"], int)]
        if ids:
            self.app.mark_seen(self.dev.id, self.thread, max(ids))

    # -- writing ----------------------------------------------------------
    def _writable(self):
        info = self._thread_info(self.thread)
        return (self.dev is not None and self.dev.online and self.thread is not None
                and not (info and info.get("group")))

    def _update_compose(self):
        writable = self._writable()
        self.compose_bar.set_visible(self.thread is not None)
        self.entry.set_sensitive(writable)
        self.call_button.set_visible(self.thread is not None)
        self.call_button.set_sensitive(writable and bool(
            (self.dev.hello or {}).get("has", {}).get("calls")))
        self._on_typing()

    def _on_typing(self, *args):
        body = self.entry.get_text()
        chars, parts = text.sms_parts(body)
        self.counter.set_label("%d · %d SMS" % (chars, parts) if body else "")
        self.send_button.set_sensitive(self._writable() and bool(body.strip()))

    def _send(self):
        body = self.entry.get_text()
        if not body.strip() or not self._writable():
            return
        thread = self.thread
        self.entry.set_sensitive(False)
        self.send_button.set_sensitive(False)

        def done(result, error):
            self.entry.set_sensitive(self._writable())
            if error is not None:
                self.app.toast(_("Not sent: %s") % text.error(error))
                self._on_typing()
                return
            if self.entry.get_text() == body:
                self.entry.set_text("")
            if thread == self.thread:
                self._load(thread)
            self.app.refresh_threads(self.dev)
            self.entry.grab_focus()

        self.dev.request("sms.send", {"to": thread, "body": body,
                                      "country": self.app.cfg["country"]}, done)

    def _on_call(self, *args):
        if self.thread is None or self.dev is None:
            return

        def done(result, error):
            self.app.toast(text.error(error) if error is not None
                           else _("Calling %s on the phone …") % result)

        self.dev.request("call", {"number": self.thread,
                                  "country": self.app.cfg["country"]}, done)
