# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""A small evolution-data-server on the private test bus: one address book
and one calendar, with the D-Bus methods PhoneBridge's agent calls. It keeps
vCards and VEVENTs in dicts, as EDS would hand them to the server."""

import re

from gi.repository import Gio, GLib

EDS = "org.gnome.evolution.dataserver."

SOURCES_XML = """<node>
 <interface name="org.freedesktop.DBus.ObjectManager">
  <method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
 </interface></node>"""
BOOK_FACTORY_XML = """<node><interface name="org.gnome.evolution.dataserver.AddressBookFactory">
 <method name="OpenAddressBook"><arg type="s" direction="in"/>
  <arg type="s" direction="out"/><arg type="s" direction="out"/></method>
</interface></node>"""
CAL_FACTORY_XML = """<node><interface name="org.gnome.evolution.dataserver.CalendarFactory">
 <method name="OpenCalendar"><arg type="s" direction="in"/>
  <arg type="s" direction="out"/><arg type="s" direction="out"/></method>
</interface></node>"""
BOOK_XML = """<node><interface name="org.gnome.evolution.dataserver.AddressBook">
 <method name="Open"><arg type="as" direction="out"/></method>
 <method name="GetContactList"><arg type="s" direction="in"/><arg type="as" direction="out"/></method>
 <method name="GetContact"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
 <method name="CreateContacts"><arg type="as" direction="in"/><arg type="u" direction="in"/>
  <arg type="as" direction="out"/></method>
 <method name="ModifyContacts"><arg type="as" direction="in"/><arg type="u" direction="in"/></method>
 <method name="RemoveContacts"><arg type="as" direction="in"/><arg type="u" direction="in"/></method>
 <property name="Writable" type="b" access="read"/>
</interface></node>"""
CAL_XML = """<node><interface name="org.gnome.evolution.dataserver.Calendar">
 <method name="Open"><arg type="as" direction="out"/></method>
 <method name="GetObjectList"><arg type="s" direction="in"/><arg type="as" direction="out"/></method>
 <method name="GetObject"><arg type="s" direction="in"/><arg type="s" direction="in"/>
  <arg type="s" direction="out"/></method>
 <method name="CreateObjects"><arg type="as" direction="in"/><arg type="u" direction="in"/>
  <arg type="as" direction="out"/></method>
 <method name="ModifyObjects"><arg type="as" direction="in"/><arg type="s" direction="in"/>
  <arg type="u" direction="in"/></method>
 <method name="RemoveObjects"><arg type="a(ss)" direction="in"/><arg type="s" direction="in"/>
  <arg type="u" direction="in"/></method>
 <property name="Writable" type="b" access="read"/>
