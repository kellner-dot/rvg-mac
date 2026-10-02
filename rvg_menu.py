#!/usr/bin/env python3
"""
RVG Mac menu bar app — user-visible status + control for the RVG Mac agent.

Runs as the console user (LaunchAgent at login). The agent itself runs as
root via the com.seth.rvg LaunchDaemon; this app only monitors and controls it.

Requires: pip install rumps
"""

import json
import os
import subprocess
import threading
import urllib.request

try:
    import rumps
except ImportError:
    raise SystemExit("rumps not installed: pip3 install --user rumps")

TOKEN_PATH = "/Users/sethkellner/rvd-mac/token.txt"
ICON_PATH = "/opt/rvg-mac/rvg-menu-icon.png"
VIEWER_URL = "http://127.0.0.1:8899/rvd/view"
STATUS_URL = "http://127.0.0.1:8899/rvd/status"
DAEMON_LABEL = "system/com.seth.rvg"


def _token():
    for p in (TOKEN_PATH, os.path.expanduser("~/rvd-mac/token.txt"),
              os.path.expanduser("~/rvd/token.txt")):
        try:
            with open(p) as f:
                tok = f.read().strip()
            if tok:
                return tok
        except OSError:
            pass
    return ""


def _agent_status():
    """Returns (running: bool, version: str)."""
    tok = _token()
    if not tok:
        return False, ""
    try:
        req = urllib.request.Request(STATUS_URL, headers={"X-RVD-Token": tok})
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.load(r)
            return True, str(d.get("version", ""))
    except Exception:
        return False, ""


def _admin(cmd):
    """Run a shell command with administrator privileges (prompts once)."""
    script = 'do shell script "%s" with administrator privileges' % cmd.replace('"', "")
    subprocess.run(["osascript", "-e", script],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class RvgMenu(rumps.App):
    def __init__(self):
        super().__init__("RVG", icon=ICON_PATH if os.path.isfile(ICON_PATH) else None,
                         template=True, quit_button=None)
        self.status_item = rumps.MenuItem("Checking…")
        self.menu = [
            self.status_item,
            rumps.separator,
            rumps.MenuItem("Open Web Viewer", callback=self.open_viewer),
            rumps.separator,
            rumps.MenuItem("Start Agent", callback=self.start_agent),
            rumps.MenuItem("Stop Agent", callback=self.stop_agent),
            rumps.MenuItem("Restart Agent", callback=self.restart_agent),
            rumps.separator,
            rumps.MenuItem("Quit Menu Bar", callback=self.quit_app),
        ]
        self._running = False
        self._version = ""
        self._ui_timer = None
        self._poller = rumps.Timer(self._poll, 30)
        self._poller.start()
        self._poll(None)  # immediate first check

    def _poll(self, _timer):
        threading.Thread(target=self._finish_poll, daemon=True).start()

    def _finish_poll(self):
        running, version = _agent_status()
        self._running, self._version = running, version
        # apply UI on the main thread via a retained one-shot timer
        self._ui_timer = rumps.Timer(self._apply_ui, 0.2)
        self._ui_timer.start()

    def _apply_ui(self, timer):
        timer.stop()
        if self._running:
            self.status_item.title = "Agent running (v%s)" % (self._version or "?")
            self.title = "●"
        else:
            self.status_item.title = "Agent stopped"
            self.title = "○"

    @rumps.clicked("Open Web Viewer")
    def open_viewer(self, _):
        subprocess.run(["open", VIEWER_URL],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @rumps.clicked("Start Agent")
    def start_agent(self, _):
        threading.Thread(target=_admin,
                         args=("launchctl kickstart -k %s" % DAEMON_LABEL,),
                         daemon=True).start()
        rumps.notification("RVG Mac", "Starting agent…", "")

    @rumps.clicked("Stop Agent")
    def stop_agent(self, _):
        threading.Thread(target=_admin,
                         args=("launchctl bootout %s" % DAEMON_LABEL,),
                         daemon=True).start()
        rumps.notification("RVG Mac", "Stopping agent…", "")

    @rumps.clicked("Restart Agent")
    def restart_agent(self, _):
        threading.Thread(target=_admin,
                         args=("launchctl kickstart -k %s" % DAEMON_LABEL,),
                         daemon=True).start()
        rumps.notification("RVG Mac", "Restarting agent…", "")

    @rumps.clicked("Quit Menu Bar")
    def quit_app(self, _):
        rumps.quit_application()


if __name__ == "__main__":
    RvgMenu().run()
