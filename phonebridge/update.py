# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Updates from GitHub: which commit is installed, whether main has moved
on since, and installing the new one - with its own install.sh, as the
first time.

Only an installed PhoneBridge looks for updates: install.sh writes the
commit to VERSION next to the package. Run from the source tree, there is
no VERSION and nothing is asked."""

import json
import os
import re
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request

REPO = "misc-de/PhoneBridge"
API = os.environ.get("PHONEBRIDGE_UPDATE_API") or "https://api.github.com/repos/" + REPO
TARBALL = (os.environ.get("PHONEBRIDGE_UPDATE_TARBALL")
           or "https://github.com/" + REPO + "/archive/{sha}.tar.gz")
LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(LIB, "VERSION")
SHA = re.compile(r"[0-9a-f]{40}")
SHOWN = 8           # changes listed when asking


def installed():
    """The installed commit, "unknown" when install.sh could not tell it,
    or None when this is not an installed PhoneBridge."""
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            v = f.read().strip()
    except OSError:
        return None
    return v if SHA.fullmatch(v) else "unknown"


def launcher():
    """bin/phonebridge of this installation (<prefix>/lib/phonebridge ->
    <prefix>/bin/phonebridge)."""
    return os.path.join(os.path.dirname(os.path.dirname(LIB)), "bin", "phonebridge")


def _open(url, timeout):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "PhoneBridge"})
    return urllib.request.urlopen(req, timeout=timeout)


def _get_json(url):
    with _open(url, 20) as r:
        return json.load(r)


def check(current):
    """{"sha", "count", "changes"} when main has moved on from `current`,
    else None. count is None when GitHub does not know `current` (any
    more); changes are first lines, newest first. Raises OSError or
    ValueError when GitHub cannot be asked."""
    head = _get_json(API + "/commits/main").get("sha", "")
    if not SHA.fullmatch(head):
        raise ValueError("no commit")
    if head == current:
        return None
    commits, count = [], None
    if SHA.fullmatch(current or ""):
        try:
            cmp = _get_json("%s/compare/%s...%s" % (API, current, head))
        except urllib.error.HTTPError as e:     # a commit GitHub does not have
            if e.code not in (404, 422):
                raise
        else:
            if cmp.get("status") in ("identical", "behind"):   # installed is newer
                return None
            commits = cmp.get("commits") or []
            count = cmp.get("ahead_by", len(commits))
    changes = []
    for c in reversed(commits):
        line = ((c.get("commit") or {}).get("message") or "").strip().splitlines()
        if line:
            changes.append(line[0])
    return {"sha": head, "count": count, "changes": changes}


def install(sha, autostart):
    """Fetches that commit and runs its install.sh, which leaves the
    running app alone (the caller restarts). None, or what went wrong."""
    if not SHA.fullmatch(sha or ""):
        return "no valid version"
    with tempfile.TemporaryDirectory(prefix="phonebridge-update-") as tmp:
        try:
            with _open(TARBALL.format(sha=sha), 120) as r, \
                    tarfile.open(fileobj=r, mode="r|gz") as tar:
                tar.extractall(tmp, filter="data")
        except (OSError, tarfile.TarError) as e:
            return str(e)
        tops = os.listdir(tmp)
        top = os.path.join(tmp, tops[0]) if len(tops) == 1 else tmp
        script = os.path.join(top, "install.sh")
        if not os.path.isfile(script):
            return "install.sh missing"
        env = dict(os.environ, PHONEBRIDGE_VERSION=sha, PHONEBRIDGE_NO_RESTART="1")
        env.pop("NO_AUTOSTART", None)
        if not autostart:
            env["NO_AUTOSTART"] = "1"
        try:
            p = subprocess.run(["bash", script], cwd=top, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as e:
            return str(e)
        if p.returncode != 0:
            lines = (p.stdout + p.stderr).strip().splitlines()
            return lines[-1] if lines else "install.sh: %d" % p.returncode
    return None


def restart_after(pid, args):
    """Starts this installation's launcher once process `pid` is gone."""
    subprocess.Popen(
        ["sh", "-c", 'while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; shift; exec "$@"',
         "sh", str(pid), launcher()] + list(args),
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
