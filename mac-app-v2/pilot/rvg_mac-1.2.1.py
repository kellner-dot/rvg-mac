#!/usr/bin/env python3
"""
RVG Mac agent v1.2.1 — PILOT TARGET (identical to v1.2.0 except the version string).

Purpose: the v2 pilot target. When the v2 agent (1.2.0) is installed,
it self-updates to this version; screenshots must still work with NO
re-grant. If they do, the loader pattern is proven. If not, v2 is dead
and v1 is restored. See FINGER-STEPS-v2.md step 5.

RVG Mac agent v1.2.1 — LOADER LAYOUT + SELF-UPDATE (v2 package, 2026-11-06).

Remote desktop agent for macOS (Apple Silicon native).

v2 LAYOUT (loader pattern): this script lives OUTSIDE the .app bundle,
at ~/rvd-mac/agent/rvg_mac.py, and the bundle launcher (RVGMac.c v2)
execs it from there. The bundle contains only the launcher + Info.plist,
so its seal never changes and the Screen Recording / Accessibility
grants survive script updates. This file is the agent; update it in
place (the agent self-updates it) — never put a copy back inside the
bundle. The previous version is kept at rvg_mac.py.prev.

SELF-UPDATE (mirrors the Razr's UpdateManager): on launch + once daily
the agent checks the Gitea release channel (kellner-dot/rvg-mac,
releases/latest, anonymous), compares the version (strictly greater),
downloads the new script to ~/rvd-mac/update/, verifies it (SHA-256
manifest + source-host allowlist + version strictly greater), then
atomically replaces this script and restarts (launchd KeepAlive
re-execs the launcher, which loads the new script). On any verification
failure the staged files are deleted, the reason is recorded in
/rvd/status, and the old version keeps running — the agent never
bricks itself. POST /rvd/update-check forces an immediate check.
Status fields (same names as the Razr): updateAvailable, updateVersion,
updateError, lastUpdateCheckMs, lastUpdateResult.

SAFETY: self-update refuses to run when this script is inside the
bundle (Resources/rvg_mac.py) — replacing a bundled script would break
the bundle signature. That layout is v1; v2 runs from outside it.

v1.1.0 changes (kept): honest screenshot errors, split status flags,
cliclick + osascript result-checking, unknown-key rejection.
See the v1 build record for the full v1.1.0 changelog.

Mirrors the Windows RVG agent API (port 8899, X-RVD-Token auth) so existing
tooling (fast.sh, viewers, watchdogs) works unchanged against the Mac.

Endpoints (all under /rvd/, all require X-RVD-Token except /rvd/view):
    GET  /rvd/status          -> agent/screen info JSON (incl. update fields)
    GET  /rvd/shot            -> screenshot PNG (params: scale, format)
    POST /rvd/input           -> mouse/keyboard {type: move|click|key|text, ...}
    POST /rvd/exec            -> {"command": "...", "timeout": 30}
                                -> {"stdout","stderr","exitCode"}
    GET  /rvd/download?path=  -> file download (octet-stream)
    POST /rvd/upload?filename=-> raw body saved to ~/rvd/inbox/ (500 MiB cap)
    POST /rvd/notify          -> {"title","message"} macOS notification
    POST /rvd/push            -> {"title","message"} ntfy.sh phone push
    POST /rvd/push-config     -> {"topic","server"} store ntfy config
    GET  /rvd/view            -> web viewer page (token gate in-page)
    POST /rvd/update-check    -> force an immediate self-update check
    POST /rvd/stop            -> stop the agent remotely

Install: staged install script in this fix package (v2: upgrade from the
v1 bundle). Runs as a user LaunchAgent in the console user's GUI session.

macOS permissions required (grant in System Settings > Privacy & Security,
finger steps — see FINGER-STEPS-v2.md):
    - Screen Recording  (for /rvd/shot)
    - Accessibility     (for /rvd/input)
"""

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "1.2.1"
PORT = int(os.environ.get("RVG_PORT", "8899"))
BIND_ADDR = os.environ.get("RVG_BIND", "0.0.0.0")
# Tailnet / private ranges allowed for non-loopback clients.
ALLOW_LIST = [s.strip() for s in
              os.environ.get("RVG_ALLOW", "100.64.0.0/10").split(",")
              if s.strip()]
UPLOAD_MAX_MB = int(os.environ.get("RVG_UPLOAD_MAX_MB", "500"))

CONSOLE_USER = "sethkellner"
# Shared layout: all Mac-agent state lives under ~/rvd-mac/ so every Kavi
# knows where to look. The token here is backed up to Drive as
# RVG-mac-token.txt (see SHARING.md).
RVD_DIR = f"/Users/{CONSOLE_USER}/rvd-mac"
INBOX_DIR = os.path.join(RVD_DIR, "inbox")
TOKEN_PATHS = [
    os.environ.get("RVG_TOKEN_FILE", ""),
    os.path.join(RVD_DIR, "token.txt"),
    # legacy fallback: pre-sharing token location
    f"/Users/{CONSOLE_USER}/rvd/token.txt",
    os.path.expanduser("~/rvd/token.txt"),
]
PUSH_CONFIG_PATH = os.path.join(RVD_DIR, "push-config.json")

# ------------------------------------------------------------ self-update
# v2: loader layout + self-update, mirroring the Razr's UpdateManager.
# Check (Gitea releases/latest, anonymous) -> version compare (strictly
# greater) -> download to staging -> verify (SHA-256 manifest + source
# host allowlist + version strictly greater) -> atomic replace of the
# external script -> restart via launchd KeepAlive. On any failure the
# staged files are deleted, the reason is recorded, and the old version
# keeps running. The agent NEVER replaces a script that lives inside
# the bundle (that would break the bundle signature) — self-update
# applies only when running from the external loader path.

