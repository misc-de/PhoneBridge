# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""A screenshot of the phone, shown on the PC - to copy or to save."""

import base64
import os
import time

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import text
from .i18n import _


def take(app, dev=None):
    dev = dev or app.active_device()
    if dev is None or not dev.online:
        return
    app.tell(_("Taking a screenshot of %s …") % dev.name)

    def got(result, error):
        if error is not None:
            app.tell(text.error(error))
            return
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(base64.b64decode(result["png"])))
        except (GLib.Error, ValueError) as e:
            app.tell(str(e))
            return
        ScreenshotDialog(app, dev, texture).present(app.show_window())

    dev.request("screen.shot", {}, got)


class ScreenshotDialog(Adw.Dialog):
    def __init__(self, app, dev, texture):
        w, h = texture.get_width(), texture.get_height()
        scale = min(1.0, 640 / max(h, 1), 900 / max(w, 1))
        super().__init__(title=_("Screenshot of %s") % dev.name,
                         content_width=max(320, int(w * scale) + 48),
                         content_height=max(320, int(h * scale) + 110))
        self.app, self.texture = app, texture
        self.name = time.strftime(_("Phone screenshot %Y-%m-%d %H-%M-%S") + ".png")
        picture = Gtk.Picture(paintable=texture, can_shrink=True,
                              content_fit=Gtk.ContentFit.CONTAIN, margin_top=12,
                              margin_bottom=12, margin_start=12, margin_end=12)
        header = Adw.HeaderBar()
        copy = Gtk.Button(label=_("Copy"))
        copy.connect("clicked", lambda *a: self.copy())
        save = Gtk.Button(label=_("Save …"))
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *a: self.save())
        header.pack_start(copy)
        header.pack_end(save)
        view = Adw.ToolbarView(content=picture)
        view.add_top_bar(header)
        self.set_child(view)

    def copy(self):
        Gdk.Display.get_default().get_clipboard().set_texture(self.texture)
        self.app.tell(_("Screenshot copied"))

    def save(self):
        folder = os.path.join(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
                              or os.path.expanduser("~/Pictures"))
        dialog = Gtk.FileDialog(title=_("Save screenshot"), initial_name=self.name, modal=True)
        if os.path.isdir(folder):
            dialog.set_initial_folder(Gio.File.new_for_path(folder))

        def chosen(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            if f is not None and f.get_path():
                self.write(f.get_path())

        dialog.save(self.get_root(), None, chosen)

    def write(self, path):
        try:
            self.texture.save_to_png(path)
        except GLib.Error as e:
            self.app.tell(e.message)
            return
        self.app.tell(_("Saved: %s") % os.path.basename(path))
        self.close()
