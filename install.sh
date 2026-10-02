#!/bin/bash
# RVG Mac agent installer — run with sudo.
# Installs the agent (LaunchDaemon, root), the menu bar app (LaunchAgent),
# builds the .icns icon, and installs "RVG Mac.app" into /Applications.
set -euo pipefail

INSTALL_DIR="/opt/rvg-mac"
DAEMON_PLIST="/Library/LaunchDaemons/com.seth.rvg.plist"
AGENT_PLIST_NAME="com.seth.rvg-menu.plist"
RVD_DIR="/Users/sethkellner/rvd-mac"
CONSOLE_USER="sethkellner"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$EUID" -ne 0 ]; then
    echo "Run with sudo: sudo bash install.sh"
    exit 1
fi

echo "=== RVG Mac agent v$(cat "$SCRIPT_DIR/VERSION") installer ==="

# 1. Install agent + menu bar app
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/rvg_mac.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/rvg_menu.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/icon/rvg-menu-icon.png" "$INSTALL_DIR/" 2>/dev/null || true
chmod 755 "$INSTALL_DIR/rvg_mac.py" "$INSTALL_DIR/rvg_menu.py"
python3 -m py_compile "$INSTALL_DIR/rvg_mac.py" && echo "[ok] agent syntax check"
python3 -m py_compile "$INSTALL_DIR/rvg_menu.py" && echo "[ok] menu syntax check"

# 2. Token — reuse the existing one if present, else generate
mkdir -p "$RVD_DIR" "$RVD_DIR/inbox"
if [ ! -f "$RVD_DIR/token.txt" ]; then
    python3 -c "import secrets; print(secrets.token_hex(32))" > "$RVD_DIR/token.txt"
    echo "[ok] generated new token at $RVD_DIR/token.txt"
else
    echo "[ok] reusing existing token at $RVD_DIR/token.txt"
fi
chmod 600 "$RVD_DIR/token.txt"
chown -R "$CONSOLE_USER:staff" "$RVD_DIR"

# 3. LaunchDaemon (root, runs the agent)
cp "$SCRIPT_DIR/com.seth.rvg.plist" "$DAEMON_PLIST"
chmod 644 "$DAEMON_PLIST"
chown root:wheel "$DAEMON_PLIST"
launchctl bootout system/com.seth.rvg 2>/dev/null || true
launchctl bootstrap system "$DAEMON_PLIST"
echo "[ok] agent LaunchDaemon started"

# 4. rumps for the menu bar app (user site-packages)
sudo -u "$CONSOLE_USER" python3 -m pip install --user --quiet rumps \
    && echo "[ok] rumps installed" || echo "[warn] rumps install failed — menu bar needs: python3 -m pip install --user rumps"

# 5. Menu bar LaunchAgent (user domain)
AGENT_DIR="/Users/$CONSOLE_USER/Library/LaunchAgents"
mkdir -p "$AGENT_DIR"
cp "$SCRIPT_DIR/$AGENT_PLIST_NAME" "$AGENT_DIR/"
chmod 644 "$AGENT_DIR/$AGENT_PLIST_NAME"
chown "$CONSOLE_USER:staff" "$AGENT_DIR/$AGENT_PLIST_NAME"
sudo -u "$CONSOLE_USER" launchctl bootout "gui/$(id -u "$CONSOLE_USER")/$AGENT_PLIST_NAME" 2>/dev/null || true
sudo -u "$CONSOLE_USER" launchctl bootstrap "gui/$(id -u "$CONSOLE_USER")" "$AGENT_DIR/$AGENT_PLIST_NAME" \
    && echo "[ok] menu bar agent started" || echo "[warn] menu bar agent did not start (needs a logged-in GUI session)"

# 6. Build the .icns and install RVG Mac.app
if [ -x "$SCRIPT_DIR/icon/make_icns.sh" ] || [ -f "$SCRIPT_DIR/icon/make_icns.sh" ]; then
    bash "$SCRIPT_DIR/icon/make_icns.sh"
    cp "$SCRIPT_DIR/icon/RVG Mac.icns" "$SCRIPT_DIR/app/RVG Mac.app/Contents/Resources/"
    echo "[ok] icon built"
fi
chmod +x "$SCRIPT_DIR/app/RVG Mac.app/Contents/MacOS/rvg-launcher"
rm -rf "/Applications/RVG Mac.app"
cp -R "$SCRIPT_DIR/app/RVG Mac.app" "/Applications/"
echo "[ok] RVG Mac.app installed in /Applications"

sleep 2
if curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:8899/ | grep -q 401; then
    echo "[ok] agent responding on 127.0.0.1:8899"
else
    echo "[warn] agent not responding yet — check /var/log/rvg-mac.log"
fi

echo ""
echo "One-time macOS permissions (System Settings > Privacy & Security):"
echo "  1. Screen Recording -> /usr/bin/python3   (for /rvd/shot)"
echo "  2. Accessibility    -> /usr/bin/python3   (for /rvd/input)"
echo "Then: sudo launchctl kickstart -k system/com.seth.rvg"
echo ""
echo "Token: $RVD_DIR/token.txt"
echo "  Back it up to Drive after install (see SHARING.md):"
echo "  hatch_gws_cli drive +upload $RVD_DIR/token.txt --name RVG-mac-token.txt"
echo "Viewer: http://127.0.0.1:8899/rvd/view  (or open RVG Mac.app)"