UPDATE_REPO = "kellner-dot/rvg-mac"
UPDATE_API_BASE = "https://kavivpn.tail1cffd9.ts.net/api/v1"
UPDATE_ALLOWED_HOSTS = {"kavivpn.tail1cffd9.ts.net"}
UPDATE_CHECK_INTERVAL_S = 86400  # once daily, like the Razr
UPDATE_FIRST_CHECK_DELAY_S = 60  # let the agent start serving first
UPDATE_SCRIPT_NAME = "rvg_mac.py"
UPDATE_MANIFEST_NAME = "rvg_mac.py.sha256"
UPDATE_USER_AGENT = "KaviSelfUpdate/1.0"
UPDATE_STATE_FILE = os.path.join(RVD_DIR, "update-state.json")
UPDATE_STAGING_DIR = os.path.join(RVD_DIR, "update")
UPDATE_AGENT_DIR = os.path.join(RVD_DIR, "agent")
UPDATE_MAX_REDIRECTS = 5
UPDATE_CONNECT_TIMEOUT_S = 20
UPDATE_READ_TIMEOUT_S = 120

# Update state, persisted to UPDATE_STATE_FILE so it survives restarts.
# Same field names as the Razr's /rvd/status (UpdateManager prefs).
_UPDATE_STATE = {
    "updateAvailable": False,
    "updateVersion": None,
    "updateError": None,
    "lastUpdateCheckMs": 0,
    "lastUpdateResult": None,
    "failedVersion": None,  # version that failed verification (skip it
                            # until a newer version or a forced check)
}
_UPDATE_LOCK = threading.Lock()
# Serializes whole check cycles: the background thread (daily) and a
# manual POST /rvd/update-check must never run concurrently (two
# downloads / two applies racing would corrupt the staging dir and
# could double-apply). A concurrent caller gets the current state
# instead of starting a second cycle.
_UPDATE_CHECK_LOCK = threading.Lock()


def _update_load_state():
    try:
        with open(UPDATE_STATE_FILE) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k in _UPDATE_STATE:
                if k in saved:
                    _UPDATE_STATE[k] = saved[k]
    except Exception:
        pass


def _update_save_state():
    try:
        os.makedirs(RVD_DIR, exist_ok=True)
        tmp = UPDATE_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_UPDATE_STATE, f)
        os.replace(tmp, UPDATE_STATE_FILE)
    except Exception:
        pass


def _update_set(**kw):
    with _UPDATE_LOCK:
        _UPDATE_STATE.update(kw)
        _update_save_state()


def _update_now_ms():
    return int(time.time() * 1000)


def _compare_versions(a, b):
    """Numeric per-segment compare (Razr's compareVersions). Returns
    -1 if a<b, 0 if equal, 1 if a>b. '1.10.0' > '1.2.0' (not lexicographic)."""
    def parts(v):
        out = []
        for seg in str(v).strip().lstrip("v").split("."):
            m = re.match(r"(\d+)", seg)
            out.append(int(m.group(1)) if m else 0)
        return out
    pa, pb = parts(a), parts(b)
    for x, y in zip(pa, pb):
        if x != y:
            return -1 if x < y else 1
    if len(pa) != len(pb):
        # longer wins only if the extra segments are non-zero
        longer = pa if len(pa) > len(pb) else pb
        rest = longer[min(len(pa), len(pb)):]
        if any(rest):
            return 1 if len(pa) > len(pb) else -1
    return 0


def _agent_script_path():
    """This script's own path (the file the launcher exec'd)."""
    try:
        return os.path.realpath(__file__)
    except Exception:
        return ""


def _is_external_layout():
    """True only when running from OUTSIDE the bundle (the v2 loader
    layout). False when running from inside /Applications/RVG Mac.app
    (the v1 bundled layout) — self-update must not touch a bundled
    script or the bundle signature breaks."""
    p = _agent_script_path()
    return bool(p) and "RVG Mac.app" not in p


def _update_http_get(url, timeout=60):
    """GET with manual redirect-following; every hop's host is validated
    against UPDATE_ALLOWED_HOSTS before the body is read (Razr's
    UpdateManager download). Returns (status, body_bytes, final_url).
    Raises on network error, too many redirects, or a disallowed host."""
    import urllib.request
    import urllib.error
    current = url
    for _ in range(UPDATE_MAX_REDIRECTS + 1):
        req = urllib.request.Request(
            current, headers={"User-Agent": UPDATE_USER_AGENT}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), current
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location", "")
                if not loc:
                    raise RuntimeError("redirect without Location from %s" % current)
                nxt = urllib.parse.urljoin(current, loc)
                host = urllib.parse.urlparse(nxt).hostname or ""
                if host not in UPDATE_ALLOWED_HOSTS:
                    raise RuntimeError(
                        "redirect to disallowed host %s (allowlist: %s)"
                        % (host, sorted(UPDATE_ALLOWED_HOSTS)))
                current = nxt
                continue
            raise
    raise RuntimeError("too many redirects (max %d)" % UPDATE_MAX_REDIRECTS)


def _update_fetch_latest():
    """GET releases/latest; returns (version, assets) where assets is a
    list of {name, browser_download_url, size}. Raises on failure."""
    url = "%s/repos/%s/releases/latest" % (UPDATE_API_BASE, UPDATE_REPO)
    status, body, _ = _update_http_get(url, timeout=60)
    if status != 200:
        raise RuntimeError("releases/latest HTTP %d" % status)
    try:
        rel = json.loads(body.decode("utf-8"))
    except Exception as e:
        raise RuntimeError("releases/latest bad JSON: %s" % e)
    tag = str(rel.get("tag_name", "")).strip()
    if not tag:
        raise RuntimeError("releases/latest has no tag_name")
    version = tag.lstrip("v")
    if not re.match(r"^\d+(\.\d+)*$", version):
        raise RuntimeError("releases/latest tag %r is not a version" % tag)
    return version, rel.get("assets") or []


def _update_download_asset(url, dest):
    """Download one release asset to dest (manual redirects, host
    allowlist). Returns the byte count. Raises on failure."""
    status, body, _ = _update_http_get(url, timeout=UPDATE_READ_TIMEOUT_S)
    if status != 200:
        raise RuntimeError("asset download HTTP %d for %s" % (status, url))
    with open(dest, "wb") as f:
        f.write(body)
    return len(body)


