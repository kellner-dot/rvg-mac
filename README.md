# RVG Mac agent v1.0.0

macOS (Apple Silicon native) remote desktop agent — the Mac equivalent of the
Windows RVG agent. Same API, same port (8899), same token auth, so all existing
tooling (`fast.sh`, viewers, scripts) works unchanged against the Mac.

## What it does

- **Screenshots** — `/rvd/shot` via `screencapture`
- **Remote control** — `/rvd/input` (mouse + keyboard via Quartz)
- **Shell** — `/rvd/exec` runs `/bin/zsh` commands, returns stdout/stderr/exitCode
- **Files** — `/rvd/download?path=` and `/rvd/upload` (gated to `~/rvd/inbox/`)
- **Notifications** — `/rvd/notify` (macOS banner), `/rvd/push` (ntfy.sh phone push)
- **Web viewer** — `/rvd/view` with token gate, auto-refresh, click-to-control

## Components

| Piece | What | Runs as |
|---|---|---|
| `rvg_mac.py` | The agent (port 8899) | root via LaunchDaemon `com.seth.rvg` |
| `rvg_menu.py` | Menu bar status + control (rumps) | user via LaunchAgent `com.seth.rvg-menu` |
| `RVG Mac.app` | Fancy .app in /Applications — opens the viewer, offers to start the agent | user (double-click) |
| `icon/` | Custom icon artwork + `make_icns.sh` (builds the .icns on the Mac) | — |

## Install (on the Mac)

```bash
cd ~/path/to/rvg-mac
sudo bash install.sh
```

One-time macOS permissions (**System Settings → Privacy & Security**):
1. **Screen Recording** → `/usr/bin/python3` (for screenshots)
2. **Accessibility** → `/usr/bin/python3` (for remote input)

Then restart the agent:
```bash
sudo launchctl kickstart -k system/com.seth.rvg
```

The token lives at `/Users/sethkellner/rvd-mac/token.txt`, backed up to
Google Drive as `RVG-mac-token.txt` (same pattern as the PC's `RVG-token.txt`).
See [SHARING.md](SHARING.md) for how any Kavi connects. Keep it private.

## Uninstall

```bash
sudo launchctl bootout system/com.seth.rvg
sudo rm /Library/LaunchDaemons/com.seth.rvg.plist
launchctl bootout gui/$(id -u)/com.seth.rvg-menu
rm ~/Library/LaunchAgents/com.seth.rvg-menu.plist
sudo rm -rf /opt/rvg-mac "/Applications/RVG Mac.app"
```

## Security model

- `X-RVD-Token` header on every API call (except the viewer page, which has
  its own in-page token gate). Missing/bad token → 401.
- Non-loopback clients must be in `RVG_ALLOW` (default `100.64.0.0/10`,
  the tailnet). Everyone else → 403.
- Uploads are gated to `~/rvd/inbox/` with a 500 MiB cap
  (`RVG_UPLOAD_MAX_MB`).
- The token is full remote control of the Mac. Never put it in chat, logs,
  or repos.

## Files

- [API.md](API.md) — full endpoint reference
- [SHARING.md](SHARING.md) — how every Kavi (kavi1–kavi5) connects
- `com.seth.rvg.plist` — LaunchDaemon (root, boot)
- `com.seth.rvg-menu.plist` — LaunchAgent (user login, menu bar)
- `install.sh` — installer
- `icon/` — icon artwork + icns builder
- `app/RVG Mac.app` — the .app bundle
