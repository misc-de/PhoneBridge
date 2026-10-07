# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The panel icon: a StatusNotifierItem with a com.canonical.dbusmenu menu,
spoken directly over D-Bus - no libappindicator, which is GTK 3 only.

XFCE's "Status Tray" plugin (systray), KDE, Cinnamon, MATE, Budgie and
waybar all host these. When the host restarts (panel restart), the item
registers itself again.

Menu items are dicts: {"id": str, "label": str, "enabled": bool,
"type": "separator", "radio": bool, "checked": bool}. A click calls
on_item(id); a left click on the icon on_activate(), a middle click
on_secondary()."""

import os

from gi.repository import Gio, GLib

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

SNI_XML = """
<node>
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg type="s"/></signal>
 </interface>
</node>"""

MENU_XML = """
<node>
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
   <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
   <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
   <arg type="ai" direction="in"/><arg type="as" direction="in"/>
   <arg type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
   <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
  </method>
  <method name="Event">
   <arg type="i" direction="in"/><arg type="s" direction="in"/>
   <arg type="v" direction="in"/><arg type="u" direction="in"/>
  </method>
  <method name="EventGroup">
   <arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/>
  </method>
  <method name="AboutToShow">
   <arg type="i" direction="in"/><arg type="b" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
   <arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/>
  </method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  <signal name="ItemsPropertiesUpdated">
   <arg type="a(ia{sv})"/><arg type="a(ias)"/>
  </signal>
 </interface>
</node>"""


def item_props(item):
    """dbusmenu properties of one menu item."""
    if item.get("type") == "separator":
        return {"type": GLib.Variant("s", "separator")}
    props = {"label": GLib.Variant("s", item.get("label", "").replace("_", "__")),
             "enabled": GLib.Variant("b", item.get("enabled", True))}
    if item.get("radio"):
        props["toggle-type"] = GLib.Variant("s", "radio")
        props["toggle-state"] = GLib.Variant("i", 1 if item.get("checked") else 0)
    if item.get("icon"):
        props["icon-name"] = GLib.Variant("s", item["icon"])
    return props


def layout(items):
    """The (ia{sv}av) tree of a flat menu: root 0, items numbered from 1."""
    children = [GLib.Variant("(ia{sv}av)", (n, item_props(item), []))
                for n, item in enumerate(items, 1)]
    return (0, {"children-display": GLib.Variant("s", "submenu")}, children)


class Tray:
    def __init__(self, on_activate, on_secondary, on_item):
        self.on_activate = on_activate
        self.on_secondary = on_secondary
        self.on_item = on_item
        self.title = "PhoneBridge"
        self.pixmaps = []
        self.tooltip = ("", "")
        self.items = []
        self.revision = 1
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.name = "org.kde.StatusNotifierItem-%d-1" % os.getpid()
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._ids = [
            self.conn.register_object(ITEM_PATH, sni, self._item_call,
                                      self._item_prop, None),
            self.conn.register_object(MENU_PATH, menu, self._menu_call,
                                      self._menu_prop, None),
        ]
        self._own = Gio.bus_own_name_on_connection(
            self.conn, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        self._watch = Gio.bus_watch_name_on_connection(
            self.conn, WATCHER, Gio.BusNameWatcherFlags.NONE,
            self._watcher_appeared, None)

    def close(self):
        Gio.bus_unwatch_name(self._watch)
        Gio.bus_unown_name(self._own)
        for rid in self._ids:
            self.conn.unregister_object(rid)

    # -- what the app sets ------------------------------------------------
    def set_icon(self, pixmaps):
        if pixmaps != self.pixmaps:
            self.pixmaps = pixmaps
            self._signal(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon")

    def set_tooltip(self, title, text):
        # some hosts (KDE ...) read the tooltip as rich text; SSIDs and
        # operator names come from strangers
        title, text = GLib.markup_escape_text(title), GLib.markup_escape_text(text)
        if (title, text) != self.tooltip:
            self.tooltip = (title, text)
            self.title = title
            self._signal(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip")
            self._signal(ITEM_PATH, "org.kde.StatusNotifierItem", "NewTitle")

    def set_menu(self, items):
        if items != self.items:
            self.items = items
            self.revision += 1
            self._signal(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                         GLib.Variant("(ui)", (self.revision, 0)))

    # -- D-Bus ------------------------------------------------------------
    def _signal(self, path, iface, name, params=None):
        try:
            self.conn.emit_signal(None, path, iface, name, params)
        except GLib.Error:
            pass

    def _watcher_appeared(self, conn, name, owner, attempt=0):
        def done(c, res):
            try:
                c.call_finish(res)
            except GLib.Error as e:
                print("phonebridge: panel icon not registered:", e.message)
                if attempt < 3:             # the host may still be starting
                    GLib.timeout_add_seconds(2, lambda: self._watcher_appeared(
                        conn, name, owner, attempt + 1) and False)

        conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER,
                  "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.name,)),
                  None, Gio.DBusCallFlags.NONE, 5000, None, done)

    def _item_prop(self, conn, sender, path, iface, prop):
        values = {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", "phonebridge"),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", "" if self.pixmaps else "phone"),
            "IconPixmap": GLib.Variant("a(iiay)", self.pixmaps),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)",
                                    ("", [], self.tooltip[0], self.tooltip[1])),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _item_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "Activate":
            GLib.idle_add(lambda: self.on_activate() and False)
        elif method == "SecondaryActivate":
            GLib.idle_add(lambda: self.on_secondary() and False)
        invocation.return_value(None)

    def _menu_prop(self, conn, sender, path, iface, prop):
        return {"Version": GLib.Variant("u", 3),
                "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"),
                "IconThemePath": GLib.Variant("as", [])}.get(prop)

    def _props_of(self, n):
        if n == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        if 1 <= n <= len(self.items):
            return item_props(self.items[n - 1])
        return {}

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(GLib.Variant(
                "(u(ia{sv}av))", (self.revision, layout(self.items))))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0] or range(len(self.items) + 1)
            invocation.return_value(GLib.Variant(
                "(a(ia{sv}))", ([(n, self._props_of(n)) for n in ids],)))
        elif method == "GetProperty":
            n, name = params.unpack()
            value = self._props_of(n).get(name)
            if value is None:
                invocation.return_dbus_error("com.canonical.dbusmenu.UnknownProperty",
                                             name)
            else:
                invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            n, event = params.unpack()[:2]
            self._event(n, event)
            invocation.return_value(None)
        elif method == "EventGroup":
            for n, event, _data, _ts in params.unpack()[0]:
                self._event(n, event)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod",
                                         method)

    def _event(self, n, event):
        if event == "clicked" and 1 <= n <= len(self.items):
            item_id = self.items[n - 1].get("id")
            if item_id:
                GLib.idle_add(lambda: self.on_item(item_id) and False)
