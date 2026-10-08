# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Sending to the phone: a web link opens in the phone's browser; files
and folders go to its Downloads - under a name of their own when the name
is taken - and the phone says so in a notification.

From the panel menu, the window's menu, `phonebridge --send FILE|URL ...`
and Thunar's "Send To" menu."""

import os
from urllib.parse import urlparse

from gi.repository import Adw, Gdk, GLib, Gtk

from . import files, text
from .i18n import _

WAIT_ONLINE = 30            # s a phone that is still connecting is waited for


def is_web_link(item):
    try:
        u = urlparse((item or "").strip())
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc)


def send(app, items, dev=None):
    """Sends what can be sent; the rest is told. Waits a little for a phone
    that is still connecting (started from Thunar, say)."""
    dev = dev or app.active_device()
    items = [i for i in items if i]
    if dev is None or not items:
        return
    if not dev.online:
        if dev._running and dev.state == "connecting":
            _when_online(app, dev, items)
        else:
            app.tell(_("%s is not connected") % dev.name)
        return
    links = [i.strip() for i in items if is_web_link(i)]
    paths = [i for i in items if not is_web_link(i)]
    missing = [p for p in paths if not os.path.exists(p)]
    paths = [p for p in paths if os.path.exists(p)]
    for p in missing:
        app.tell(_("Not found: %s") % p)
    for link in links:
        dev.request("open.uri", {"uri": link}, lambda r, e, l=link: app.tell(
            _("Opened on %(phone)s: %(what)s") % {"phone": dev.name, "what": l} if e is None
            else _("Not sent: %s") % text.error(e)))
    if paths:
        dev.request("files.places", {}, lambda r, e: _upload(app, dev, paths, r, e))


def _when_online(app, dev, items):
    state = {}

    def changed(d):
        if d.online:
            done()
            send(app, items, d)
        elif d.state == "offline":
            done()
            app.tell(_("%s is not connected") % d.name)

    def timeout():
        state["timer"] = 0
        done()
        app.tell(_("%s is not connected") % dev.name)
        return False

    def done():
        if state.get("handler"):
            dev.disconnect(state.pop("handler"))
        if state.get("timer"):
            GLib.source_remove(state.pop("timer"))

    state["handler"] = dev.connect("changed", changed)
    state["timer"] = GLib.timeout_add_seconds(WAIT_ONLINE, timeout)


def _upload(app, dev, paths, places, error):
    if error is not None:
        app.tell(_("Not sent: %s") % text.error(error))
        return
    folder = next((p["path"] for p in places["places"] if p["id"] == "downloads"),
                  places["home"])
    for local in paths:
        name = os.path.basename(local.rstrip("/"))

        def got(result, error, local=local, name=name):
            if error is not None:
                app.tell(_("Not sent: %s") % text.error(error))
                return
            is_dir = os.path.isdir(local)
            t = files.Transfer(dev, "upload", result["path"], local, is_dir=is_dir,
                               size=files.local_size(local) if is_dir else 0)

            def done(error):
                if error == "cancelled":
                    return
                if error is not None:
                    app.tell(_("Upload of %s failed: %s") % (name, text.error(error)))
                    return
                sent_as = os.path.basename(result["path"])
                dev.request("notify", {"title": _("From the PC"), "body": sent_as})
                app.tell(_("Sent to %(phone)s: %(what)s") % {"phone": dev.name, "what": sent_as})

            if app.window is not None:
                app.window.files.run_transfer(t, done)
            else:
                t.connect("finished", lambda t, error: done(error))
                t.start()

        dev.request("files.free_name", {"path": folder, "name": name}, got)


def ask(app):
    """The dialog: a link to open, or files to choose."""
    dev = app.active_device()
    if dev is None:
        return
    window = app.show_window()
    dialog = Adw.AlertDialog(
        heading=_("Send to %s") % dev.name,
        body=_("A web link opens in the phone's browser; files go to its Downloads folder."))
    entry = Gtk.Entry(placeholder_text="https://…", activates_default=True,
                      input_purpose=Gtk.InputPurpose.URL)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("files", _("Choose files …"))
    dialog.add_response("link", _("Open link"))
    dialog.set_response_appearance("link", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("link")
    dialog.set_close_response("cancel")

    def check(*args):
        dialog.set_response_enabled("link", is_web_link(entry.get_text()))

    entry.connect("changed", check)
    check()

    def pasted(clipboard, res):
        try:
            value = clipboard.read_text_finish(res)
        except GLib.Error:
            return
        if value and is_web_link(value) and not entry.get_text():
            entry.set_text(value.strip())

    Gdk.Display.get_default().get_clipboard().read_text_async(None, pasted)

    def answered(d, response):
        if response == "link":
            send(app, [entry.get_text()], dev)
        elif response == "files":
            chooser = Gtk.FileDialog(title=_("Send to %s") % dev.name, modal=True)

            def chosen(c, res):
                try:
                    model = c.open_multiple_finish(res)
                except GLib.Error:
                    return
                send(app, [model.get_item(i).get_path() for i in range(model.get_n_items())
                           if model.get_item(i).get_path()], dev)

            chooser.open_multiple(window, None, chosen)

    dialog.connect("response", answered)
    dialog.present(window)
    return dialog
