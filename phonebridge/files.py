# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Moving files between the PC and the phone.

Each transfer is an ssh of its own carrying the raw bytes - at the speed
of the network, and the agent's connection stays free for calls and
messages. Folders travel as a tar stream. An upload lands under a hidden
name first and takes its real name only when every byte arrived: a
broken or cancelled transfer never leaves half a file in place of the
old one. Downloads likewise (".part" until complete).

Files opened from the phone are downloaded to a cache of their own
(readable by you only) and opened from there; a change saved there can go
back to the phone."""

import hashlib
import os
import shlex
import shutil
import subprocess
import tarfile
import threading
import time

from gi.repository import GLib, GObject

from .connection import ssh_argv

CHUNK = 256 * 1024
OPEN_KEEP_DAYS = 3


def cache_dir():
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                        "phonebridge", "open")


def open_path(dev_id, remote):
    """Where a file opened from the phone is kept: one folder per file, so
    the name stays the phone's."""
    key = hashlib.sha1(("%s:%s" % (dev_id, remote)).encode()).hexdigest()[:16]
    return os.path.join(cache_dir(), key, os.path.basename(remote))


def clean_open_cache(days=OPEN_KEEP_DAYS):
    """Files opened more than `days` ago go."""
    root = cache_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return
    limit = time.time() - days * 86400
    for name in names:
        path = os.path.join(root, name)
        try:
            if os.path.getmtime(path) < limit:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def unique_path(path):
    """path, or "name (2).ext" ... when it is taken."""
    if not os.path.lexists(path):
        return path
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    if os.path.isdir(path):
        stem, ext = name, ""
    n = 2
    while True:
        candidate = os.path.join(folder, "%s (%d)%s" % (stem, n, ext))
        if not os.path.lexists(candidate):
            return candidate
        n += 1


def local_size(path):
    if not os.path.isdir(path):
        return os.path.getsize(path)
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


class _Counted:
    """A file object that counts what is read through it."""

    def __init__(self, f, count):
        self.f, self.count = f, count

    def read(self, n=-1):
        data = self.f.read(n)
        self.count(len(data))
        return data


class _CountedWriter:
    def __init__(self, f, count):
        self.f, self.count = f, count

    def write(self, data):
        self.f.write(data)
        self.count(len(data))
        return len(data)

    def flush(self):
        self.f.flush()


