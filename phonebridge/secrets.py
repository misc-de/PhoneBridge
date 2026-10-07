# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""SSH passwords for phones that do not take the key, kept in the desktop's
keyring (Secret Service: GNOME Keyring, KWallet ...) - never in a file of
PhoneBridge's.

ssh gets the password through SSH_ASKPASS: a tiny helper prints it from
the environment of that one ssh process (readable by the user only)."""

import os
import tempfile

import gi
from gi.repository import GLib

try:                                # optional (deps.py): without it, no keyring
    gi.require_version("Secret", "1")
    from gi.repository import Secret
    SCHEMA = Secret.Schema.new(
        "io.github.miscde.PhoneBridge.ssh", Secret.SchemaFlags.NONE,
        {"host": Secret.SchemaAttributeType.STRING,
         "user": Secret.SchemaAttributeType.STRING,
         "port": Secret.SchemaAttributeType.STRING})
except (ImportError, ValueError):
    Secret = SCHEMA = None

ASKPASS = """#!/bin/sh
# PhoneBridge: hands ssh the password of this one connection
printf '%s\\n' "$PHONEBRIDGE_SSH_PASSWORD"
"""


# tests: a keyring in memory (run-tests.sh sets it) - never the user's
_MEMORY = {} if os.environ.get("PHONEBRIDGE_KEYRING") == "memory" else None


def attributes(info):
    return {"host": info["host"], "user": info["user"],
            "port": str(info.get("port") or 22)}


def label(info):
    return "PhoneBridge: %s@%s" % (info["user"], info["host"])


def _key(info):
    return tuple(sorted(attributes(info).items()))


def lookup(info, callback):
    """callback(password or None)."""
    if _MEMORY is not None:
        GLib.idle_add(lambda: callback(_MEMORY.get(_key(info))) and False)
        return
    if Secret is None:
        GLib.idle_add(lambda: callback(None) and False)
        return

    def done(source, res):
        try:
            callback(Secret.password_lookup_finish(res))
        except GLib.Error:
            callback(None)          # no keyring, locked and not opened ...

    try:
        Secret.password_lookup(SCHEMA, attributes(info), None, done)
    except GLib.Error:
        GLib.idle_add(lambda: callback(None) and False)


def store(info, password, callback=None):
    """callback(error or None)."""
    if _MEMORY is not None:
        _MEMORY[_key(info)] = password
        if callback:
            GLib.idle_add(lambda: callback(None) and False)
        return
    if Secret is None:
        if callback:
            GLib.idle_add(lambda: callback("libsecret is not installed") and False)
        return

    def done(source, res):
        try:
            Secret.password_store_finish(res)
            error = None
        except GLib.Error as e:
            error = e.message
        if callback:
            callback(error)

    Secret.password_store(SCHEMA, attributes(info), Secret.COLLECTION_DEFAULT,
                          label(info), password, None, done)


def clear(info, callback=None):
    if _MEMORY is not None or Secret is None:
        if _MEMORY is not None:
            _MEMORY.pop(_key(info), None)
        if callback:
            GLib.idle_add(lambda: callback() and False)
        return

    def done(source, res):
        try:
            Secret.password_clear_finish(res)
        except GLib.Error:
            pass
        if callback:
            callback()

    Secret.password_clear(SCHEMA, attributes(info), None, done)


def askpass_helper():
    """The SSH_ASKPASS program - written once into the runtime directory."""
    d = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    path = os.path.join(d, "phonebridge-askpass")
    try:
        with open(path) as f:
            if f.read() == ASKPASS:
                return path
    except OSError:
        pass
    tmp = "%s.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w") as f:
        f.write(ASKPASS)
    os.replace(tmp, path)
    return path


def ssh_env(password):
    """The environment for an ssh that logs in with this password."""
    return dict(os.environ, SSH_ASKPASS=askpass_helper(), SSH_ASKPASS_REQUIRE="force",
                PHONEBRIDGE_SSH_PASSWORD=password)
