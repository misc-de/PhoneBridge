# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Profile pictures from the phone (contacts, chatty), cached on the PC.

sms.threads names a picture by a key that changes with the picture; the
picture itself is fetched once with the "avatar" command and kept in
~/.cache/phonebridge/avatars/<key>, so it costs nothing the next time."""

import base64
import os
import re

from gi.repository import Gdk, GLib

CACHE_DIR = os.environ.get("PHONEBRIDGE_CACHE") or os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "phonebridge", "avatars")


def private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def write_private(path, data):
    """Whole, then in place; readable by the user only."""
    tmp = "%s.%d.part" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


LIMIT = 300         # pictures kept in memory


def _texture(raw):
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
    except GLib.Error:
        return None


class Avatars:
    def __init__(self):
        self.textures = {}          # key -> texture, or None: not a usable picture
        self._waiting = {}

    def _keep(self, key, texture):
        self.textures.pop(key, None)
        self.textures[key] = texture
        while len(self.textures) > LIMIT:       # the oldest go first
            self.textures.pop(next(iter(self.textures)))

    def get(self, dev, key, callback):
        """callback(texture) - right away when known, later when fetched;
        never when there is no usable picture."""
        if not key or not re.fullmatch(r"[0-9a-f]{8,64}", key):
            return
        if key in self.textures:
            if self.textures[key] is not None:
                callback(self.textures[key])
            return
        path = os.path.join(CACHE_DIR, key)
        if os.path.exists(path):
            with open(path, "rb") as f:
                texture = _texture(f.read())
            if texture is not None:
                self._keep(key, texture)
                callback(texture)
                return
        if key in self._waiting:
            self._waiting[key].append(callback)
            return
        self._waiting[key] = [callback]

        def done(result, error):
            callbacks = self._waiting.pop(key, [])
            if error is not None:
                return      # not connected, timeout ... - asked again next time
            try:
                raw = base64.b64decode(result["data"])
            except (KeyError, TypeError, ValueError):
                raw = b""
            texture = _texture(raw) if raw else None
            self._keep(key, texture)    # None: not a picture GTK can show
            if texture is None:
                return
            try:
                private_dir(CACHE_DIR)
                write_private(path, raw)
            except OSError:
                pass
            for cb in callbacks:
                cb(texture)

        dev.request("avatar", {"key": key}, done)
