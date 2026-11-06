# Finger steps — Mac RVG agent v2 upgrade (loader layout + self-update)
# (Seth at the MacBook Air)

All steps below need Seth's hands/eyes at the Mac. Do them in order.

## What v2 is (read this first)

v1 (installed Nov 6, 2026, proven: verify PASS, screenshots 3/3, mouse
works) runs the agent script from INSIDE the bundle
(`.../RVG Mac.app/Contents/Resources/rvg_mac.py`). Any script change
meant a rebuild + re-sign + re-grant.

v2 moves the agent script OUTSIDE the bundle, to
`~/rvd-mac/agent/rvg_mac.py`. The bundle now contains only the launcher
+ Info.plist, so its seal never changes and the grants survive script
updates. The agent self-updates the external script (check -> download
-> SHA-verify -> atomic replace -> restart) with no finger step.

**This upgrade is a ONE-TIME rebuild + re-sign + re-grant.** The v2
bundle is a new seal, so the v1 grants do NOT carry over — step 3
re-grants them. After that, script updates need no re-grant. That is
the whole point of v2.

**The pilot (step 5) is the proof.** TCC attribution for an EXTERNAL
script is unproven until the pilot passes: after install + re-grant +
verify PASS, a self-update to a newer script version must leave
screenshots working WITHOUT a re-grant. If it does not, the loader
pattern is dead — roll back to v1 (step 6) and stay on the bundled
layout (Option A).

## 0. Confirm the signing certificate (already exists — do NOT create a second one)

The v1 certificate `RVG Mac Signing` (System keychain, exactly once,
Always Trust, expires Nov 2036) is reused. Verify in Terminal (no sudo):

```sh
security find-identity -v -p codesigning
```

The list must show `RVG Mac Signing` exactly once. If it shows zero
times, STOP and tell kavi7 (the cert is missing — do not create a new
one without his word). If it shows twice, STOP and tell kavi7 (the
install refuses a duplicate).

## 1. Confirm integrity before running anything

Mac awake, logged in as **sethkellner**, on a normal desktop (not the
login window). Confirm the delivered stage directory (this folder,
`mac-app-v2/`):

```sh
cd <stage-dir> && shasum -a 256 -c SHA256SUMS
```

All lines must say OK. If any line fails, STOP and tell kavi7.

## 2. Run the upgrade (FOREGROUND — it restarts the agent serving you)

In Terminal, foreground, no `nohup`, no `&` (background jobs cannot
read the sudo password — that failure mode is proven):

```sh
cd <stage-dir> && sudo bash install-v2.sh --go >>/Users/sethkellner/rvd-mac/install-v2-2026-11-06.log 2>&1; tail -25 /Users/sethkellner/rvd-mac/install-v2-2026-11-06.log
```

- The install ends with `=== install complete (mechanical part) ===`.
  If it ends with `FATAL` instead: STOP, keep the log, tell kavi7.
  Do NOT run verify — the rollback in step 6 is the next step.
