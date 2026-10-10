# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Files: the phone's folders - open, download, upload, new folder, rename,
delete. Folders and thumbnails come from the agent; the contents travel
over an ssh of their own (files.py), so a big transfer never holds up calls
or messages.

The list is a Gtk.ColumnView over a Gio.ListStore: thousands of entries
cost only the rows on screen, and thumbnails are asked for only for rows
that are shown, a few at a time."""

import base64
import os
import posixpath

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from . import files, text
from .i18n import N_, _, n_

PLACES = (("home", N_("Home folder"), "user-home-symbolic"),
          ("documents", N_("Documents"), "folder-documents-symbolic"),
          ("downloads", N_("Downloads"), "folder-download-symbolic"),
          ("pictures", N_("Pictures"), "folder-pictures-symbolic"),
          ("music", N_("Music"), "folder-music-symbolic"),
          ("videos", N_("Videos"), "folder-videos-symbolic"))
THUMB_DELAY = 120           # ms: rows scrolled past in that time ask for nothing
ZOOM = (16, 24, 32, 48, 64, 96, 128)    # icon and thumbnail sizes of the list
ZOOM_DEFAULT = 2


class FileItem(GObject.Object):
    def __init__(self, folder, entry):
        super().__init__()
        self.name = entry["name"]
        self.is_dir = bool(entry["dir"])
        self.size = entry.get("size") or 0
        self.mtime = entry.get("mtime") or 0
        self.link = bool(entry.get("link"))
        self.path = posixpath.join(folder, self.name)
        self.hidden = self.name.startswith(".")
        self.content_type = ("inode/directory" if self.is_dir
                             else Gio.content_type_guess(self.name, None)[0])
        self.thumb_key = "%s\0%d" % (self.path, self.mtime)

    @property
    def wants_thumb(self):
        return not self.is_dir and self.content_type.split("/")[0] in ("image", "video")

    @property
    def playable(self):
        """Music: played here, in the page's player."""
        return not self.is_dir and self.content_type.startswith("audio/")


def _name_sort(a, b, _data=None):
    x, y = a.name.casefold(), b.name.casefold()
    return (x > y) - (x < y)