def _update_manifest_sha(manifest_path):
    """Parse the .sha256 manifest ('<hex>  rvg_mac.py'); returns the hex
    for UPDATE_SCRIPT_NAME. Raises if missing/malformed."""
    try:
        with open(manifest_path) as f:
            text = f.read()
    except OSError as e:
        raise RuntimeError("cannot read manifest: %s" % e)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == UPDATE_SCRIPT_NAME:
            sha = parts[0].strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", sha):
                return sha
    raise RuntimeError("manifest has no valid SHA-256 for %s" % UPDATE_SCRIPT_NAME)


def _update_script_version(path):
    """Parse the VERSION = "..." line from a staged script. Raises if
    it cannot be found."""
    try:
        with open(path) as f:
            text = f.read(65536)
    except OSError as e:
        raise RuntimeError("cannot read staged script: %s" % e)
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise RuntimeError("staged script has no VERSION = ... line")
    return m.group(1)


def _update_syntax_ok(path):
    """py_compile the staged script; raises with the compiler error if
    it does not parse. A syntactically-broken script must never replace
    the live one (it would crash-loop the agent)."""
    import py_compile
    try:
        py_compile.compile(path, doraise=True)
    except Exception as e:
        raise RuntimeError("staged script fails py_compile: %s" % e)


def _update_check_once(force=False):
    """One full check -> download -> verify -> apply cycle. Never throws
    (all failures are recorded in state). Returns the state dict copy.
    force=True bypasses the failed-version skip (manual check).
    Serialized: if another cycle is already running, returns the
    current state immediately instead of starting a second cycle."""
    if not _UPDATE_CHECK_LOCK.acquire(blocking=False):
        _update_load_state()
        return dict(_UPDATE_STATE)
    try:
        return _update_check_inner(force=force)
    finally:
        _UPDATE_CHECK_LOCK.release()


