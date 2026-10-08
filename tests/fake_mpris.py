# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""A made-up music player on the (private) session bus, as MPRIS has it:
org.mpris.MediaPlayer2.<name> with Player properties and methods. It runs
in a thread of its own, so the tests can call it synchronously."""

import os
import threading

from gi.repository import Gio, GLib

XML = """<node>
  <interface name="org.mpris.MediaPlayer2"><property name="Identity" type="s" access="read"/></interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="PlayPause"/><method name="Play"/><method name="Pause"/>
    <method name="Next"/><method name="Previous"/><method name="Stop"/>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
  </interface>
</node>"""
PLAYER = "org.mpris.MediaPlayer2.Player"


class FakePlayer:
    def __init__(self, name="fake", title="Blue Morning", artist="The Examples"):
        self.name = "org.mpris.MediaPlayer2." + name
        self.status = "Paused"
        self.title, self.artist = title, artist
        self.calls = []
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
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        for iface in node.interfaces:
            self.conn.register_object("/org/mpris/MediaPlayer2", iface, self._method,
                                      self._get, None)
        self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                            "org.freedesktop.DBus", "RequestName",
                            GLib.Variant("(su)", (self.name, 0)), None,
                            Gio.DBusCallFlags.NONE, 3000, None)
        self.loop = GLib.MainLoop(self.ctx)
        ready.set()
        self.loop.run()

    def _props(self):
        return {"PlaybackStatus": GLib.Variant("s", self.status),
                "Metadata": GLib.Variant("a{sv}", {
                    "xesam:title": GLib.Variant("s", self.title),
                    "xesam:artist": GLib.Variant("as", [self.artist])}),
                "CanGoNext": GLib.Variant("b", True), "CanGoPrevious": GLib.Variant("b", False),
                "CanPlay": GLib.Variant("b", True), "CanPause": GLib.Variant("b", True)}

    def _get(self, conn, sender, path, iface, prop):
        if prop == "Identity":
            return GLib.Variant("s", "Example Player")
        return self._props().get(prop)

    def _method(self, conn, sender, path, iface, method, params, inv):
        self.calls.append(method)
        if method == "PlayPause":
            self.status = "Paused" if self.status == "Playing" else "Playing"
            conn.emit_signal(None, path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                             GLib.Variant("(sa{sv}as)", (PLAYER, {
                                 "PlaybackStatus": GLib.Variant("s", self.status)}, [])))
        inv.return_value(None)

    def close(self):
        def stop():
            self.conn.close_sync(None)
            self.loop.quit()
            return False
        src = GLib.idle_source_new()
        src.set_callback(stop)
        src.attach(self.ctx)
        self.thread.join(5)
