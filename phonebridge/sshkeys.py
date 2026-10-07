# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The PC's own SSH key - found, or made when the user agrees - so the
phone can be told to take it (agent: ssh.authorize, like ssh-copy-id)."""

import os
import socket
import subprocess

SSH_DIR = os.environ.get("PHONEBRIDGE_SSH_DIR") or os.path.expanduser("~/.ssh")
# what ssh tries by itself, best first
NAMES = ("id_ed25519", "id_ecdsa", "id_ed25519_sk", "id_rsa")


def public_key_path():
    for name in NAMES:
        path = os.path.join(SSH_DIR, name + ".pub")
        if os.path.exists(path) and os.path.exists(path[:-4]):
            return path
    return None


def public_key():
    path = public_key_path()
    if path is None:
        return None
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def make_key():
    """A new ed25519 key without a passphrase in ~/.ssh/id_ed25519 - only
    where there is none; -> the public key, or raises OSError."""
    path = os.path.join(SSH_DIR, "id_ed25519")
    if os.path.exists(path):
        raise OSError("%s exists already" % path)
    os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    comment = "phonebridge@%s" % socket.gethostname()
    r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment,
                        "-f", path], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or "ssh-keygen failed")
    return public_key()