class TransferRow(Gtk.ListBoxRow):
    def __init__(self, transfer):
        super().__init__(activatable=False)
        self.transfer = transfer
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=12,
                      margin_end=12)
        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        head = Gtk.Box(spacing=8)
        verb = _("Downloading") if transfer.kind == "download" else _("Uploading")
        name = Gtk.Label(label="%s: %s" % (verb, transfer.name), xalign=0, hexpand=True,
                         ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.amount = Gtk.Label(xalign=1)
        self.amount.add_css_class("dim-label")
        self.amount.add_css_class("caption")
        head.append(name)
        head.append(self.amount)
        self.bar = Gtk.ProgressBar()
        lines.append(head)
        lines.append(self.bar)
        cancel = Gtk.Button(label=_("Cancel"), valign=Gtk.Align.CENTER)
        cancel.connect("clicked", lambda *a: transfer.cancel())
        box.append(lines)
        box.append(cancel)
        self.set_child(box)
        transfer.connect("progress", self._progress)
        self._progress(transfer, 0, transfer.total)

    def _progress(self, transfer, done, total):
        if total > 0:
            self.bar.set_fraction(min(1.0, done / total))
            self.amount.set_label(_("%(done)s of %(total)s") % {
                "done": GLib.format_size(int(done)), "total": GLib.format_size(int(total))})
        else:
            self.bar.pulse()
            self.amount.set_label(GLib.format_size(int(done)))


class FilesPage(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.dev = None
        self.path = None            # the folder shown
        self.home = None
        self.places = {}
        self.writable = False
        self.names = set()
        self._history = []
        self._serial = 0
        self._loaded_for = None
        self._thumbs = {}           # thumb key -> Gdk.Texture (or None: there is none)
        self._shown = {}            # thumb key -> the Gtk.Image showing it now
        self._thumb_queue = []
        self._thumb_busy = False
        self._thumb_timer = 0
        self._watching = {}         # local copy -> (monitor, device, remote, mtime)
        self._name_boxes = []       # every name cell made: their icons follow the zoom
        self._reveal = None         # a name to show once its folder is there (search)
        self._target = None         # the folder last asked for, until it is there
        self._scrolled = 0.0
        z = app.cfg.get("files_zoom", ZOOM_DEFAULT)
        self.zoom = z if isinstance(z, int) and 0 <= z < len(ZOOM) else ZOOM_DEFAULT
        files.clean_open_cache()

        self._actions()
        self.split = Adw.OverlaySplitView(sidebar=self._sidebar(), content=self._content(),
                                          min_sidebar_width=180, max_sidebar_width=240,
                                          vexpand=True)
        self.append(self.split)
        self.split.bind_property("show-sidebar", self.sidebar_toggle, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL
                                 | GObject.BindingFlags.SYNC_CREATE)
        self.split.bind_property("collapsed", self.sidebar_toggle, "visible",
                                 GObject.BindingFlags.SYNC_CREATE)
        self._shortcuts()
        self._update_actions()
        self._update_zoom()

    # -- building ----------------------------------------------------------------
    def _actions(self):
        self.group = Gio.SimpleActionGroup()
        self.acts = {}
        for name, cb in (("open", lambda *a: self.open_selected()),
                         ("play", lambda *a: self.play_selected()),
                         ("stop", lambda *a: self.stop_playing()),
                         ("download", lambda *a: self.download_selected()),
                         ("download-to", lambda *a: self.download_selected(ask=True)),
                         ("rename", lambda *a: self.ask_rename()),
                         ("delete", lambda *a: self.ask_delete()),
                         ("new-folder", lambda *a: self.ask_new_folder()),
                         ("upload", lambda *a: self.ask_upload()),
                         ("upload-folder", lambda *a: self.ask_upload(folder=True)),
                         ("refresh", lambda *a: self.refresh()),
                         ("up", lambda *a: self.go_up()),
                         ("back", lambda *a: self.go_back()),
                         ("zoom-in", lambda *a: self.set_zoom(self.zoom + 1)),
                         ("zoom-out", lambda *a: self.set_zoom(self.zoom - 1)),
                         ("zoom-reset", lambda *a: self.set_zoom(ZOOM_DEFAULT))):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.group.add_action(action)
            self.acts[name] = action
        hidden = Gio.SimpleAction.new_stateful("hidden", None, GLib.Variant("b", False))
        hidden.connect("change-state", self._on_hidden)
        self.group.add_action(hidden)
        self.acts["hidden"] = hidden
        self.acts["stop"].set_enabled(False)
        self.insert_action_group("files", self.group)

    def _shortcuts(self):
        controller = Gtk.ShortcutController()
        for keys, action in (("Delete", "delete"), ("F2", "rename"), ("F5", "refresh"),
                             ("BackSpace|<Alt>Up", "up"), ("<Alt>Left", "back"),
                             ("<Control>h", "hidden"),
                             ("<Control>plus|<Control>equal|<Control>KP_Add", "zoom-in"),
                             ("<Control>minus|<Control>KP_Subtract", "zoom-out"),
                             ("<Control>0|<Control>KP_0", "zoom-reset")):
            controller.add_shortcut(Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string(keys),
                action=Gtk.NamedAction.new("files." + action)))
        self.add_controller(controller)

    def _sidebar(self):
        self.place_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.place_list.add_css_class("navigation-sidebar")
        self.place_rows = {}
        for pid, label, icon in PLACES:
            row = Gtk.ListBoxRow()
            row.place = pid
            box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=_(label), xalign=0))
            row.set_child(box)
            row.set_visible(pid == "home")
            self.place_list.append(row)
            self.place_rows[pid] = row
        self.place_list.connect("row-activated", self._on_place)
        scroller = Gtk.ScrolledWindow(child=self.place_list, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        # bottom left: the thumbnail of the one picture or video selected
        self.preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                               margin_top=6, margin_bottom=12, margin_start=12,
                               margin_end=12, visible=False)
        self.preview.key = None
        self.preview_picture = Gtk.Picture(content_fit=Gtk.ContentFit.SCALE_DOWN,
                                           halign=Gtk.Align.START, can_shrink=True)
        self.preview_name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.preview_name.add_css_class("caption")
        self.preview_name.add_css_class("dim-label")
        self.preview.append(self.preview_picture)
        self.preview.append(self.preview_name)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(scroller)
        box.append(self.preview)
        return box

    def _content(self):
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        self.sidebar_toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                               tooltip_text=_("Places"))
        bar.append(self.sidebar_toggle)
        nav = Gtk.Box()
        nav.add_css_class("linked")
        nav.append(Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Back"),
                              action_name="files.back"))
        nav.append(Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Up"),
                              action_name="files.up"))
        bar.append(nav)
        self.crumbs = Gtk.Box()
        self.crumbs.add_css_class("linked")
        crumb_scroll = Gtk.ScrolledWindow(child=self.crumbs, hexpand=True,
                                          vscrollbar_policy=Gtk.PolicyType.NEVER,
                                          hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                          propagate_natural_height=True,
                                          propagate_natural_width=True)
        self.crumb_scroll = crumb_scroll
        bar.append(crumb_scroll)
        self.search = Gtk.SearchEntry(placeholder_text=_("Filter"), width_chars=12)
        self.search.connect("search-changed", lambda *a: self.filter.changed(
            Gtk.FilterChange.DIFFERENT))
        bar.append(self.search)
        bar.append(Gtk.Button(label=_("New folder"), action_name="files.new-folder"))
        upload = Gtk.Button(label=_("Upload …"), action_name="files.upload")
        upload.add_css_class("suggested-action")
        bar.append(upload)
        menu = Gio.Menu()
        menu.append(_("Upload folder …"), "files.upload-folder")
        menu.append(_("Show hidden files"), "files.hidden")
        menu.append(_("Refresh"), "files.refresh")
        zoom = Gio.Menu()
        zoom.append(_("Larger"), "files.zoom-in")
        zoom.append(_("Smaller"), "files.zoom-out")
        zoom.append(_("Normal size"), "files.zoom-reset")
        menu.append_section(None, zoom)
        bar.append(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu,
                                  tooltip_text=_("More")))

        self.store = Gio.ListStore(item_type=FileItem)
        self.filter = Gtk.CustomFilter.new(self._visible, None)
        filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.view = Gtk.ColumnView(vexpand=True, hexpand=True)
        self.view.add_css_class("data-table")
        name_col = self._column(_("Name"), self._setup_name, self._bind_name,
                                Gtk.CustomSorter.new(_name_sort, None), expand=True)
        size_col = self._column(_("Size"), self._setup_label, self._bind_size,
                                Gtk.CustomSorter.new(
                                    lambda a, b, d: (a.size > b.size) - (a.size < b.size), None))
        date_col = self._column(_("Modified"), self._setup_label, self._bind_date,
                                Gtk.CustomSorter.new(
                                    lambda a, b, d: (a.mtime > b.mtime) - (a.mtime < b.mtime),
                                    None))
        for col in (name_col, size_col, date_col):
            self.view.append_column(col)
        sorter = Gtk.MultiSorter()
        sorter.append(Gtk.CustomSorter.new(lambda a, b, d: int(b.is_dir) - int(a.is_dir), None))
        sorter.append(self.view.get_sorter())
        self.sorted = Gtk.SortListModel(model=filtered, sorter=sorter)
        self.selection = Gtk.MultiSelection(model=self.sorted)
        self.selection.connect("selection-changed", lambda *a: self._update_actions())
        self.view.set_model(self.selection)
        self.view.sort_by_column(name_col, Gtk.SortType.ASCENDING)
        self.view.connect("activate",
                          lambda v, pos: self.activate_item(self.sorted.get_item(pos)))

        self.status = Adw.StatusPage(icon_name="folder-symbolic")
        self.stack = Gtk.Stack()
        self.stack.add_named(Gtk.ScrolledWindow(child=self.view), "list")
        # Ctrl + wheel (or touchpad): larger, smaller - before the list scrolls
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL,
                                           propagation_phase=Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", lambda c, dx, dy: self.zoom_scroll(
            dy, c.get_current_event_state()))
        self.view.add_controller(scroll)
        self.stack.add_named(self.status, "status")

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.stack.add_controller(drop)

        # the music file playing: fetched like one opened, played here
        self.player_name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                                     max_width_chars=32)
        self.player_controls = Gtk.MediaControls(hexpand=True)
        stop = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=_("Stop"),
                          valign=Gtk.Align.CENTER, action_name="files.stop")
        stop.add_css_class("flat")
        player = Gtk.Box(spacing=8, margin_top=3, margin_bottom=3, margin_start=12,
                         margin_end=6)
        player.append(Gtk.Image(icon_name="audio-x-generic-symbolic"))
        player.append(self.player_name)
        player.append(self.player_controls)
        player.append(stop)
        self.player = Gtk.Revealer(child=player, reveal_child=False)
        self._play_want = None      # the copy being fetched to play

        self.transfers = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.transfers.add_css_class("boxed-list")
        self.transfer_revealer = Gtk.Revealer(child=Gtk.Box(
            margin_start=6, margin_end=6, margin_bottom=6), reveal_child=False)
        self.transfer_revealer.get_child().append(self.transfers)
        self.transfers.set_hexpand(True)

        self.action_bar = Gtk.ActionBar(revealed=False)
        self.selected_label = Gtk.Label()
        self.action_bar.pack_start(self.selected_label)
        for label, action in ((_("Delete"), "delete"), (_("Rename"), "rename"),
                              (_("Download"), "download"), (_("Open"), "open"),
                              (_("Play"), "play")):
            b = Gtk.Button(label=label, action_name="files." + action)
            if action == "delete":
                b.add_css_class("destructive-action")
            elif action == "play":
                self.play_button = b
            self.action_bar.pack_end(b)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(bar)
        box.append(self.stack)
        box.append(self.player)
        box.append(self.transfer_revealer)
        box.append(self.action_bar)
        return box

    def _column(self, title, setup, bind, sorter, expand=False):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", setup)
        factory.connect("bind", bind)
        if bind == self._bind_name:
            factory.connect("unbind", self._unbind_name)
        col = Gtk.ColumnViewColumn(title=title, factory=factory, expand=expand,
                                   resizable=True, sorter=sorter)
        return col

    def _cell_menu(self, widget, item):
        """Right click on a row: it is selected (if not yet) and the menu shows."""
        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", lambda g, n, x, y: self._popup(widget, item, x, y))
        widget.add_controller(click)

    def _setup_name(self, factory, item):
        box = Gtk.Box(spacing=10, margin_top=2, margin_bottom=2)
        box.image = Gtk.Image(pixel_size=ZOOM[self.zoom])
        box.label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        box.append(box.image)
        box.append(box.label)
        item.set_child(box)
        self._name_boxes.append(box)
        self._cell_menu(box, item)

    def _setup_label(self, factory, item):
        label = Gtk.Label(xalign=0)
        label.add_css_class("dim-label")
        item.set_child(label)
        self._cell_menu(label, item)

    def _bind_name(self, factory, list_item):
        f, box = list_item.get_item(), list_item.get_child()
        box.label.set_label(f.name)
        box.key = f.thumb_key
        texture = self._thumbs.get(f.thumb_key)
        if texture is not None:
            box.image.set_from_paintable(texture)
        else:
            box.image.set_from_gicon(Gio.content_type_get_icon(f.content_type))
            if f.wants_thumb and f.thumb_key not in self._thumbs:
                self._shown[f.thumb_key] = box.image
                self._want_thumb(f)

    def _unbind_name(self, factory, list_item):
        box = list_item.get_child()
        if self._shown.get(getattr(box, "key", None)) is box.image:
            del self._shown[box.key]

    def _bind_size(self, factory, list_item):
        f = list_item.get_item()
        list_item.get_child().set_label("" if f.is_dir else GLib.format_size(f.size))

    def _bind_date(self, factory, list_item):
        f = list_item.get_item()
        list_item.get_child().set_label(text.activity(f.mtime) if f.mtime else "")

    # -- zoom ------------------------------------------------------------------------------
    def set_zoom(self, level):
        level = max(0, min(len(ZOOM) - 1, level))
        if level != self.zoom:
            self.zoom = level
            for box in self._name_boxes:
                box.image.set_pixel_size(ZOOM[level])
            self.app.cfg["files_zoom"] = level
            from . import config
            config.save(self.app.cfg)
        self._update_zoom()

    def _update_zoom(self):
        self.acts["zoom-in"].set_enabled(self.zoom < len(ZOOM) - 1)
        self.acts["zoom-out"].set_enabled(self.zoom > 0)
        self.acts["zoom-reset"].set_enabled(self.zoom != ZOOM_DEFAULT)

    def zoom_scroll(self, dy, state):
        """With Ctrl: a step per notch of the wheel - a touchpad's small
        steps add up to one. Without: the list scrolls."""
        if not state & Gdk.ModifierType.CONTROL_MASK:
            self._scrolled = 0.0
            return False
        self._scrolled += dy
        while abs(self._scrolled) >= 1:
            step = 1 if self._scrolled < 0 else -1      # up: larger
            self._scrolled += step
            self.set_zoom(self.zoom + step)
        return True

    # -- the phone ---------------------------------------------------------------------
    def set_device(self, dev):
        if dev is not self.dev:
            if self.dev is not None:
                self.stop_playing()        # the other phone's music
            self.dev = dev
            self._serial += 1
            self._loaded_for = None
            self.path = self.home = self._target = None
            self._history = []
            self._thumbs.clear()
            self._shown.clear()
            self._thumb_queue = []
            self.store.remove_all()
        self.device_changed()

    def device_changed(self):
        if self.dev is not None and self.dev.online and self.get_mapped():
            self.load()
        self._update_actions()

    def load(self, force=False):
        dev = self.dev
        if dev is None or not dev.online:
            self._status(_("Not connected"))
            return
        if self._loaded_for is dev and not force:
            return
        self._loaded_for = dev
        if self.path is None:
            self._status(_("Loading …"))
        self.navigate(self.path or self._target, push=False)

    def _set_places(self, result):
        self.home = result["home"]
        self.places = {"home": result["home"]}
        self.places.update({p["id"]: p["path"] for p in result["places"]
                            if self._in_home(p["path"])})
        for pid, row in self.place_rows.items():
            row.set_visible(pid in self.places)

    def refresh(self):
        if self.path is not None:
            self.navigate(self.path, push=False)

    def _in_home(self, path):
        """Only the home folder and what is below it is shown."""
        home = (self.home or "").rstrip("/")
        return bool(home) and (path == self.home or path.rstrip("/") == home
                               or path.startswith(home + "/"))

    def navigate(self, path, push=True):
        """path None or outside the home folder: the home folder."""
        dev = self.dev
        if dev is None or not dev.online:
            return
        self._target = path
        if self.home is None:       # first where the home folder is
            self._serial += 1
            serial = self._serial

            def got_places(result, error):
                if serial != self._serial:
                    return
                if error is not None:
                    self._status(text.error(error))
                    return
                self._set_places(result)
                self.navigate(path, push=False)

            dev.request("files.places", {}, got_places)
            return
        if not path or not self._in_home(path):
            path = self.home
        self._serial += 1
        serial = self._serial

        def got(result, error):
            if serial != self._serial:
                return
            if error is not None:
                self.app.toast("%s: %s" % (path, text.error(error)))
                if self.path is None:
                    self._status(text.error(error))
                return
            if not self._in_home(result["path"]):
                self.navigate(self.home, push=push)
                return
            if push and self.path is not None and self.path != result["path"]:
                self._history.append(self.path)
            self._show_folder(result)

        dev.request("files.list", {"path": path}, got)

    def _show_folder(self, result):
        folder = result["path"]
        changed = folder != self.path
        self.path = folder
        self.writable = result["writable"]
        items = [FileItem(folder, e) for e in result["entries"]]
        self.names = {i.name for i in items}
        self._shown.clear()
        self._thumb_queue = []
        self.store.splice(0, self.store.get_n_items(), items)
        if changed:
            self.search.set_text("")
            if self.sorted.get_n_items():
                self.view.scroll_to(0, None, Gtk.ListScrollFlags.NONE, None)
        if self._reveal is not None:
            self.search.set_text(self._reveal)      # the file looked for, alone
            self.filter.changed(Gtk.FilterChange.DIFFERENT)
            self._reveal = None
        self._crumbs()
        self._mark_place()
        if result.get("truncated"):
            self.app.toast(_("Only the first %d entries are shown.") % len(items))
        self._update_empty()
        self._update_actions()

    def _update_empty(self):
        if self.sorted.get_n_items():
            self.stack.set_visible_child_name("list")
        elif self.store.get_n_items() and self.search.get_text():
            self._status(_("Nothing matches"))
        else:
            self._status(_("Empty folder"))

    def _status(self, title):
        self.status.set_title(title)
        self.stack.set_visible_child_name("status")

    def _visible(self, item, _data=None):
        if item.hidden and not self.acts["hidden"].get_state().get_boolean():
            return False
        q = self.search.get_text().strip().casefold() if hasattr(self, "search") else ""
        return not q or q in item.name.casefold()

    def _on_hidden(self, action, value):
        action.set_state(value)
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_empty()

    # -- path bar and places ---------------------------------------------------------
    def _crumbs(self):
        while (child := self.crumbs.get_first_child()) is not None:
            self.crumbs.remove(child)
        path = self.path
        first = Gtk.Button(icon_name="user-home-symbolic", tooltip_text=_("Home folder"))
        rest = path[len(self.home.rstrip("/")):].strip("/")
        base = self.home
        parts = [(first, base)]
        for name in [p for p in rest.split("/") if p]:
            base = posixpath.join(base, name)
            b = Gtk.Button(label=name)
            b.get_child().set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            b.get_child().set_max_width_chars(24)
            parts.append((b, base))
        for b, target in parts:
            b.connect("clicked", lambda _b, t=target: t != self.path and self.navigate(t))
            self.crumbs.append(b)
        GLib.idle_add(self._crumbs_to_end)

    def _crumbs_to_end(self):
        adj = self.crumb_scroll.get_hadjustment()
        adj.set_value(adj.get_upper())
        return False

    def _mark_place(self):
        self.place_list.unselect_all()
        best = None
        for pid, path in self.places.items():
            if self.path == path:
                best = pid
                break
        if best is not None:
            self.place_list.select_row(self.place_rows[best])

    def _on_place(self, listbox, row):
        path = self.places.get(row.place)
        if path and path != self.path:
            self.navigate(path)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def reveal(self, path, is_dir=False):
        """A folder opened - or a file shown in its folder (from the search)."""
        if is_dir:
            self.navigate(path)
            return
        self._reveal = posixpath.basename(path)
        self.navigate(posixpath.dirname(path))

    def go_up(self):
        if self.path and self._in_home(self.path) and self.path.rstrip("/") != self.home.rstrip("/"):
            self.navigate(posixpath.dirname(self.path))

    def go_back(self):
        if self._history:
            self.navigate(self._history.pop(), push=False)

    # -- thumbnails ---------------------------------------------------------------------
    def _want_thumb(self, item):
        self._thumb_queue.append(item)             # doubles go in _ask_thumbs
        if not self._thumb_timer and not self._thumb_busy:
            self._thumb_timer = GLib.timeout_add(THUMB_DELAY, self._ask_thumbs)

    def _ask_thumbs(self):
        self._thumb_timer = 0
        dev = self.dev
        # only what is still on screen, and not known yet
        wanted, seen = [], set()
        for item in self._thumb_queue:
            if (item.thumb_key in self._shown and item.thumb_key not in self._thumbs
                    and item.thumb_key not in seen):
                wanted.append(item)
                seen.add(item.thumb_key)
        self._thumb_queue = []
        batch, rest = wanted[:24], wanted[24:]
        if not batch or dev is None or not dev.online:
            return False
        self._thumb_busy = True
        serial = self._serial
        keys = {i.path: i.thumb_key for i in batch}

        def got(result, error):
            self._thumb_busy = False
            if serial != self._serial:
                return
            for path, data in (result or {}).items():
                key = keys.get(path)
                if key is None:
                    continue
                texture = None
                if data:
                    try:
                        texture = Gdk.Texture.new_from_bytes(
                            GLib.Bytes.new(base64.b64decode(data)))
                    except (GLib.Error, ValueError):
                        texture = None
                self._thumbs[key] = texture
                image = self._shown.get(key)
                if image is not None and texture is not None:
                    image.set_from_paintable(texture)
            if error is not None:
                for key in keys.values():
                    self._thumbs.setdefault(key, None)
            if self.preview.key in keys.values():
                self._update_preview()
            self._thumb_queue = rest + self._thumb_queue
            if self._thumb_queue:
                self._ask_thumbs()

        dev.request("files.thumbs", {"paths": list(keys)}, got)
        return False

    # -- selection and menu -----------------------------------------------------------
    def selected(self):
        bits = self.selection.get_selection()
        return [self.selection.get_item(bits.get_nth(i)) for i in range(bits.get_size())]

    def _update_actions(self):
        online = self.dev is not None and self.dev.online and self.path is not None
        sel = self.selected() if online else []
        writable = online and self.writable
        one = len(sel) == 1
        self.acts["open"].set_enabled(one)
        self.acts["play"].set_enabled(one and sel[0].playable)
        self.play_button.set_visible(one and sel[0].playable)
        self.acts["download"].set_enabled(bool(sel))
        self.acts["download-to"].set_enabled(bool(sel))
        self.acts["rename"].set_enabled(one and writable)
        self.acts["delete"].set_enabled(bool(sel) and writable)
        self.acts["new-folder"].set_enabled(writable)
        self.acts["upload"].set_enabled(writable)
        self.acts["upload-folder"].set_enabled(writable)
        self.acts["refresh"].set_enabled(online)
        self.acts["up"].set_enabled(online and bool(self.path) and bool(self.home)
                                   and self.path.rstrip("/") != self.home.rstrip("/"))
        self.acts["back"].set_enabled(online and bool(self._history))
        self.action_bar.set_revealed(bool(sel))
        if sel:
            self.selected_label.set_label(n_("%d selected", "%d selected", len(sel)))
        self._update_preview(sel)

    def _update_preview(self, sel=None):
        """The sidebar's thumbnail: one picture or video selected and its
        thumbnail there - otherwise nothing."""
        if sel is None:
            sel = self.selected() if self.dev is not None and self.dev.online else []
        item = sel[0] if len(sel) == 1 and sel[0].wants_thumb else None
        self.preview.key = item.thumb_key if item is not None else None
        texture = self._thumbs.get(item.thumb_key) if item is not None else None
        if texture is None:
            self.preview.set_visible(False)
            self.preview_picture.set_paintable(None)
            return
        self.preview_picture.set_paintable(texture)
        self.preview_name.set_label(item.name)
        self.preview.set_visible(True)

    def _popup(self, widget, list_item, x, y):
        pos = list_item.get_position()
        if pos == Gtk.INVALID_LIST_POSITION:
            return
        if not self.selection.is_selected(pos):
            self.selection.select_item(pos, True)
        menu = Gio.Menu()
        first = Gio.Menu()
        item = self.sorted.get_item(pos)
        if item.playable and len(self.selected()) == 1:
            first.append(_("Play"), "files.play")
        first.append(_("Open"), "files.open")
        first.append(_("Download"), "files.download")
        first.append(_("Download to …"), "files.download-to")
        menu.append_section(None, first)
        second = Gio.Menu()
        second.append(_("Rename …"), "files.rename")
        second.append(_("Delete …"), "files.delete")
        menu.append_section(None, second)
        popover = Gtk.PopoverMenu(menu_model=menu, has_arrow=False,
                                  halign=Gtk.Align.START)
        popover.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: GLib.idle_add(lambda: p.unparent() and False))
        popover.popup()

    # -- opening -------------------------------------------------------------------------
    def open_selected(self):
        sel = self.selected()
        if len(sel) == 1:
            self.open_item(sel[0])

    def activate_item(self, item):
        """A double click, Enter: into a folder, music played here, any
        other file opened on the PC."""
        if item is not None and item.playable:
            self.play_item(item)
        else:
            self.open_item(item)

    def open_item(self, item):
        if item is None:
            return
        if item.is_dir:
            self.navigate(item.path)
            return
        dev = self.dev

        def fetched(local):
            self._launch(local)
            self._watch(local, dev, item.path)

        self._fetch(item, fetched)

    def _fetch(self, item, then):
        """The file into the cache of opened ones; then(local) once it is there."""
        local = files.open_path(self.dev.id, item.path)
        os.makedirs(files.cache_dir(), mode=0o700, exist_ok=True)
        os.makedirs(os.path.dirname(local), mode=0o700, exist_ok=True)
        t = files.Transfer(self.dev, "download", item.path, local, size=item.size)
        self.run_transfer(t, lambda error: error is None and then(local))
        return local

    # -- music --------------------------------------------------------------------------
    def play_selected(self):
        sel = self.selected()
        if len(sel) == 1 and sel[0].playable:
            self.play_item(sel[0])

    def play_item(self, item):
        """Fetched like a file opened, then played in the bar below the list -
        on this PC, while the folders change."""
        def fetched(local):
            if self._play_want == local:
                self._play(local, item.name)

        self._play_want = self._fetch(item, fetched)

    def _play(self, local, name):
        old = self.player_controls.get_media_stream()
        if old is not None:
            old.pause()
        media = Gtk.MediaFile.new_for_filename(local)
        media.connect("notify::error", self._on_play_error, name)
        self.player_controls.set_media_stream(media)
        self.player_name.set_label(name)
        self.player_name.set_tooltip_text(name)
        self.player.set_reveal_child(True)
        self.acts["stop"].set_enabled(True)
        media.play()

    def _on_play_error(self, media, _pspec, name):
        if media is not self.player_controls.get_media_stream() or media.get_error() is None:
            return
        self.app.toast(_("Could not play %(name)s: %(error)s") % {
            "name": name, "error": media.get_error().message})
        self.stop_playing()

    def stop_playing(self):
        self._play_want = None
        media = self.player_controls.get_media_stream()
        if media is not None:
            media.pause()
            self.player_controls.set_media_stream(None)
        self.player.set_reveal_child(False)
        self.acts["stop"].set_enabled(False)

    def _launch(self, local):
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(local))

        def launched(source, res):
            try:
                source.launch_finish(res)
            except GLib.Error as e:
                self.app.toast(_("Could not open %(name)s: %(error)s") % {
                    "name": os.path.basename(local), "error": e.message})

        launcher.launch(self.get_root(), None, launched)

    def _watch(self, local, dev, remote):
        """A change saved in the opened copy: offered to go back to the phone."""
        old = self._watching.pop(local, None)
        if old is not None:
            old[0].cancel()
        try:
            monitor = Gio.File.new_for_path(local).monitor_file(Gio.FileMonitorFlags.NONE, None)
        except GLib.Error:
            return
        self._watching[local] = [monitor, dev, remote, os.path.getmtime(local), 0]
        monitor.connect("changed", self._on_local_changed, local)

    def _on_local_changed(self, monitor, f, other, event, local):
        if event not in (Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED,
                         Gio.FileMonitorEvent.MOVED_IN, Gio.FileMonitorEvent.RENAMED):
            return
        w = self._watching.get(local)
        if w is None or w[4]:
            return
        w[4] = GLib.timeout_add(800, self._offer_upload, local)

    def _offer_upload(self, local):
        w = self._watching.get(local)
        if w is None:
            return False
        w[4] = 0
        try:
            mtime = os.path.getmtime(local)
        except OSError:
            return False
        if mtime == w[3]:
            return False
        w[3] = mtime
        monitor, dev, remote, _m, _t = w
        toast = Adw.Toast(title=_("%s was changed") % os.path.basename(remote),
                          button_label=_("Save to phone"), timeout=20, use_markup=False)
        toast.connect("button-clicked", lambda *a: self.upload_paths(
            [local], dev=dev, folder=posixpath.dirname(remote), replace=True, refresh=True))
        if self.app.window is not None:
            self.app.window.toasts.add_toast(toast)
        return False

    # -- transfers ---------------------------------------------------------------------
    def run_transfer(self, transfer, done=None):
        row = TransferRow(transfer)
        self.transfers.append(row)
        self.transfer_revealer.set_reveal_child(True)

        def finished(t, error):
            self.transfers.remove(row)
            if self.transfers.get_row_at_index(0) is None:
                self.transfer_revealer.set_reveal_child(False)
            if error is not None and error != "cancelled":
                verb = (_("Download of %s failed: %s") if t.kind == "download"
                        else _("Upload of %s failed: %s"))
                self.app.toast(verb % (t.name, text.error(error)))
            if done is not None:
                done(error)

        transfer.connect("finished", finished)
        transfer.start()

    def download_selected(self, ask=False):
        items = self.selected()
        if not items:
            return
        if not ask:
            folder = (GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
                      or os.path.expanduser("~/Downloads"))
            self.download(items, folder)
            return
        dialog = Gtk.FileDialog(title=_("Download to"), modal=True)

        def chosen(d, res):
            try:
                folder = d.select_folder_finish(res)
            except GLib.Error:
                return
            if folder is not None and folder.get_path():
                self.download(items, folder.get_path())

        dialog.select_folder(self.get_root(), None, chosen)

    def download(self, items, folder):
        dev = self.dev
        left = {"n": len(items), "ok": []}

        def one_done(local):
            def done(error):
                left["n"] -= 1
                if error is None:
                    left["ok"].append(local)
                if left["n"] == 0 and left["ok"]:
                    self._downloaded(left["ok"], folder)
            return done

        for item in items:
            local = files.unique_path(os.path.join(folder, item.name))
            self.run_transfer(files.Transfer(dev, "download", item.path, local,
                                             is_dir=item.is_dir, size=item.size),
                              one_done(local))

    def _downloaded(self, paths, folder):
        if len(paths) == 1:
            title = _("Saved: %s") % os.path.basename(paths[0])
        else:
            title = n_("%d item saved", "%d items saved", len(paths))
        toast = Adw.Toast(title=title, button_label=_("Show"), use_markup=False)
        toast.connect("button-clicked", lambda *a: Gtk.FileLauncher.new(
            Gio.File.new_for_path(paths[0])).open_containing_folder(self.get_root(), None,
                                                                   None))
        if self.app.window is not None:
            self.app.window.toasts.add_toast(toast)

    def ask_upload(self, folder=False):
        dialog = Gtk.FileDialog(title=_("Upload folder") if folder else _("Upload files"),
                                modal=True)

        def chosen(d, res):
            try:
                if folder:
                    picked = [d.select_folder_finish(res)]
                else:
                    model = d.open_multiple_finish(res)
                    picked = [model.get_item(i) for i in range(model.get_n_items())]
            except GLib.Error:
                return
            self.upload_paths([f.get_path() for f in picked if f is not None and f.get_path()])

        if folder:
            dialog.select_folder(self.get_root(), None, chosen)
        else:
            dialog.open_multiple(self.get_root(), None, chosen)

    def _on_drop(self, target, value, x, y):
        if not self.acts["upload"].get_enabled():
            return False
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if paths:
            self.upload_paths(paths)
        return bool(paths)

    def upload_paths(self, paths, dev=None, folder=None, replace=False, refresh=True):
        """Uploads into `folder` (the one shown). Existing files: asked
        whether to replace them; an existing folder: the new one gets a
        name of its own."""
        dev = dev or self.dev
        folder = folder or self.path
        if dev is None or folder is None or not paths:
            return
        taken = set(self.names) if folder == self.path else set()
        plan, clashes = [], []
        for p in paths:
            name = os.path.basename(p.rstrip("/"))
            is_dir = os.path.isdir(p)
            if name in taken and is_dir:
                n = 2
                while "%s (%d)" % (name, n) in taken:
                    n += 1
                name = "%s (%d)" % (name, n)
            elif name in taken and not replace:
                clashes.append((p, name))
                continue
            taken.add(name)
            plan.append((p, name, is_dir))

        def go(items):
            if not items:
                return
            left = {"n": len(items)}

            def done(error):
                left["n"] -= 1
                if left["n"] == 0 and refresh and self.dev is dev and self.path == folder:
                    self.refresh()

            for p, name, is_dir in items:
                self.run_transfer(files.Transfer(
                    dev, "upload", posixpath.join(folder, name), p, is_dir=is_dir,
                    size=files.local_size(p) if is_dir else 0), done)

        if not clashes:
            go(plan)
            return
        dialog = Adw.AlertDialog(
            heading=n_("Replace %d file?", "Replace %d files?", len(clashes)),
            body="\n".join(name for _p, name in clashes[:8])
            + ("\n…" if len(clashes) > 8 else ""))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("skip", _("Skip"))
        dialog.add_response("replace", _("Replace"))
        dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def answered(d, response):
            if response == "replace":
                go(plan + [(p, name, False) for p, name in clashes])
            elif response == "skip":
                go(plan)

        dialog.connect("response", answered)
        dialog.present(self.get_root())

    # -- changing --------------------------------------------------------------------------
    def _ask_name(self, heading, initial, confirm, then):
        dialog = Adw.AlertDialog(heading=heading)
        entry = Gtk.Entry(text=initial, activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("ok", confirm)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")

        def check(*args):
            name = entry.get_text().strip()
            dialog.set_response_enabled("ok", bool(name) and "/" not in name
                                        and name not in (".", "..")
                                        and (name == initial or name not in self.names))

        entry.connect("changed", check)
        check()
        dialog.connect("response", lambda d, r: r == "ok" and then(entry.get_text().strip()))
        dialog.present(self.get_root())
        stem = os.path.splitext(initial)[0] if "." in initial[1:] else initial
        entry.grab_focus()
        entry.select_region(0, len(stem))
        return dialog

    def _changed(self, what):
        def done(result, error):
            if error is not None:
                self.app.toast(_("%(what)s failed: %(error)s") % {
                    "what": what, "error": text.error(error)})
            self.refresh()
        return done

    def ask_new_folder(self):
        if not self.acts["new-folder"].get_enabled():
            return
        folder = self.path
        self._ask_name(_("New folder"), "", _("Create"), lambda name: self.dev.request(
            "files.mkdir", {"path": folder, "name": name}, self._changed(_("New folder"))))

    def ask_rename(self):
        sel = self.selected()
        if len(sel) != 1 or not self.acts["rename"].get_enabled():
            return
        item = sel[0]
        self._ask_name(_("Rename"), item.name, _("Rename"), lambda name: name != item.name
                       and self.dev.request("files.rename", {"path": item.path, "name": name},
                                            self._changed(_("Rename"))))

    def ask_delete(self):
        sel = self.selected()
        if not sel or not self.acts["delete"].get_enabled():
            return
        heading = (_("Delete %s?") % sel[0].name if len(sel) == 1
                   else n_("Delete %d item?", "Delete %d items?", len(sel)))
        dialog = Adw.AlertDialog(
            heading=heading,
            body=_("It is deleted on the phone for good - folders with everything in them."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        paths = [i.path for i in sel]

        def deleted(result, error):
            if error is None and result.get("failed"):
                f = result["failed"][0]
                error = "%s: %s" % (posixpath.basename(f["path"]), f["error"])
            self._changed(_("Delete"))(result, error)

        dialog.connect("response", lambda d, r: r == "delete" and self.dev.request(
            "files.delete", {"paths": paths}, deleted))
        dialog.present(self.get_root())
