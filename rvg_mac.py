#!/usr/bin/env python3
"""
RVG Mac agent v1.0.0 — remote desktop agent for macOS (Apple Silicon native).

Mirrors the Windows RVG agent API (port 8899, X-RVD-Token auth) so existing
tooling (fast.sh, viewers, watchdogs) works unchanged against the Mac.

Endpoints (all under /rvd/, all require X-RVD-Token except /rvd/view):
    GET  /rvd/status          -> agent/screen info JSON
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
    POST /rvd/stop            -> stop the agent remotely

Install: sudo bash install.sh
Runs as a LaunchDaemon (root), starts on boot.

macOS permissions required (grant once in System Settings > Privacy & Security):
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

VERSION = "1.0.0"
PORT = int(os.environ.get("RVG_PORT", "8899"))
BIND_ADDR = os.environ.get("RVG_BIND", "0.0.0.0")
# Tailnet / private ranges allowed for non-loopback clients.
ALLOW_LIST = [s.strip() for s in
              os.environ.get("RVG_ALLOW", "100.64.0.0/10").split(",")
              if s.strip()]
UPLOAD_MAX_MB = int(os.environ.get("RVG_UPLOAD_MAX_MB", "500"))
# Download allowlist: GET /rvd/download?path= may only serve files under
# these roots. Extra roots via RVG_DOWNLOAD_ALLOW (comma-separated).
# Sensitive names/paths (tokens, SSH keys, system dirs) are blocked
# explicitly even under an allowlisted root. Everything else -> 403.
DOWNLOAD_ALLOW_EXTRA = [s.strip() for s in
                        os.environ.get("RVG_DOWNLOAD_ALLOW", "").split(",")
                        if s.strip()]

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

def _download_roots():
    """Canonical allowlist roots for /rvd/download."""
    roots = [os.path.realpath(RVD_DIR),
             os.path.realpath(os.path.expanduser("~/Downloads"))]
    for extra in DOWNLOAD_ALLOW_EXTRA:
        roots.append(os.path.realpath(os.path.expanduser(extra)))
    return roots

# Basenames never served, and path parts never served, even under an
# allowlisted root (defense in depth if RVG_DOWNLOAD_ALLOW is widened).
_DOWNLOAD_BLOCKED_NAMES = ("token.txt",)
_DOWNLOAD_BLOCKED_PARTS = (".ssh", ".gnupg", "Library/Keychains")

def _download_allowed(real):
    """True if the realpath'd file may be served by /rvd/download."""
    base = os.path.basename(real).lower()
    if base in _DOWNLOAD_BLOCKED_NAMES:
        return False
    low = real.lower()
    for part in _DOWNLOAD_BLOCKED_PARTS:
        pl = part.lower()
        if ("/" + pl + "/") in low or low.endswith("/" + pl):
            return False
    if real == "/etc" or real.startswith("/etc/"):
        return False
    return any(real == r or real.startswith(r + os.sep)
               for r in _download_roots())

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

def take_screenshot(scale=1.0, fmt="png"):
    """Capture with screencapture (no flash, no sound), optionally scaled."""
    fmt = (fmt or "png").lower()
    if fmt not in ("png", "jpg", "jpeg"):
        fmt = "png"
    with tempfile.NamedTemporaryFile(suffix="." + fmt, delete=False) as tf:
        tmp = tf.name
    try:
        subprocess.run(["screencapture", "-x", "-t", fmt, tmp],
                       timeout=20, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if scale and abs(float(scale) - 1.0) > 0.01 and shutil.which("sips"):
            w, h = _screen_size()
            nw, nh = max(1, int(w * float(scale))), max(1, int(h * float(scale)))
            subprocess.run(["sips", "-z", str(nh), str(nw), tmp],
                           timeout=20, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        with open(tmp, "rb") as f:
            return f.read(), "image/" + ("jpeg" if fmt in ("jpg", "jpeg") else "png")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

# ---------------------------------------------------------------- input
# Primary: Quartz (PyObjC ships with macOS system python3). Fallback: cliclick.
# Requires Accessibility permission for the agent process.

try:
    from Quartz import (
        CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap,
        kCGEventMouseMoved, kCGEventLeftMouseDown, kCGEventLeftMouseUp,
        CGEventCreateKeyboardEvent, CGEventKeyboardSetUnicodeString,
        CGEventSetIntegerValueField, kCGKeyboardEventKeycode,
    )
    HAS_QUARTZ = True
except Exception:
    HAS_QUARTZ = False

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
    if not shutil.which("cliclick"):
        return False
    subprocess.run(["cliclick"] + args, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True

def do_input(action):
    """Mirror the Windows /rvd/input shapes: move/click/key/text."""
    t = (action.get("type") or "").lower()
    try:
        if t == "move":
            x, y = int(action["x"]), int(action["y"])
            ok = _quartz_move(x, y) if HAS_QUARTZ else _cliclick(["m:%d,%d" % (x, y)])
            return {"ok": bool(ok)}
        if t == "click":
            x = action.get("x")
            y = action.get("y")
            if HAS_QUARTZ:
                ok = _quartz_click(x, y)
            else:
                ok = _cliclick(["c:%d,%d" % (x, y)] if x is not None else ["c:."])
            return {"ok": bool(ok)}
        if t == "key":
            key = action.get("key", "")
            if HAS_QUARTZ and _quartz_key(key, action.get("ctrl"), action.get("alt"),
                                          action.get("shift")):
                return {"ok": True}
            # osascript fallback for single keys
            script = 'tell application "System Events" to keystroke "%s"' % key.replace('"', "")
            subprocess.run(["osascript", "-e", script], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True}
        if t == "text":
            text = str(action.get("text", ""))
            if HAS_QUARTZ and _quartz_text(text):
                return {"ok": True}
            script = 'tell application "System Events" to keystroke "%s"' % text.replace('"', "")
            subprocess.run(["osascript", "-e", script], timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True}
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
        subprocess.run(
            ["osascript", "-e",
             'display notification "%s" with title "%s"' % (message, title)],
            timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

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
        if not _download_allowed(real):
            return self._json({"ok": False, "error": "path not allowed"}, 403)
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
                    "quartz": HAS_QUARTZ,
                    "bind": BIND_ADDR,
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
    if BIND_ADDR != "127.0.0.1":
        print("[WARN] non-loopback bind — allowlist: %s" % ",".join(ALLOW_LIST))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nRVG Mac agent stopped.")


if __name__ == "__main__":
    main()
