# v2 pilot target — v1.2.1

## What this is

`rvg_mac-1.2.1.py` is the v2 pilot target: byte-identical to the v1.2.0
agent (`../rvg_mac.py`) except for the `VERSION` string (`"1.2.1"`)
and a docstring stamp marking it as the pilot target. It is NOT
installed by install-v2.sh and is NOT part of the v2 bundle. It exists
only so the pilot (FINGER-STEPS-v2.md step 5) has a newer version to
self-update TO.

`rvg_mac-1.2.1.py.sha256` is its SHA-256 manifest (same format as the
v1.2.0 manifest: `<hex>  rvg_mac.py` — the filename inside the manifest
is `rvg_mac.py`, not the pilot filename, because that is the asset name
the agent downloads and verifies).

## When to publish it

AFTER the v2 install + re-grant + verify PASS (FINGER-STEPS-v2.md
steps 2–4). Do NOT publish v1.2.1 before the v2 agent is installed:
it would become `releases/latest` and the freshly installed 1.2.0
agent would immediately self-update to it, collapsing the two pilot
phases (check-path proof, then update proof) into one.

## How to publish it

From the VM, after the v2 install + verify PASS:

```sh
cd ~/workspace/kavibot-readiness/round-2026-10-28/rvg-fix/mac-app-v2
python3 publish_release_v2.py --version 1.2.1 \
    --script pilot/rvg_mac-1.2.1.py --manifest pilot/rvg_mac-1.2.1.py.sha256
```

The script tags `v1.2.1`, creates the release on
`kellner-dot/kavibot-readiness`, uploads the two assets, and verifies
via anonymous GET `releases/latest` + download + SHA-match (same as
the v1.2.0 publish). The v2 agent then sees 1.2.1 on its next check
(daily, on launch, or via POST `/rvd/update-check`) and self-updates.

## Pilot pass/fail (FINGER-STEPS-v2.md step 5)

- **PASS:** after the self-update, `/rvd/status` reports version
  1.2.1 and `verify-v2.sh --with-input` passes (screenshots 3/3,
  cursor moves seen) with NO re-grant. The loader pattern is proven.
- **FAIL:** screenshots fail after the self-update with no re-grant.
  Do NOT re-grant to "fix" it. Roll back to v1
  (`rollback-v2.sh --go`) — the loader pattern is dead, stay on
  Option A (bundled script, finger install per update).
