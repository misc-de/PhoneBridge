# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The panel icon over D-Bus, as a panel sees it - on the private session
bus of run-tests.sh, with a stand-in StatusNotifierWatcher."""

import os
import unittest

from gi.repository import Gio, GLib

from phonebridge import icon
from phonebridge.tray import Tray, item_props, layout

from .support import run_loop_until

WATCHER_XML = """
<node><interface name="org.kde.StatusNotifierWatcher">
 <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
</interface></node>"""

ITEMS = [{"label": "Phone: 80 %", "enabled": False},
         {"type": "separator"},
         {"id": "device:a", "label": "A_1", "radio": True, "checked": True},
         {"id": "quit", "label": "Quit"}]


class Layout(unittest.TestCase):
    def test_variant_types(self):
        v = GLib.Variant("(u(ia{sv}av))", (3, layout(ITEMS)))
        rev, (root, props, children) = v.unpack()
        self.assertEqual(rev, 3)
        self.assertEqual(root, 0)
        self.assertEqual(len(children), 4)
        self.assertEqual(children[1][1], {"type": "separator"})

    def test_props(self):
        p = {k: v.unpack() for k, v in item_props(ITEMS[2]).items()}
        self.assertEqual(p["label"], "A__1")  # an underscore would be a mnemonic
        self.assertEqual(p["toggle-type"], "radio")
        self.assertEqual(p["toggle-state"], 1)
        self.assertFalse(item_props(ITEMS[0])["enabled"].unpack())


@unittest.skipUnless(os.environ.get("DBUS_SESSION_BUS_ADDRESS"), "no session bus")
class OverDBus(unittest.TestCase):
    def setUp(self):
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.registered = []
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]

        def call(conn, sender, path, iface, method, params, inv):
            self.registered.append(params.unpack()[0])
            inv.return_value(None)

        self.reg = self.conn.register_object("/StatusNotifierWatcher", info, call,
                                             None, None)
        self.own = Gio.bus_own_name_on_connection(
            self.conn, "org.kde.StatusNotifierWatcher", Gio.BusNameOwnerFlags.NONE,
            None, None)
        self.clicked = []
        self.activated = []
        self.tray = Tray(lambda: self.activated.append(1), lambda: None,
                         self.clicked.append)

    def tearDown(self):
        self.tray.close()
        Gio.bus_unown_name(self.own)
        self.conn.unregister_object(self.reg)

    def call(self, path, iface, method, args, reply):
        box = {}

        def done(conn, res):
            try:
                box["r"] = conn.call_finish(res).unpack()
            except GLib.Error as e:
                box["e"] = e

        self.conn.call(self.tray.name, path, iface, method, args,
                       GLib.VariantType(reply) if reply else None,
                       Gio.DBusCallFlags.NONE, 3000, None, done)
        self.assertTrue(run_loop_until(lambda: box, 5))
        if "e" in box:
            raise box["e"]
        return box["r"]

    def prop(self, path, iface, name):
        return self.call(path, "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (iface, name)), "(v)")[0]

    def test_registers_and_serves(self):
        self.assertTrue(run_loop_until(lambda: self.registered, 5))
        self.assertEqual(self.registered, [self.tray.name])

        self.tray.set_icon(icon.pixmaps(percent=50, online=True))
        self.tray.set_tooltip("PhoneBridge – A", "Battery: 50 %")
        self.tray.set_menu(ITEMS)
        sni = "org.kde.StatusNotifierItem"
        pix = self.prop("/StatusNotifierItem", sni, "IconPixmap")
        self.assertEqual([(w, h) for w, h, _d in pix], [(s, s) for s in icon.SIZES])
        self.assertEqual(self.prop("/StatusNotifierItem", sni, "ToolTip")[2:],
                         ("PhoneBridge – A", "Battery: 50 %"))
        self.assertEqual(self.prop("/StatusNotifierItem", sni, "Menu"), "/MenuBar")

        rev, tree = self.call("/MenuBar", "com.canonical.dbusmenu", "GetLayout",
                              GLib.Variant("(iias)", (0, -1, [])), "(u(ia{sv}av))")
        self.assertEqual(tree[2][3][1]["label"], "Quit")

        self.call("/MenuBar", "com.canonical.dbusmenu", "Event",
                  GLib.Variant("(isvu)", (4, "clicked", GLib.Variant("s", ""), 0)), None)
        self.assertTrue(run_loop_until(lambda: self.clicked, 5))
        self.assertEqual(self.clicked, ["quit"])

        self.call("/StatusNotifierItem", sni, "Activate", GLib.Variant("(ii)", (0, 0)), None)
        self.assertTrue(run_loop_until(lambda: self.activated, 5))

        props = self.call("/MenuBar", "com.canonical.dbusmenu", "GetGroupProperties",
                          GLib.Variant("(aias)", ([3], [])), "(a(ia{sv}))")[0]
        self.assertEqual(props[0][1]["toggle-state"], 1)


if __name__ == "__main__":
    unittest.main()
