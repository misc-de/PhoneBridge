# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""A made-up GNOME Calls on the (private) session bus, as Calls exports its
calls: org.gnome.Calls with an object manager at /org/gnome/Calls and one
org.gnome.Calls.Call per call. It runs in a thread of its own, so the tests
can call it synchronously. Numbers are invented."""

import os
import threading

from gi.repository import Gio, GLib

CALL_XML = """<node>
  <interface name="org.gnome.Calls.Call">
    <method name="Accept"/><method name="Hangup"/><method name="Silence"/>
    <method name="SendDtmf"><arg name="tone" type="s" direction="in"/></method>
    <property name="Inbound" type="b" access="read"/>
    <property name="State" type="u" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="DisplayName" type="s" access="read"/>
    <property name="Protocol" type="s" access="read"/>
  </interface>
</node>"""
MANAGER_XML = """<node>
  <interface name="org.freedesktop.DBus.ObjectManager">
    <method name="GetManagedObjects">
      <arg name="objects" type="a{oa{sa{sv}}}" direction="out"/>
    </method>
  </interface>
</node>"""
CALL = "org.gnome.Calls.Call"


class FakeCalls:
    def __init__(self):
        self.calls = {}         # path -> properties
        self.methods = []       # (path, method)
        self._ids = {}
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self.thread.start()
        ready.wait(5)

    def _run(self, ready):
        self.ctx = GLib.MainContext()
        self.ctx.push_thread_default()
        self.conn = Gio.DBusConnection.new_for_address_sync(
            os.environ["DBUS_SESSION_BUS_ADDRESS"],
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        self.call_iface = Gio.DBusNodeInfo.new_for_xml(CALL_XML).interfaces[0]
        self.conn.register_object("/org/gnome/Calls",
                                  Gio.DBusNodeInfo.new_for_xml(MANAGER_XML).interfaces[0],
                                  self._manager, None, None)
        self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                            "org.freedesktop.DBus", "RequestName",
                            GLib.Variant("(su)", ("org.gnome.Calls", 0)), None,
                            Gio.DBusCallFlags.NONE, 3000, None)
        self.loop = GLib.MainLoop(self.ctx)
        ready.set()
        self.loop.run()

    def _in_thread(self, fn):
        done = threading.Event()

        def run():
            fn()
            done.set()
            return False
        src = GLib.idle_source_new()
        src.set_callback(run)
        src.attach(self.ctx)
        done.wait(5)

    @staticmethod
    def _variants(props):
        types = {"Inbound": "b", "State": "u", "Id": "s", "DisplayName": "s", "Protocol": "s"}
        return {k: GLib.Variant(types[k], v) for k, v in props.items()}

    def add(self, address, state=5, protocol="sip", display_name=None, inbound=True):
        """A call; state as Calls has it (5 incoming, 1 active, 3 dialing)."""
        path = "/org/gnome/Calls/Call/%d" % (len(self._ids) + 1)
        props = {"Inbound": inbound, "State": state, "Id": address, "Protocol": protocol,
                 "DisplayName": address.split(":")[-1].split("@")[0]
                 if display_name is None else display_name}

        def export():
            self.calls[path] = props
            self._ids[path] = self.conn.register_object(path, self.call_iface, self._method,
                                                        self._get, None)
            self.conn.emit_signal(None, "/org/gnome/Calls", "org.freedesktop.DBus.ObjectManager",
                                  "InterfacesAdded", GLib.Variant("(oa{sa{sv}})", (
                                      path, {CALL: self._variants(props)})))
        self._in_thread(export)
        return path

    def _manager(self, conn, sender, path, iface, method, params, inv):
        inv.return_value(GLib.Variant("(a{oa{sa{sv}}})", ({
            p: {CALL: self._variants(props)} for p, props in self.calls.items()},)))

    def _get(self, conn, sender, path, iface, prop):
        return self._variants(self.calls[path]).get(prop)

    def _method(self, conn, sender, path, iface, method, params, inv):
        self.methods.append((path, method))
        if method == "Accept":
            self.calls[path]["State"] = 1
        elif method == "Hangup":
            self.calls.pop(path, None)
            conn.unregister_object(self._ids.pop(path))
        inv.return_value(None)

    def close(self):
        def stop():
            self.conn.close_sync(None)
            self.loop.quit()
        self._in_thread(stop)
        self.thread.join(5)