</interface></node>"""

BOOK_UID = "book-contacts"
CAL_UID = "cal-personal"
ACCOUNT_UID = "account-1"


def _iface(xml):
    return Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]


def _uid(text):
    m = re.search(r"^UID:(.*)$", text.replace("\r", ""), re.M)
    return m.group(1).strip() if m else None


class FakeEDS:
    def __init__(self):
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.contacts = {}
        self.events = {}           # uid -> VEVENT text (series and exceptions together)
        self.calls = []
        self._n = 0
        self._reg = []
        self._own = []
        self._register()

    def _register(self):
        sources = _iface(SOURCES_XML)
        self._reg.append(self.conn.register_object(
            "/org/gnome/evolution/dataserver/SourceManager", sources, self._sources, None, None))
        self._reg.append(self.conn.register_object(
            "/org/gnome/evolution/dataserver/AddressBookFactory", _iface(BOOK_FACTORY_XML),
            lambda *a: self._factory("/book/1", EDS + "AddressBook10", *a), None, None))
        self._reg.append(self.conn.register_object(
            "/org/gnome/evolution/dataserver/CalendarFactory", _iface(CAL_FACTORY_XML),
            lambda *a: self._factory("/cal/1", EDS + "Calendar8", *a), None, None))
        self._reg.append(self.conn.register_object(
            "/book/1", _iface(BOOK_XML), self._book, self._writable, None))
        self._reg.append(self.conn.register_object(
            "/cal/1", _iface(CAL_XML), self._cal, self._writable, None))
        for name in ("Sources5", "AddressBook10", "Calendar8"):
            self._own.append(Gio.bus_own_name_on_connection(
                self.conn, EDS + name, Gio.BusNameOwnerFlags.NONE, None, None))

    def close(self):
        for o in self._own:
            Gio.bus_unown_name(o)
        for r in self._reg:
            self.conn.unregister_object(r)

    def _writable(self, *args):
        return GLib.Variant("b", True)

    def _sources(self, conn, sender, path, iface, method, params, inv):
        def source(uid, data):
            return {EDS + "Source": {"UID": GLib.Variant("s", uid),
                                     "Data": GLib.Variant("s", data)}}
        objs = {
            "/s/0": source(ACCOUNT_UID, "[Data Source]\nDisplayName=me@cloud.example\n"
                                        "Enabled=true\n[Collection]\nBackendName=webdav\n"),
            "/s/1": source(BOOK_UID, "[Data Source]\nDisplayName=Contacts\nEnabled=true\n"
                                     "Parent=%s\n[Address Book]\nBackendName=carddav\n"
                           % ACCOUNT_UID),
            "/s/2": source(CAL_UID, "[Data Source]\nDisplayName=Personal\nEnabled=true\n"
                                    "Parent=%s\n[Calendar]\nBackendName=caldav\n"
                                    "Color=#62a0ea\n" % ACCOUNT_UID),
            "/s/3": source("off", "[Data Source]\nDisplayName=Off\nEnabled=false\n"
                                  "[Calendar]\nBackendName=caldav\n"),
        }
        inv.return_value(GLib.Variant("(a{oa{sa{sv}}})", (objs,)))

    def _factory(self, obj, name, conn, sender, path, iface, method, params, inv):
        inv.return_value(GLib.Variant("(ss)", (obj, name)))

    def _new_uid(self):
        self._n += 1
        return "uid-%d" % self._n

    def _book(self, conn, sender, path, iface, method, params, inv):
        self.calls.append(method)
        args = params.unpack()
        if method == "Open":
            inv.return_value(GLib.Variant("(as)", ([],)))
        elif method == "GetContactList":
            inv.return_value(GLib.Variant("(as)", (list(self.contacts.values()),)))
        elif method == "GetContact":
            if args[0] not in self.contacts:
                inv.return_dbus_error(EDS + "AddressBook.Error.ContactNotFound", "not found")
                return
            inv.return_value(GLib.Variant("(s)", (self.contacts[args[0]],)))
        elif method == "CreateContacts":
            uids = []
            for v in args[0]:
                uid = self._new_uid()
                v = v.replace("BEGIN:VCARD\r\n", "BEGIN:VCARD\r\nUID:%s\r\n" % uid, 1)
                self.contacts[uid] = v
                uids.append(uid)
            inv.return_value(GLib.Variant("(as)", (uids,)))
        elif method == "ModifyContacts":
            for v in args[0]:
                self.contacts[_uid(v)] = v
            inv.return_value(None)
        elif method == "RemoveContacts":
            for uid in args[0]:
                self.contacts.pop(uid, None)
            inv.return_value(None)

    def _cal(self, conn, sender, path, iface, method, params, inv):
        self.calls.append(method)
        args = params.unpack()
        if method == "Open":
            inv.return_value(GLib.Variant("(as)", ([],)))
        elif method == "GetObjectList":
            inv.return_value(GLib.Variant("(as)", (list(self.events.values()),)))
        elif method == "GetObject":
            if args[0] not in self.events:
                inv.return_dbus_error(EDS + "Calendar.Error.ObjectNotFound", "not found")
                return
            inv.return_value(GLib.Variant("(s)", (self.events[args[0]],)))
        elif method == "CreateObjects":
            uids = []
            for v in args[0]:
                uid = _uid(v) or self._new_uid()
                self.events[uid] = v
                uids.append(uid)
            inv.return_value(GLib.Variant("(as)", (uids,)))
        elif method == "ModifyObjects":
            for v in args[0]:
                self.events[_uid(v)] = v
            inv.return_value(None)
        elif method == "RemoveObjects":
            for uid, rid in args[0]:
                self.events.pop(uid, None)
            inv.return_value(None)
