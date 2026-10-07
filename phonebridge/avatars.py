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


def _texture(raw):
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
    except GLib.Error:
        return None


class Avatars:
    def __init__(self):
        self.textures = {}
        self._waiting = {}

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
                self.textures[key] = texture
                callback(texture)
                return
        if key in self._waiting:
            self._waiting[key].append(callback)
            return
        self._waiting[key] = [callback]

        def done(result, error):
            callbacks = self._waiting.pop(key, [])
            texture = None
            if error is None:
                raw = base64.b64decode(result["data"])
                texture = _texture(raw)
                if texture is not None:
                    try:
                        os.makedirs(CACHE_DIR, exist_ok=True)
                        tmp = "%s.%d.part" % (path, os.getpid())
                        with open(tmp, "wb") as f:
                            f.write(raw)
                        os.replace(tmp, path)
                    except OSError:
                        pass
            if texture is None and error != "not connected":
                self.textures[key] = None       # not a picture GTK can show
            elif texture is not None:
                self.textures[key] = texture
                for cb in callbacks:
                    cb(texture)

        dev.request("avatar", {"key": key}, done)
