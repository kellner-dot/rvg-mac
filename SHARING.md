# RVG Mac — shared access for all Kavis

The Mac agent is reachable by **every Kavi** (kavi1–kavi5), using the same
pattern as the Windows RVG agent: a token file on the machine plus a Drive
backup. The API is identical to Windows RVG, so all existing tooling works
unchanged — just point it at the Mac.

## Connection details

| Item | Value |
|---|---|
| Tailnet IP | `100.92.65.104` (`seths-macbook-air`) |
| Port | `8899` (same as Windows RVG) |
| Auth header | `X-RVD-Token: <token>` on every API call |
| Token on the Mac | `/Users/sethkellner/rvd-mac/token.txt` |
| Token in Drive | `RVG-mac-token.txt` (next to `RVG-token.txt`) |
| Viewer | `http://100.92.65.104:8899/rvd/view` |

## Quick-connect snippet

Add to your shell (mirrors `fast.sh` on the PC side):

```bash
export TUNNEL_PROXY="${HTTPS_PROXY%:*}:3130"
export RVG_MAC_TOKEN="<paste from Drive/RVG-mac-token.txt>"   # transient only
export MAC="100.92.65.104"

# Run a shell command on the Mac, return stdout
mac_rvg() {
    curl --proxy "$TUNNEL_PROXY" --max-time 30 -s \
        -H "X-RVD-Token: $RVG_MAC_TOKEN" \
        -d "{\"command\":$(echo "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" \
        "http://$MAC:8899/rvd/exec" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('stdout','').strip())"
}

# Screenshot to /tmp/rvg_mac.png
mac_shot() {
    curl --proxy "$TUNNEL_PROXY" --max-time 15 -s \
        -H "X-RVD-Token: $RVG_MAC_TOKEN" \
        "http://$MAC:8899/rvd/shot?scale=0.5&format=png" -o /tmp/rvg_mac.png && echo "shot ok"
}
```

Example:
```bash
mac_rvg "sw_vers && system_profiler SPHardwareDataType | head -8"
mac_shot
```

## Token lifecycle

1. **Install** (`sudo bash install.sh` on the Mac) generates the token at
   `/Users/sethkellner/rvd-mac/token.txt` (600 perms, owned by sethkellner)
   — unless one already exists, in which case it is reused.
2. **Back up to Drive** (run once after install, from any machine with the
   Drive skill):
   ```bash
   hatch_gws_cli drive +upload /Users/sethkellner/rvd-mac/token.txt --name RVG-mac-token.txt
   ```
   On the installing machine you can also just open Drive in a browser and
   upload the file manually.
3. **Rotate**: overwrite `/Users/sethkellner/rvd-mac/token.txt` on the Mac,
   re-upload to Drive, restart the agent
   (`sudo launchctl kickstart -k system/com.seth.rvg`). All Kavis pick up
   the new token from Drive.

## Why a separate token from the PC

The Mac agent uses its **own** token (`RVG-mac-token.txt`), distinct from the
PC's `RVG-token.txt`. If one token is ever compromised, the other machine
stays safe. Any Kavi can hold both — they're both in Drive.

To deliberately unify them (one token for PC + Mac), copy the PC token over
the Mac token file and restart the Mac agent. Not recommended, but supported.

## API compatibility

Every endpoint mirrors Windows RVG v1.17 semantics:

- `GET /rvd/status` → `{ok, version, screenW, screenH, hostname, platform, arch}`
- `GET /rvd/shot?scale=&format=` → PNG/JPEG blob
- `POST /rvd/input` → `{type: move|click|key|text, ...}` (same shapes as PC)
- `POST /rvd/exec` → `{"command"}` runs via `/bin/zsh` (not PowerShell)
- `GET /rvd/download?path=` / `POST /rvd/upload?filename=` (inbox-gated)
- `POST /rvd/notify`, `/rvd/push`, `/rvd/push-config`, `/rvd/stop`

The one intentional difference: `/rvd/exec` runs **zsh**, not PowerShell.
Write Mac commands accordingly.

## Caveats for remote Kavis

- The Mac **must be awake** and on the tailnet. If `100.92.65.104:8899`
  doesn't answer, the Mac is asleep — nothing remote can wake it.
  (Seth fixed this 2026-10-01 with `pmset -c/-b sleep 0`, but verify.)
- `/rvd/shot` needs **Screen Recording** permission for `/usr/bin/python3`;
  `/rvd/input` needs **Accessibility**. If input returns `ok:true` but nothing
  happens, those permissions are the first thing to check.
- Uploads land in `/Users/sethkellner/rvd-mac/inbox/` (500 MiB cap).
