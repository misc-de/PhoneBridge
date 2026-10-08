# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""A made-up notification daemon on the (private) session bus, in a thread
of its own: Notify answers with an id, CloseNotification closes and says so."""

import os
import threading

from gi.repository import Gio, GLib

XML = """<node><interface name="org.freedesktop.Notifications">
  <method name="Notify">
    <arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/>
    <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="as" direction="in"/>
    <arg type="a{sv}" direction="in"/><arg type="i" direction="in"/>
    <arg type="u" direction="out"/></method>
  <method name="CloseNotification"><arg type="u" direction="in"/></method>
  <signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal>
</interface></node>"""
IFACE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"


class FakeNotifyDaemon:
    def __init__(self):
        self.shown, self.closed, self.next_id = [], [], 41
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self.thread.start()
        ready.wait(5)

    def _connect(self):
        return Gio.DBusConnection.new_for_address_sync(
            os.environ["DBUS_SESSION_BUS_ADDRESS"],
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)

    def _run(self, ready):
        self.ctx = GLib.MainContext()
        self.ctx.push_thread_default()
        self.conn = self._connect()
        self.conn.register_object(PATH, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0],
                                  self._method, None, None)
        self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                            "org.freedesktop.DBus", "RequestName",
                            GLib.Variant("(su)", (IFACE, 0)), None,
                            Gio.DBusCallFlags.NONE, 3000, None)
        self.loop = GLib.MainLoop(self.ctx)
        ready.set()
        self.loop.run()

    def _method(self, conn, sender, path, iface, method, params, inv):
        if method == "Notify":
            self.next_id += 1
            self.shown.append((self.next_id, params.unpack()))
            inv.return_value(GLib.Variant("(u)", (self.next_id,)))
        else:
            nid = params.unpack()[0]
            self.closed.append(nid)
            inv.return_value(None)
            conn.emit_signal(None, PATH, IFACE, "NotificationClosed",
                             GLib.Variant("(uu)", (nid, 3)))

    def notify(self, app, summary, body, hints=None):
        """An app on the phone sends one - from a connection of its own."""
        conn = self._connect()
        try:
            v = conn.call_sync(IFACE, PATH, IFACE, "Notify", GLib.Variant(
                "(susssasa{sv}i)", (app, 0, "", summary, body, [], hints or {}, -1)),
                None, Gio.DBusCallFlags.NONE, 3000, None)
            return v.unpack()[0]
        finally:
            conn.close_sync(None)

    def close(self):
        def stop():
            self.conn.close_sync(None)
            self.loop.quit()
            return False
        src = GLib.idle_source_new()
        src.set_callback(stop)
        src.attach(self.ctx)
        self.thread.join(5)