class Transfer(GObject.Object):
    """One download or upload. start() once; "progress" (done, total
    bytes), then "finished" with None, "cancelled" or what went wrong.

      download: remote file or folder -> local path (taken as given)
      upload:   local file or folder -> remote path"""

    __gsignals__ = {
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (float, float)),
        "finished": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, device, kind, remote, local, is_dir=False, size=0):
        super().__init__()
        self.info = dict(device.info)
        self.password = device.password
        self.kind = kind
        self.remote = remote
        self.local = local
        self.is_dir = is_dir
        self.total = size
        self.done = 0
        self.name = os.path.basename(remote if kind == "download" else local)
        self.proc = None
        self._cancelled = False
        self._last = 0.0
        self._stderr = []

    # -- what runs on the phone --------------------------------------------------
    def remote_command(self):
        q = shlex.quote
        folder, name = os.path.split(self.remote)
        if self.kind == "download":
            if self.is_dir:
                return "exec tar -C %s -cf - -- %s" % (q(folder or "/"), q(name))
            return "exec cat -- %s" % q(self.remote)
        part = os.path.join(folder, ".%s.phonebridge-part" % name)
        if self.is_dir:
            # into a hidden folder first; in place only when tar read it all
            return ("t=%s; mkdir -p -- \"$t\" && tar -xf - -C \"$t\" && mv -- \"$t\"/%s %s; "
                    "r=$?; rm -rf -- \"$t\"; exit $r" % (q(part), q(name), q(self.remote)))
        # all bytes there (the size says so), then the real name - else nothing
        return ("p=%s; f=%s; trap 'rm -f -- \"$p\"' HUP INT TERM; cat > \"$p\"; "
                "if [ \"$(wc -c < \"$p\")\" -eq %d ]; then mv -f -- \"$p\" \"$f\"; "
                "else rm -f -- \"$p\"; echo incomplete >&2; exit 1; fi"
                % (q(part), q(self.remote), self.total))

    # -- running -------------------------------------------------------------------
    def start(self):
        env = None
        if self.password is not None:
            from .secrets import ssh_env
            env = ssh_env(self.password)
        if self.kind == "upload" and not self.is_dir:
            try:
                self.total = os.path.getsize(self.local)
            except OSError as e:
                message = e.strerror or str(e)
                GLib.idle_add(lambda: self._finish(message) and False)
                return
        argv = ssh_argv(self.info, self.remote_command(), password=self.password is not None)
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=env)
        except OSError as e:
            message = str(e)
            GLib.idle_add(lambda: self._finish(message) and False)
            return
        threading.Thread(target=self._errors, daemon=True).start()
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self._cancelled = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()

    def _count(self, n):
        self.done += n
        now = time.monotonic()
        if now - self._last > 0.1:
            self._last = now
            done, total = self.done, self.total
            GLib.idle_add(lambda: self.emit("progress", done, total) and False)

    def _errors(self):
        for line in self.proc.stderr:
            line = line.decode("utf-8", "replace").strip()
            if line:
                self._stderr = (self._stderr + [line])[-5:]

    def _run(self):
        error = None
        try:
            if self.kind == "download":
                self.proc.stdin.close()
                self._download()
            else:
                self._upload()
        except (OSError, ValueError, tarfile.TarError) as e:
            error = getattr(e, "strerror", None) or str(e)
        code = self.proc.wait()
        if self._cancelled:
            error = "cancelled"
        elif code != 0 and error is None:
            error = self._stderr[-1] if self._stderr else "ssh: %d" % code
        elif error is not None and self._stderr:
            error = self._stderr[-1]
        if error is not None and self.kind == "download":
            self._remove_partial()
        GLib.idle_add(lambda: self._finish(error) and False)

    def _download(self):
        os.makedirs(os.path.dirname(self.local) or ".", exist_ok=True)
        if self.is_dir:
            tmp = self.local + ".part"
            os.makedirs(tmp, exist_ok=True)
            with tarfile.open(fileobj=_Counted(self.proc.stdout, self._count), mode="r|") as tar:
                tar.extractall(tmp, filter="data")
            self.proc.stdout.read()         # tar's padding after the end
            if self.proc.wait() != 0 or self._cancelled:
                return
            os.replace(os.path.join(tmp, os.path.basename(self.remote)), self.local)
            os.rmdir(tmp)
            return
        tmp = self.local + ".part"
        with open(tmp, "wb") as f:
            while True:
                data = self.proc.stdout.read1(CHUNK)
                if not data:
                    break
                f.write(data)
                self._count(len(data))
        if self.proc.wait() == 0 and not self._cancelled:
            os.replace(tmp, self.local)

    def _remove_partial(self):
        tmp = self.local + ".part"
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _upload(self):
        out = self.proc.stdin
        try:
            if self.is_dir:
                with tarfile.open(fileobj=_CountedWriter(out, self._count), mode="w|") as tar:
                    tar.add(self.local, arcname=os.path.basename(self.remote))
            else:
                with open(self.local, "rb") as f:
                    while not self._cancelled:
                        data = f.read(CHUNK)
                        if not data:
                            break
                        out.write(data)
                        self._count(len(data))
        except BrokenPipeError:
            pass                    # the phone side ended - its error tells why
        finally:
            try:
                out.close()
            except OSError:
                pass
        self.proc.stdout.read()

    def _finish(self, error):
        if error is None:
            self.done = max(self.done, self.total)
            self.emit("progress", self.done, self.total)
        self.emit("finished", error)