def _update_check_inner(force=False):
    """The check cycle itself (called only via _update_check_once,
    which holds the check lock)."""
    _update_load_state()
    now_ms = _update_now_ms()
    _update_set(lastUpdateCheckMs=now_ms)
    try:
        if not _is_external_layout():
            raise RuntimeError(
                "agent runs from inside the bundle (%s); self-update "
                "requires the v2 loader layout (external script)"
                % _agent_script_path())
        version, assets = _update_fetch_latest()
        cmp = _compare_versions(version, VERSION)
        if cmp <= 0:
            _update_set(updateAvailable=False, updateVersion=None,
                        updateError=None,
                        lastUpdateResult="up to date (%s)" % VERSION)
            return dict(_UPDATE_STATE)
        if (not force and _UPDATE_STATE.get("failedVersion") == version):
            _update_set(updateAvailable=True, updateVersion=version,
                        updateError=None,
                        lastUpdateResult=(
                            "update %s previously failed verification; "
                            "waiting for a newer version or a forced check"
                            % version))
            return dict(_UPDATE_STATE)
        by_name = {a.get("name"): a for a in assets if a.get("name")}
        script_asset = by_name.get(UPDATE_SCRIPT_NAME)
        manifest_asset = by_name.get(UPDATE_MANIFEST_NAME)
        if not script_asset or not manifest_asset:
            raise RuntimeError(
                "release %s is missing %s and/or %s assets"
                % (version, UPDATE_SCRIPT_NAME, UPDATE_MANIFEST_NAME))
        os.makedirs(UPDATE_STAGING_DIR, exist_ok=True)
        staged_script = os.path.join(UPDATE_STAGING_DIR, UPDATE_SCRIPT_NAME)
        staged_manifest = os.path.join(UPDATE_STAGING_DIR, UPDATE_MANIFEST_NAME)
        for p in (staged_script, staged_manifest):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            _update_download_asset(
                script_asset["browser_download_url"], staged_script)
            _update_download_asset(
                manifest_asset["browser_download_url"], staged_manifest)
            expect_sha = _update_manifest_sha(staged_manifest)
            h = hashlib.sha256()
            with open(staged_script, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != expect_sha:
                raise RuntimeError(
                    "staged script SHA-256 mismatch (manifest %s, got %s)"
                    % (expect_sha[:12], h.hexdigest()[:12]))
            staged_version = _update_script_version(staged_script)
            if _compare_versions(staged_version, VERSION) <= 0:
                raise RuntimeError(
                    "staged script version %s is not newer than %s"
                    % (staged_version, VERSION))
            if staged_version != version:
                raise RuntimeError(
                    "staged script version %s != release tag %s"
                    % (staged_version, version))
            _update_syntax_ok(staged_script)
        except Exception:
            for p in (staged_script, staged_manifest):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            raise
        # Verified. Stage is good; apply it.
        _update_set(updateAvailable=True, updateVersion=version,
                    updateError=None,
                    lastUpdateResult="update %s verified, applying" % version)
        _update_apply(staged_script, version)
        return dict(_UPDATE_STATE)
    except Exception as e:
        msg = str(e)
        failed = None
        try:
            version, _ = _update_fetch_latest()
            failed = version
        except Exception:
            pass
        _update_set(updateAvailable=False,
                    updateError=msg,
                    lastUpdateResult="update check failed: %s" % msg,
                    failedVersion=failed)
        return dict(_UPDATE_STATE)


def _update_apply(staged_script, version):
    """Atomically replace the live external script and restart. The
    previous version is kept at rvg_mac.py.prev (manual rollback).
    Raises (before replacing) on any problem; after os.replace the
    restart is triggered and this process exits via launchd KeepAlive.
    Caller has already verified the staged script."""
    live = _agent_script_path()
    if not live or not _is_external_layout():
        raise RuntimeError("refusing to apply: not an external layout")
    prev = live + ".prev"
    tmp = live + ".tmp"
    try:
        shutil.copyfile(staged_script, tmp)
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        try:
            if os.path.isfile(live):
                shutil.copyfile(live, prev)
        except OSError:
            pass  # previous-version backup is best-effort, not fatal
        os.replace(tmp, live)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError("apply failed before replace: %s" % e)
    for p in (staged_script,
              os.path.join(UPDATE_STAGING_DIR, UPDATE_MANIFEST_NAME)):
        try:
            os.unlink(p)
        except OSError:
            pass
    _update_set(updateAvailable=False, updateVersion=None,
                updateError=None,
                lastUpdateResult="updated to %s; restarting" % version,
                failedVersion=None)
    # Restart: shut the server down; launchd KeepAlive re-execs the
    # launcher, which loads the new external script. Same mechanism as
    # POST /rvd/stop.
    threading.Thread(target=_delayed_shutdown, daemon=True).start()


def _update_loop():
    """Background thread: first check after a short delay, then once a
    day. Never throws; never crashes the agent."""
    time.sleep(UPDATE_FIRST_CHECK_DELAY_S)
    while True:
        try:
            _update_check_once(force=False)
        except Exception:
            pass
        time.sleep(UPDATE_CHECK_INTERVAL_S)

# ---------------------------------------------------------------- token

def _load_token():
    env_tok = os.environ.get("RVG_TOKEN", "").strip()
    if env_tok:
        return env_tok
    for p in TOKEN_PATHS:
        if p and os.path.isfile(p):
            try:
                with open(p) as f:
                    tok = f.read().strip()
                if tok:
                    return tok
            except OSError:
                pass
    return ""

TOKEN = _load_token()

# ------------------------------------------------------------ net helpers

def _is_loopback(ip):
    return ip in ("127.0.0.1", "::1") or ip.startswith("127.")

def _ip_in_cidr(ip, cidr):
    try:
        import ipaddress
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except Exception:
        return False

def _ip_allowed(ip):
    if _is_loopback(ip):
        return True
    return any(_ip_in_cidr(ip, c) for c in ALLOW_LIST)

# --------------------------------------------------------------- screen

def _screen_size():
    """Screen size in points via system_profiler-free AppleScript-free path."""
    try:
        out = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True, text=True, timeout=15).stdout
        m = re.search(r"Resolution:\s*(\d+)\s*x\s*(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 2560, 1600  # M1 MacBook Air native

class ScreenshotError(RuntimeError):
    """A capture that produced no usable image. _serve_shot converts this
    to the 500 JSON error path — a failed capture must never return
    HTTP 200 with 0 bytes (the v1.0.0 defect)."""


# Last capture outcome, for the /rvd/status screenshot flags. Tri-state:
# ok=None until the first capture attempt; then True/False from the most
# recent attempt. Never claims readiness that was not observed.
_LAST_SHOT = {"ok": None, "error": None, "at": None, "bytes": 0}


_GUI_SESSION = None


def _gui_session():
    """True if this process runs in a GUI (Aqua) login session, False if a
    background/daemon context, None if it cannot be determined. Best
    effort only; never raises. Cached after the first check."""
    global _GUI_SESSION
    if _GUI_SESSION is not None:
        return _GUI_SESSION
    try:
        p = subprocess.run(["launchctl", "managername"],
                           capture_output=True, text=True, timeout=10)
        _GUI_SESSION = (p.stdout.strip() == "Aqua")
    except Exception:
        _GUI_SESSION = None
    return _GUI_SESSION


def take_screenshot(scale=1.0, fmt="png"):
    """Capture with screencapture (no flash, no sound), optionally scaled.

    Fails loudly: the screencapture exit code and stderr are checked, the
    output file must exist, be non-empty, and start with the PNG/JPEG
    magic bytes, and the sips scaling step is checked too. Any failure
    raises ScreenshotError. The temp path is NOT pre-created: screencapture
    must create the file itself, so a failed capture cannot masquerade as
    an empty read of a pre-created file (the v1.0.0 defect).
    """
    fmt = (fmt or "png").lower()
    if fmt not in ("png", "jpg", "jpeg"):
        fmt = "png"
    magic = b"\x89PNG\r\n\x1a\n" if fmt == "png" else b"\xff\xd8\xff"
    ctype = "image/" + ("jpeg" if fmt in ("jpg", "jpeg") else "png")
    fd, tmp = tempfile.mkstemp(suffix="." + fmt)
    os.close(fd)
    try:
        os.unlink(tmp)  # screencapture creates it; see docstring
    except OSError:
        pass
    try:
        try:
            p = subprocess.run(["screencapture", "-x", "-t", fmt, tmp],
                               timeout=20, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            raise ScreenshotError("screencapture timed out after 20s")
        if p.returncode != 0:
            raise ScreenshotError("screencapture exit=%d: %s"
                                  % (p.returncode, p.stderr.strip()))
        if not os.path.isfile(tmp):
            raise ScreenshotError(
                "screencapture exited 0 but created no file "
                "(stdout=%r stderr=%r)" % (p.stdout, p.stderr))
        if scale and abs(float(scale) - 1.0) > 0.01 and shutil.which("sips"):
            w, h = _screen_size()
            nw, nh = max(1, int(w * float(scale))), max(1, int(h * float(scale)))
            try:
                sp = subprocess.run(["sips", "-z", str(nh), str(nw), tmp],
                                    timeout=20, capture_output=True, text=True)
            except subprocess.TimeoutExpired:
                raise ScreenshotError("sips timed out after 20s")
            if sp.returncode != 0:
                raise ScreenshotError("sips exit=%d: %s"
                                      % (sp.returncode, sp.stderr.strip()))
        with open(tmp, "rb") as f:
            data = f.read()
        if len(data) == 0:
            raise ScreenshotError(
                "capture produced 0 bytes (screencapture wrote an empty file)")
        if not data.startswith(magic):
            raise ScreenshotError(
                "capture bytes fail %s magic check (first 8 bytes: %s)"
                % (fmt, data[:8].hex()))
        _LAST_SHOT.update({"ok": True, "error": None,
                           "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                           "bytes": len(data)})
        return data, ctype
    except Exception as e:
        if not isinstance(e, ScreenshotError):
            # NOTE: `raise e`, not bare `raise`: bare raise would re-raise
            # the ORIGINAL exception and discard this conversion.
            e = ScreenshotError(str(e))
        _LAST_SHOT.update({"ok": False, "error": str(e),
                           "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                           "bytes": 0})
        raise e
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

# ---------------------------------------------------------------- input
# Primary: Quartz (PyObjC). Fallback: cliclick for mouse, osascript +
# System Events for key/text. Requires Accessibility permission for the
# agent process.
#
# "quartz" in /rvd/status reports ONLY whether the PyObjC Quartz import
# succeeded (the input backend). It says nothing about screenshots;
# screenshot readiness is reported separately (screenshotReady/lastShot).

_QUARTZ_IMPORT_ERROR = None
try:
    from Quartz import (
        CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap,
        kCGEventMouseMoved, kCGEventLeftMouseDown, kCGEventLeftMouseUp,
        CGEventCreateKeyboardEvent, CGEventKeyboardSetUnicodeString,
        CGEventSetIntegerValueField, kCGKeyboardEventKeycode,
    )
    HAS_QUARTZ = True
except Exception as _e:  # noqa: F841 — recorded below, not swallowed
    HAS_QUARTZ = False
    _QUARTZ_IMPORT_ERROR = "%s: %s" % (type(_e).__name__, _e)


def _cliclick_available():
    return bool(shutil.which("cliclick"))


def _input_backend():
    """What actually backs input right now. 'osascript-only' means mouse
    move/click are dead (no Quartz, no cliclick); key/text still go
    through osascript and are result-checked."""
    if HAS_QUARTZ:
        return "quartz"
    if _cliclick_available():
        return "cliclick"
    return "osascript-only"

# macOS virtual keycodes for the Windows-RVG key names
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
    "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
    "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35,
    "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41,
    "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "tab": 48, "space": 49, "`": 50, "backspace": 51, "delete": 51,
    "escape": 53,
    "f17": 64, "f18": 65, "f19": 66, "f20": 67,
    "f5": 96, "f6": 97, "f7": 98, "f3": 99, "f8": 100,
    "f9": 101, "f11": 103, "f13": 105, "f16": 106, "f14": 107,
    "f10": 109, "f12": 111, "f15": 113,
    "home": 115, "pageup": 116, "end": 119, "pagedown": 121,
    "f4": 118, "f2": 120, "f1": 122,
    "left": 123, "right": 124, "down": 125, "up": 126,
}

def _quartz_key(key, ctrl=False, alt=False, shift=False):
    name = str(key).lower()
    if name not in _KEYCODES:
        return False
    code = _KEYCODES[name]
    flags = 0
    if ctrl:
        flags |= 0x40000
    if alt:
        flags |= 0x80000
    if shift:
        flags |= 0x20002
    down = CGEventCreateKeyboardEvent(None, code, True)
    up = CGEventCreateKeyboardEvent(None, code, False)
    if flags:
        CGEventSetIntegerValueField(down, kCGKeyboardEventKeycode, code)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)
    return True

def _quartz_text(text):
    ev = CGEventCreateKeyboardEvent(None, 0, True)
    CGEventKeyboardSetUnicodeString(ev, len(text), text)
    CGEventPost(kCGHIDEventTap, ev)
    ev2 = CGEventCreateKeyboardEvent(None, 0, False)
    CGEventPost(kCGHIDEventTap, ev2)
    return True

def _quartz_move(x, y):
    ev = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (int(x), int(y)), 0)
    CGEventPost(kCGHIDEventTap, ev)
    return True

def _quartz_click(x=None, y=None):
    if x is not None and y is not None:
        _quartz_move(x, y)
        pos = (int(x), int(y))
    else:
        # click at current position: down+up at (0,0) is wrong; use NSEvent
        try:
            from Quartz import CGEventGetLocation
            from AppKit import NSEvent
            pos = tuple(NSEvent.mouseLocation())
            pos = (pos[0], _screen_size()[1] - pos[1])
        except Exception:
            pos = (100, 100)
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, pos, 0)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, pos, 0)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)
    return True

def _cliclick(args):
    """Run cliclick. Returns (ok, error): never claims success when the
    binary is missing or the command fails (the v1.0.0 defect)."""
    if not shutil.which("cliclick"):
        return False, "cliclick is not installed"
    try:
        p = subprocess.run(["cliclick"] + args, timeout=10,
                           capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return False, "cliclick timed out after 10s"
    if p.returncode != 0:
        return False, "cliclick exit=%d: %s" % (
            p.returncode, (p.stderr or p.stdout).strip())
    return True, ""


def _applescript_string(s):
    """Escape a string for embedding in an AppleScript double-quoted
    literal. v1.0.0 stripped double quotes and left backslashes raw."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _osascript_keystroke(text, timeout=15):
    """Type via System Events. Returns (ok, error): the osascript result
    is checked — v1.0.0 returned ok:true unconditionally (fabricated
    success when osascript failed, e.g. without an Accessibility
    grant)."""
    script = ('tell application "System Events" to keystroke "%s"'
              % _applescript_string(text))
    try:
        p = subprocess.run(["osascript", "-e", script], timeout=timeout,
                           capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return False, "osascript timed out after %ds" % timeout
    if p.returncode != 0:
        return False, "osascript exit=%d: %s" % (p.returncode, p.stderr.strip())
    return True, ""


def do_input(action):
    """Mirror the Windows /rvd/input shapes: move/click/key/text.

    Every path reports its real outcome: ok:false carries the reason
    (missing backend, failed subprocess, unknown key/type)."""
    t = (action.get("type") or "").lower()
    try:
        if t == "move":
            x, y = int(action["x"]), int(action["y"])
            if HAS_QUARTZ:
                return {"ok": bool(_quartz_move(x, y))}
            ok, err = _cliclick(["m:%d,%d" % (x, y)])
            return {"ok": True} if ok else {"ok": False, "error": err}
        if t == "click":
            x = action.get("x")
            y = action.get("y")
            if HAS_QUARTZ:
                return {"ok": bool(_quartz_click(x, y))}
            ok, err = _cliclick(["c:%d,%d" % (x, y)]
                                if x is not None else ["c:."])
            return {"ok": True} if ok else {"ok": False, "error": err}
        if t == "key":
            key = str(action.get("key", ""))
            if key.lower() not in _KEYCODES:
                # v1.0.0 fell through to typing the literal key name
                # ("f13" -> the characters f,1,3). Reject explicitly.
                return {"ok": False, "error": "unknown key: %s" % key}
            if HAS_QUARTZ:
                _quartz_key(key, action.get("ctrl"), action.get("alt"),
                            action.get("shift"))
                return {"ok": True}
            # osascript fallback for single keys; result-checked
            ok, err = _osascript_keystroke(key, timeout=10)
            return {"ok": True} if ok else {"ok": False, "error": err}
        if t == "text":
            text = str(action.get("text", ""))
            if HAS_QUARTZ:
                _quartz_text(text)
                return {"ok": True}
            ok, err = _osascript_keystroke(text, timeout=15)
            return {"ok": True} if ok else {"ok": False, "error": err}
        return {"ok": False, "error": "unknown input type: %s" % t}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ------------------------------------------------------------------ exec

def do_exec(command, timeout=30):
    """Run one shell command via zsh; a bad command can never crash the agent."""
    command = str(command)
    if len(command) > 8000:
        return {"stdout": "", "stderr": "command too long (8000 chars max)",
                "exitCode": 1}
    try:
        p = subprocess.run(["/bin/zsh", "-c", command],
                           capture_output=True, text=True,
                           timeout=int(timeout))
        return {"stdout": p.stdout, "stderr": p.stderr, "exitCode": p.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "command timed out", "exitCode": 124}
    except Exception as e:
        return {"stdout": "", "stderr": "exec failed: %s" % e, "exitCode": 1}

# ---------------------------------------------------------------- notify

def do_notify(title, message):
    title = str(title).replace('"', "")
    message = str(message).replace('"', "")
    try:
        p = subprocess.run(
            ["osascript", "-e",
             'display notification "%s" with title "%s"' % (message, title)],
            timeout=10, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "osascript timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if p.returncode != 0:
        # v1.0.0 returned ok:true unconditionally here too.
        return {"ok": False, "error": "osascript exit=%d: %s"
                % (p.returncode, p.stderr.strip())}
    return {"ok": True}

# ------------------------------------------------------------------ push
# ntfy.sh phone push, mirroring the Windows RVG /rvd/push API.

def _push_config():
    try:
        with open(PUSH_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def do_push(title, message):
    cfg = _push_config()
    topic = cfg.get("topic", "")
    server = cfg.get("server", "https://ntfy.sh").rstrip("/")
    if not topic:
        return {"ok": False, "error": "no push topic configured (POST /rvd/push-config)"}
    try:
        import urllib.request
        req = urllib.request.Request(
            "%s/%s" % (server, topic),
            data=str(message).encode("utf-8"),
            headers={"Title": str(title)[:60]})
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"ok": r.status in (200, 201)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------------------------------------------------------------- viewer

VIEWER_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RVG Mac</title>
<style>
body{background:#111;color:#eee;font-family:system-ui;margin:0;padding:12px}
#gate{position:fixed;inset:0;background:#111;display:flex;align-items:center;justify-content:center;z-index:10}
#gate div{background:#1c1c1c;padding:24px;border-radius:8px;text-align:center}
input{background:#2a2a2a;border:1px solid #444;color:#eee;padding:8px;border-radius:4px;width:220px}
button{background:#0a84ff;border:0;color:#fff;padding:8px 16px;border-radius:4px;margin:4px;cursor:pointer}
#bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
#shot{max-width:100%;border:1px solid #333;cursor:crosshair}
#status{color:#888;font-size:13px}
label{font-size:13px;color:#aaa}
</style></head><body>
<div id="gate"><div>
<h3>RVG Mac token</h3>
<p style="color:#888;font-size:13px">Same token as ~/rvd-mac/token.txt on the Mac (Drive backup: RVG-mac-token.txt)</p>
<input id="tok" type="password" placeholder="token"><br><br>
<button onclick="connect()">Connect</button>
</div></div>
<div id="bar">
<button onclick="toggleRefresh()" id="rfbtn">⏸ pause</button>
<button id="ctlbtn" onclick="toggleCtl()">🖱 click-to-control: OFF</button>
<input id="typebox" placeholder="type text + Enter" style="width:180px"
 onkeydown="if(event.key==='Enter'){sendText();}">
<button onclick="sendText()">Send</button>
<span id="status"></span>
</div>
<img id="shot" alt="screenshot">
<script>
let token="",refresh=true,ctl=false,timer=null;
const img=document.getElementById('shot'),st=document.getElementById('status');
function connect(){token=document.getElementById('tok').value;
 document.getElementById('tok').value='';
 document.getElementById('gate').style.display='none';poll();}
function api(p,o){return fetch('/rvd/'+p,{headers:{'X-RVD-Token':token},...o});}
async function poll(){
 try{
  const r=await api('shot?scale=0.5&format=png');
  if(r.status===401){document.getElementById('gate').style.display='flex';return;}
  if(r.ok){const b=await r.blob();img.src=URL.createObjectURL(b);
   st.textContent='shot '+new Date().toLocaleTimeString();}
 }catch(e){st.textContent='error: '+e;}
 if(refresh)timer=setTimeout(poll,1200);
}
function toggleRefresh(){refresh=!refresh;
 document.getElementById('rfbtn').textContent=refresh?'⏸ pause':'▶ resume';
 if(refresh)poll();else clearTimeout(timer);}
function toggleCtl(){ctl=!ctl;
 document.getElementById('ctlbtn').textContent='🖱 click-to-control: '+(ctl?'ON':'OFF');}
img.onclick=e=>{
 if(!ctl||!token)return;
 const r=img.getBoundingClientRect();
 fetch('/rvd/status',{headers:{'X-RVD-Token':token}}).then(x=>x.json()).then(s=>{
  const px=Math.round(e.clientX-r.left)*s.screenW/Math.round(r.width);
  const py=Math.round(e.clientY-r.top)*s.screenH/Math.round(r.height);
  api('input',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({type:'move',x:px,y:py})})
  .then(()=>api('input',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({type:'click'})}));
 });};
document.addEventListener('keydown',e=>{
 if(!ctl||!token||e.target.tagName==='INPUT')return;
 const k=e.key.toLowerCase();
 const special={enter:'enter',tab:'tab',escape:'escape',backspace:'backspace',
  delete:'delete',' ':'space',arrowleft:'left',arrowup:'up',arrowright:'right',
  arrowdown:'down',home:'home',end:'end',pageup:'pageup',pagedown:'pagedown'};
 let body;
 if(e.ctrlKey||e.metaKey||special[k]||k.length>1&&!special[k])
  body={type:'key',key:special[k]||k,ctrl:e.ctrlKey||e.metaKey,alt:e.altKey,shift:e.shiftKey};
 else body={type:'text',text:e.key};
 api('input',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body)});
 e.preventDefault();
});
function sendText(){const t=document.getElementById('typebox').value;if(!t)return;
 api('input',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({type:'text',text:t})});
 document.getElementById('typebox').value='';}
</script></body></html>"""

# ------------------------------------------------------------- HTTP layer

class Handler(BaseHTTPRequestHandler):
    server_version = "RVG-Mac/" + VERSION

    def _path(self):
        p = urlparse(self.path).path
        return p[len("/rvd"):] if p.startswith("/rvd") else p

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _client_ip(self):
        return self.client_address[0]

    def _auth_ok(self):
        got = self.headers.get("X-RVD-Token", "")
        return bool(got) and bool(TOKEN) and hmac.compare_digest(got, TOKEN)

    def _remote_gate(self, endpoint):
        ip = self._client_ip()
        if _is_loopback(ip):
            return None
        if not _ip_allowed(ip):
            return (403, {"ok": False, "error": "client IP not in RVG_ALLOW"})
        return None

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _deny(self):
        self._json({"ok": False, "error": "bad or missing token"}, 401)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        try:
            return json.loads(self.rfile.read(min(n, 2_000_000)).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _serve_shot(self):
        q = self._query()
        try:
            scale = float(q.get("scale", ["1"])[0])
        except ValueError:
            scale = 1.0
        fmt = q.get("format", ["png"])[0]
        try:
            data, ctype = take_screenshot(scale=scale, fmt=fmt)
        except Exception as e:
            return self._json({"ok": False, "error": "screenshot failed: %s" % e}, 500)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_download(self):
        q = self._query()
        path = q.get("path", [""])[0]
        if not path:
            return self._json({"ok": False, "error": "need ?path="}, 400)
        real = os.path.realpath(os.path.expanduser(path))
        if not os.path.isfile(real):
            return self._json({"ok": False, "error": "not found"}, 404)
        try:
            size = os.path.getsize(real)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % os.path.basename(real))
            self.end_headers()
            with open(real, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
        except Exception as e:
            # headers may already be sent; best effort
            try:
                self._json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass

    def _serve_upload(self):
        q = self._query()
        name = q.get("filename", [""])[0] or q.get("name", [""])[0]
        if not name:
            # also accept Content-Disposition filename
            cd = self.headers.get("Content-Disposition", "")
            m = re.search(r'filename="([^"]+)"', cd)
            name = m.group(1) if m else ""
        safe = os.path.basename(name).strip()
        if not safe or safe in (".", ".."):
            return self._json({"ok": False, "error": "need ?filename="}, 400)
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        cap = UPLOAD_MAX_MB * 1024 * 1024
        if n > cap:
            return self._json({"ok": False,
                               "error": "file too large (%d MiB cap)" % UPLOAD_MAX_MB}, 413)
        os.makedirs(INBOX_DIR, exist_ok=True)
        dest = os.path.join(INBOX_DIR, safe)
        # avoid collisions
        base, ext = os.path.splitext(dest)
        i = 1
        while os.path.exists(dest):
            dest = "%s-%d%s" % (base, i, ext)
            i += 1
        try:
            remaining = n
            with open(dest, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            return self._json({"ok": True, "path": dest, "size": n - remaining})
        except Exception as e:
            try:
                os.unlink(dest)
            except OSError:
                pass
            return self._json({"ok": False, "error": str(e)}, 500)

    # ---------------------------------------------------------- GET routes

    def do_GET(self):
        p = self._path()
        if p == "/view":
            gate = self._remote_gate("GET " + p)
            if gate:
                code, obj = gate
                return self._json(obj, code)
            html = VIEWER_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if p in ("", "/"):
            # no token at root -> 401, mirroring Windows agent
            return self._deny()
        if not self._auth_ok():
            return self._deny()
        gate = self._remote_gate("GET " + p)
        if gate:
            code, obj = gate
            return self._json(obj, code)
        try:
            if p == "/status":
                w, h = _screen_size()
                self._json({
                    "ok": True,
                    "version": VERSION,
                    "screenW": w,
                    "screenH": h,
                    "hostname": socket.gethostname(),
                    "platform": "darwin",
                    "arch": "arm64",
                    "user": CONSOLE_USER,
                    # Input backend ONLY. "quartz" says nothing about
                    # screenshots (v1.0.0 conflated the two in
                    # diagnosis; the fields below keep them apart).
                    "quartz": HAS_QUARTZ,
                    "quartzImportError": _QUARTZ_IMPORT_ERROR,
                    "inputBackend": _input_backend(),
                    "mouseReady": bool(HAS_QUARTZ or _cliclick_available()),
                    "cliclick": _cliclick_available(),
                    # Capture readiness: tri-state, observed only.
                    # null until the first capture attempt; then the
                    # most recent attempt's outcome.
                    "screenshotReady": _LAST_SHOT["ok"],
                    "lastShot": dict(_LAST_SHOT),
                    # Launch context: True = Aqua GUI session (screenshots
                    # possible); False = daemon/background (no display).
                    "guiSession": _gui_session(),
                    "bind": BIND_ADDR,
                    # v2 loader layout + self-update (same field names as
                    # the Razr's /rvd/status). loaderLayout True = the
                    # script runs from outside the bundle (self-update
                    # applies); False = bundled script (v1 layout,
                    # self-update refuses). agentPath is the live script.
                    "loaderLayout": _is_external_layout(),
                    "agentPath": _agent_script_path(),
                    "updateAvailable": _UPDATE_STATE["updateAvailable"],
                    "updateVersion": _UPDATE_STATE["updateVersion"],
                    "updateError": _UPDATE_STATE["updateError"],
                    "lastUpdateCheckMs": _UPDATE_STATE["lastUpdateCheckMs"],
                    "lastUpdateResult": _UPDATE_STATE["lastUpdateResult"],
                })
            elif p == "/shot":
                self._serve_shot()
            elif p == "/download":
                self._serve_download()
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    # --------------------------------------------------------- POST routes

    def do_POST(self):
        if not self._auth_ok():
            return self._deny()
        p = self._path()
        gate = self._remote_gate("POST " + p)
        if gate:
            code, obj = gate
            return self._json(obj, code)
        try:
            if p == "/input":
                body = self._read_json()
                if body is None:
                    return self._json({"ok": False, "error": "bad JSON"}, 400)
                self._json(do_input(body))
            elif p == "/exec":
                body = self._read_json()
                if body is None or "command" not in body:
                    return self._json({"ok": False,
                                       "error": 'need {"command": ...}'}, 400)
                self._json(do_exec(body["command"], body.get("timeout", 30)))
            elif p == "/upload":
                self._serve_upload()
            elif p == "/notify":
                body = self._read_json() or {}
                self._json(do_notify(body.get("title", "RVG"),
                                     body.get("message", "")))
            elif p == "/push":
                body = self._read_json() or {}
                self._json(do_push(body.get("title", "RVG"),
                                   body.get("message", "")))
            elif p == "/push-config":
                body = self._read_json() or {}
                os.makedirs(RVD_DIR, exist_ok=True)
                cfg = {"topic": str(body.get("topic", "")),
                       "server": str(body.get("server", "https://ntfy.sh"))}
                with open(PUSH_CONFIG_PATH, "w") as f:
                    json.dump(cfg, f)
                os.chmod(PUSH_CONFIG_PATH, 0o600)
                self._json({"ok": True})
            elif p == "/stop":
                self._json({"ok": True, "stopping": True})
                threading.Thread(target=_delayed_shutdown, daemon=True).start()
            elif p == "/update-check":
                # Force an immediate self-update check (bypasses the
                # failed-version skip and the daily interval). Runs the
                # full check -> verify -> apply cycle synchronously and
                # returns the update state. If an update applies, the
                # agent restarts right after this response is sent.
                state = _update_check_once(force=True)
                self._json({"ok": True, "update": state})
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def log_message(self, fmt, *args):  # quiet console
        pass


_server = None

def _delayed_shutdown():
    time.sleep(1)
    if _server:
        _server.shutdown()


def main():
    global _server
    if not TOKEN:
        print("[FATAL] no token: set RVG_TOKEN or create %s" %
              os.path.join(RVD_DIR, "token.txt"), file=sys.stderr)
        sys.exit(1)
    srv = None
    for _ in range(20):
        try:
            srv = ThreadingHTTPServer((BIND_ADDR, PORT), Handler)
            break
        except OSError:
            time.sleep(1)
    if srv is None:
        print("[FATAL] %s:%d still busy after 20s" % (BIND_ADDR, PORT),
              file=sys.stderr)
        sys.exit(1)
    _server = srv
    scope = "loopback only" if BIND_ADDR == "127.0.0.1" else "NON-LOCAL"
    print("=" * 52)
    print("  RVG Mac agent v%s — remote desktop, token auth" % VERSION)
    print("=" * 52)
    print("[PASS] listening on %s:%d (%s)" % (BIND_ADDR, PORT, scope))
    print("[PASS] token loaded (%d chars)" % len(TOKEN))
    print("[%s] Quartz input backend" % ("PASS" if HAS_QUARTZ else "WARN no"))
    if not HAS_QUARTZ and _QUARTZ_IMPORT_ERROR:
        print("[WARN] Quartz import failed: %s" % _QUARTZ_IMPORT_ERROR)
    print("[%s] input backend: %s (cliclick: %s)"
          % ("PASS" if _input_backend() != "osascript-only" else "WARN",
             _input_backend(), "yes" if _cliclick_available() else "no"))
    _gs = _gui_session()
    print("[%s] GUI session: %s"
          % ("PASS" if _gs else ("WARN" if _gs is False else "WARN unknown"),
             "Aqua" if _gs else ("non-GUI" if _gs is False else "unknown")))
    _update_load_state()
    print("[%s] loader layout: %s (script: %s)"
          % ("PASS" if _is_external_layout() else "WARN",
             "external" if _is_external_layout() else "bundled (self-update disabled)",
             _agent_script_path()))
    threading.Thread(target=_update_loop, daemon=True).start()
    print("[PASS] self-update checker started (daily + on launch; POST /rvd/update-check forces one)")
    if BIND_ADDR != "127.0.0.1":
        print("[WARN] non-loopback bind — allowlist: %s" % ",".join(ALLOW_LIST))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nRVG Mac agent stopped.")


if __name__ == "__main__":
    main()