- The only password prompt is the `sudo` prompt in Terminal. A
  keychain prompt may appear for the signing step ("codesign wants to
  use your key") — click **Always Allow** so it does not re-ask.
  Nothing else asks for a password. No token is typed anywhere.

What the install did: verified the staged file hashes, the toolchain,
the certificate (exactly once), and the interpreter path; snapshotted
the live v1 state (whole v1 bundle + live plist + any external script
+ stale /opt source) with a SHA manifest; compiled the v2 launcher,
built the v2 bundle (launcher + Info.plist ONLY — no bundled script),
signed it with your certificate, verified the signature and its
designated requirement; placed the v2 agent script at
`~/rvd-mac/agent/rvg_mac.py` (SHA-verified + py_compile); replaced the
v1 bundle with the v2 bundle; swapped the user LaunchAgent to the v2
plist (PATH includes /opt/homebrew/bin for cliclick); verified the job
runs the bundle. No downloads, no PyObjC fetch.

## 3. Re-grant Screen Recording + Accessibility (REQUIRED — new seal)

The v2 bundle is a new seal, so the v1 grants do NOT carry over.
Reset both entries first, then grant again from scratch:

```sh
tccutil reset ScreenCapture com.seth.rvgmac
tccutil reset Accessibility com.seth.rvgmac
```

(Terminal, no sudo; only ever run on a rebuild, never casually.)

Then:
- Run the check once (step 4 without `--with-input`). The first shot
  attempt registers the app with macOS. No prompt is expected: the
  denial is silent, and the grant is made by hand.
- Open **System Settings → Privacy & Security → Screen & System Audio
  Recording**. Switch the **RVG Mac** entry ON. (The entry name is the
  app's display name, `RVG Mac` — if several entries exist, it is the
  one whose icon is the monitor icon and whose name matches exactly.
  Do NOT enable entries you do not recognize.)
- Open **System Settings → Privacy & Security → Accessibility** and
  switch the **RVG Mac** entry ON.
- Restart the user agent job once:
  `launchctl kickstart -k gui/501/com.seth.rvg` (Terminal, no sudo).
- Re-run the check. Screenshots must come back as valid images.

## 4. Verify v2 (screenshots + mouse + loader layout)

```sh
cd <stage-dir> && bash verify-v2.sh --with-input
```

Watch the cursor move to (400,400) then (800,400), and SAY whether
you saw it. `ok:true` alone is not proof.

The verify also confirms the loader layout: the bundle has NO
`Resources/rvg_mac.py`, the agent process runs the external script
(`rvd-mac/agent/rvg_mac.py`), `/rvd/status` reports version 1.2.0,
`loaderLayout: true`, and the self-update fields are present.

- If verify ends with `VERIFY RESULT: PASS`, v2 is installed and
  drivable. Tell kavi7: verify passed. Then do the pilot (step 5).
- If verify ends with `FAIL` or `PARTIAL` after the grants: STOP, keep
  the output, tell kavi7. Do NOT re-run the install — the rollback in
  step 6 is the next step.

## 5. THE PILOT — prove a self-update preserves the grants (the whole point of v2)

Do this AFTER step 4 passes. It proves TCC attribution survives an
external-script update with NO re-grant. If it fails, v2 is dead and
v1 is restored (step 6).

### 5a. Publish the pilot target (v1.2.1)

kavi7 prepares the v1.2.1 agent (identical to v1.2.0 except the version
string) and publishes it as a Gitea release on
`kellner-dot/rvg-mac` (tag `v1.2.1`). You do not need to do
this — kavi7 runs the publish script from the VM. Wait for his word
that v1.2.1 is published before continuing.

### 5b. Force the agent to self-update

In Terminal (no sudo), trigger an immediate update check:

```sh
curl -s -H "X-RVD-Token: $(cat ~/rvd-mac/token.txt)" -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8899/rvd/update-check
```

(Or ask kavi7 to trigger it from the VM — he can do it through the
RVG lane.)

The response shows the update state. It should say the update to
1.2.1 was verified and is applying, and the agent restarts right
after. Wait ~10 seconds, then check the version:

```sh
curl -s -H "X-RVD-Token: $(cat ~/rvd-mac/token.txt)" http://127.0.0.1:8899/rvd/status | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['version'], d['loaderLayout'], d['lastUpdateResult'])"
```

It should print `1.2.1 True updated to 1.2.1; restarting` (or a
later check result). If the version is still 1.2.0, look at
`lastUpdateResult` / `updateError` in the full status and tell kavi7.

### 5c. Confirm screenshots + mouse still work — WITHOUT re-granting

This is the pass/fail arbiter. Do NOT touch the Screen Recording or
Accessibility toggles. Run:

```sh
cd <stage-dir> && bash verify-v2.sh --with-input
```

- **PILOT PASS:** `VERIFY RESULT: PASS`, screenshots 3/3, and you saw
  the cursor move — with NO re-grant after the self-update. The loader
  pattern is proven: future script updates self-apply with zero
  finger steps. Tell kavi7: pilot passed. v2 is the live layout.
- **PILOT FAIL:** screenshots fail (0/3 or partial) or moves fail
  after the self-update, with no re-grant. That means TCC attribution
  does NOT survive an external-script update. Do NOT re-grant to
  "fix" it — that would only prove v2 needs a finger step per update,
  which defeats it. Go to step 6 (rollback to v1) and tell kavi7:
  pilot failed, loader pattern dead, staying on Option A (bundled
  script, finger install per update).

## 6. If verification or the pilot fails — roll back to v1

- Do NOT re-run the install. Run the rollback (restores the v1 bundle
  + v1 plist, SHA-verified; preserves the v2 external script in the
  backup dir, does not delete it):
  `sudo bash rollback-v2.sh --go`
- The restored v1 bundle is the EXACT bundle that was granted (same
  seal, snapshotted whole), so its grants should still be in place —
  no re-grant is expected after a rollback.
- After the rollback, run the v1 verify to confirm:
  `cd <mac-app-v1 stage dir> && bash verify-v1.sh --with-input`
  (Screenshots should still work. If they do not, re-grant for the v1
  bundle and tell kavi7.)
- Tell kavi7 the verify/pilot output + the rollback result. Keep
  `~/rvd-mac/rvg-app.log`, `~/rvd-mac/rvg-app.err.log`, and the
  install log — do not delete them.

## 7. After a passing verify + pilot — the new rules (read this)

- Leave the Mac logged in (the agent only runs in your GUI session;
  it stops at logout). Sleep is fine; logout is not.
- **Script updates now self-apply.** When kavi7 publishes a new agent
  version, the agent checks daily + on launch, downloads it, verifies
  the SHA, replaces `~/rvd-mac/agent/rvg_mac.py`, and restarts itself.
  No finger step, no re-grant. You can see the update state in
  `/rvd/status` (`updateAvailable`, `updateVersion`, `updateError`,
  `lastUpdateCheckMs`, `lastUpdateResult`).
- **The bundle itself still must never be rebuilt without re-granting.**
  A launcher or Info.plist change needs a rebuild + re-sign + re-grant
  (same rule as v1: `tccutil reset` both entries, then toggle both ON).
  The launcher is 78 lines and changes rarely; the agent script is
  where the behaviour lives, and it updates itself.
- **If the agent ever crash-loops on launch**, check the external
  script first: `~/rvd-mac/agent/rvg_mac.py`. The previous version is
  kept at `~/rvd-mac/agent/rvg_mac.py.prev` — restoring it by hand
  (copy `.prev` over `rvg_mac.py`, then `launchctl kickstart -k
  gui/501/com.seth.rvg`) is the manual rollback for a bad self-update.
  Tell kavi7 if this ever happens.
- Tell kavi7: verify + pilot passed. The Mac lane is drivable AND
  self-updating.
