# RVG Mac API reference (v1.0.0)

Base: `http://<host>:8899`. Every call needs the `X-RVD-Token` header
**except** `GET /rvd/view` (the page itself; the token is entered in-page).
`GET /` with no token returns 401. Bad/missing token on any other
endpoint returns `401 {"ok": false, "error": "bad or missing token"}`.
Non-loopback clients outside `RVG_ALLOW` (default `100.64.0.0/10`) get 403.

Mirrors Windows RVG semantics; the one intentional difference is that
`/rvd/exec` runs `/bin/zsh`, not PowerShell.

## `GET /rvd/status`

```json
{
  "ok": true,
  "version": "1.0.0",
  "screenW": 2560,
  "screenH": 1600,
  "hostname": "Seths-MacBook-Air",
  "platform": "darwin",
  "arch": "arm64",
  "user": "sethkellner",
  "quartz": true,
  "bind": "0.0.0.0"
}
```

## `GET /rvd/shot?scale=0.5&format=png`

Returns the screenshot as an image blob (`image/png` or `image/jpeg`).
- `scale` — e.g. `0.5` (resized with `sips`; default `1`)
- `format` — `png` (default) or `jpg`
- 500 with `{"ok": false, ...}` if capture fails (e.g. Screen Recording
  permission missing)

The viewer auto-refreshes every 1200 ms when enabled.

## `POST /rvd/input`

`Content-Type: application/json`. Returns `{"ok": true}` or
`{"ok": false, "error": "..."}`.

| `type` | Fields | Effect |
|---|---|---|
| `"move"` | `x`, `y` (integers) | Move mouse to absolute screen pixels |
| `"click"` | `x`, `y` (optional) | Click (at `x`,`y`, else current position) |
| `"key"` | `key`, `ctrl`/`alt`/`shift` (booleans) | Press a key with modifiers |
| `"text"` | `text` (string) | Type a string (Unicode-safe) |

Key names mirror Windows RVG: single characters lowercased (`"a"`, `"5"`),
plus `"enter"`, `"tab"`, `"escape"`, `"backspace"`, `"delete"`, `"space"`,
`"left"`, `"up"`, `"right"`, `"down"`, `"home"`, `"end"`, `"pageup"`,
`"pagedown"`, `"f1"`…`"f12"`.

Backend: Quartz `CGEvent` (PyObjC, ships with macOS) when available,
`cliclick` fallback for mouse, `osascript` fallback for keys.
Requires the **Accessibility** permission for the agent process.

## `POST /rvd/exec`

Body: `{"command": "...", "timeout": 30}`. Runs via `/bin/zsh -c`.
Commands over 8000 chars are rejected.

```json
{ "stdout": "...", "stderr": "...", "exitCode": 0 }
```

Timeouts return `exitCode: 124`. A bad command never crashes the agent.

## `GET /rvd/download?path=`

Downloads a file as `application/octet-stream`. `path` may be absolute or
`~`-relative. 404 if not found.

## `POST /rvd/upload?filename=`

Raw request body is saved to `/Users/sethkellner/rvd-mac/inbox/`
(filename sanitized with `basename`; collisions get `-1`, `-2` suffixes).
Default cap: 500 MiB (`RVG_UPLOAD_MAX_MB`); over-cap returns 413.

Response: `{"ok": true, "path": "...", "size": 1234}`.

## `POST /rvd/notify`

Body: `{"title": "...", "message": "..."}`. Shows a macOS notification
banner on the Mac.

## `POST /rvd/push` and `POST /rvd/push-config`

Phone push via ntfy.sh, mirroring Windows RVG.
- `POST /rvd/push-config` with `{"topic": "...", "server": "https://ntfy.sh"}`
  stores the config at `/Users/sethkellner/rvd-mac/push-config.json` (600).
- `POST /rvd/push` with `{"title", "message"}` delivers to the topic.
  Returns `{"ok": false, "error": "no push topic configured"}` until configured.

## `GET /rvd/view`

The web viewer (200, HTML, no token header needed):
- Token gate overlay (token from `/Users/sethkellner/rvd-mac/token.txt`)
- Auto-refresh toggle (1200 ms)
- Click-to-control toggle: click the screenshot to move+click; keydown
  sends keys; a type box sends longer text
- Status line with shot timestamp

## `POST /rvd/stop`

Returns `{"ok": true, "stopping": true}` and stops the agent. (The
LaunchDaemon's `KeepAlive` will restart it — use `launchctl bootout` to
stop it persistently.)
