#!/bin/bash
# RVG Mac .app wrapper install — v2 UPGRADE (v1 bundle -> v2 loader layout).
#
# What this does, in order: gates (staged SHAs, toolchain, signing
# identity, interpreter, live v1 state) BEFORE any snapshot; snapshots
# the live v1 state (whole v1 bundle + live plist + external script if
# present + stale /opt source if present) with a bare-filename SHA
# manifest; builds the v2 "RVG Mac" .app bundle on this Mac (compile
# the v2 launcher, assemble launcher + Info.plist ONLY — the v2 bundle
# carries NO Resources/rvg_mac.py, the agent lives outside it — sign
# with the self-signed certificate, verify the signature); places the
# v2 agent script at ~/rvd-mac/agent/rvg_mac.py (SHA-verified);
# replaces the v1 bundle with the v2 bundle; swaps the user LaunchAgent
# to the v2 plist (PATH includes /opt/homebrew/bin for cliclick);
# verifies the job runs the bundle.
#
# What this does NOT do: no network, no downloads, no pip, no fetches
# of any kind. PyObjC is NOT fetched (separate explicitly-worded step).
# The token file is never read. No HTTP checks run here (verify-v2.sh
# does those).
#
# This is an UPGRADE, not a fresh install: it EXPECTS the v1 bundle to
# already exist (and refuses if it does not — that is a fresh-install
# state, not this script's job). The v1 bundle is snapshotted whole so
# rollback-v2.sh can restore it exactly.
#
# Signing discipline (see BUILD-RECORD + v1 re-audit): TCC grants are
# keyed to the bundle identifier AND the code-signing identity's
# designated requirement. Ad-hoc signatures make the requirement BE the
# cdhash, so every rebuild voids the grants. This install therefore
# REFUSES to ad-hoc sign: it requires exactly one matching self-signed
# certificate identity and fails closed without it. The v2 bundle is a
# NEW seal (new cdhash), so after this install Seth MUST re-grant
# Screen Recording + Accessibility by finger (tccutil reset + toggles,
# FINGER-STEPS-v2.md step 3) before the Mac is drivable again. That is
# the ONE-TIME cost of v2. After it, script updates never touch the
# bundle, so the seal never changes and the grants survive them — that
# is the whole point of v2.
#
# Run in the FOREGROUND under sudo from the stage directory (the v2
# nohup/& form cannot read the sudo password and dies). Example:
#   cd <stage-dir> && sudo bash install-v2.sh --go \
#     >>/Users/sethkellner/rvd-mac/install-v2-2026-11-06.log 2>&1
# The ONLY password prompt is sudo's. On FATAL after the snapshot,
# run rollback-v2.sh --go; do NOT re-run this script.

set -u
# NOTE: no `set -e`. Every fallible step checks its own result and
# calls die() with a named cause, so a mid-script failure can never
# silently fall through to the next step.

PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

# ---- constants (SHA gates: change nothing here without re-staging) ----
BUNDLE_ID="com.seth.rvgmac"
APP_NAME="RVG Mac"
SIGN_IDENTITY="RVG Mac Signing"
JOB_LABEL="com.seth.rvg"
EXPECTED_SRC_SHA="61f6ec37a892285d2f69aaeefb05978f06771de8a11e40c8312214b98f98212b"
EXPECTED_LAUNCHER_SHA="2c24e045eca5450c430829628de880d1fc9d2f5deabdf9015b3f35a346cfbcbc"
EXPECTED_INFO_SHA="f5282a574b7b9a97cfed7d3a6c43159c2d4c650e6e9a06c0143eb43a475a135a"
EXPECTED_PLIST_SHA="255e994152920d59d6b507597eb723aaa3d9ec760b49e91dbc25f519a9222d24"
INTERPRETER="/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python"
AGENT_SCRIPT="rvg_mac.py"

# ---- paths (MACAPP_* overrides exist ONLY for the /tmp simulation) ----
STAGE_DIR="$(cd "$(dirname "$0")" && pwd)"
CONSOLE_USER="$(stat -f%Su /dev/console 2>/dev/null || echo UNKNOWN)"
CONSOLE_HOME="/Users/$CONSOLE_USER"
OPT_DIR="/opt/rvg-mac"
BUNDLE_DIR="/Applications/RVG Mac.app"
AGENTS_DIR="$CONSOLE_HOME/Library/LaunchAgents"
BACKUP_PARENT="$CONSOLE_HOME/rvd-mac"
AGENT_DIR="$CONSOLE_HOME/rvd-mac/agent"
# Sim-only overrides (honored ONLY when MACAPP_SIM is set; a real run
# never silently uses an environment-redirected path):
if [ -n "${MACAPP_SIM:-}" ]; then
  CONSOLE_USER="${MACAPP_CONSOLE_USER:-$CONSOLE_USER}"
  CONSOLE_HOME="${MACAPP_CONSOLE_HOME:-$CONSOLE_HOME}"
  OPT_DIR="${MACAPP_OPT:-$OPT_DIR}"
  BUNDLE_DIR="${MACAPP_BUNDLE_DIR:-$BUNDLE_DIR}"
  AGENTS_DIR="${MACAPP_AGENTS_DIR:-$AGENTS_DIR}"
  BACKUP_PARENT="${MACAPP_BACKUP_PARENT:-$BACKUP_PARENT}"
  AGENT_DIR="${MACAPP_AGENT_DIR:-$AGENT_DIR}"
fi
LIVE_PLIST="$AGENTS_DIR/com.seth.rvg.plist"
DAEMON_PLIST="/Library/LaunchDaemons/com.seth.rvg.plist"
BUILD_DIR="$STAGE_DIR/build"
LIVE_AGENT_SCRIPT="$AGENT_DIR/$AGENT_SCRIPT"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

# ---- gates: --go, root, console user ----
[ "${1:-}" = "--go" ] || { echo "usage: $0 --go (refuses to run without --go)" >&2; exit 2; }
if [ -z "${MACAPP_SIM:-}" ]; then
    [ "$(id -u)" = "0" ] || die "must run as root (sudo); refusing without root"
    [ "$CONSOLE_USER" = "sethkellner" ] || die "console user is '$CONSOLE_USER', expected 'sethkellner'; refusing"
fi

# ---- gates: stage files present + staged SHAs (BEFORE any snapshot) ----
for f in RVGMac.c Info.plist rvg_mac.py com.seth.rvg.plist; do
    [ -f "$STAGE_DIR/$f" ] || die "stage file missing: $STAGE_DIR/$f"
done
sha_of() { shasum -a 256 "$1" | awk '{print $1}'; }
[ "$(sha_of "$STAGE_DIR/rvg_mac.py")" = "$EXPECTED_SRC_SHA" ] \
    || die "staged rvg_mac.py SHA mismatch; refusing (re-stage, do not edit)"
[ "$(sha_of "$STAGE_DIR/RVGMac.c")" = "$EXPECTED_LAUNCHER_SHA" ] \
    || die "staged RVGMac.c SHA mismatch; refusing (re-stage, do not edit)"
[ "$(sha_of "$STAGE_DIR/Info.plist")" = "$EXPECTED_INFO_SHA" ] \
    || die "staged Info.plist SHA mismatch; refusing (re-stage, do not edit)"
[ "$(sha_of "$STAGE_DIR/com.seth.rvg.plist")" = "$EXPECTED_PLIST_SHA" ] \
    || die "staged com.seth.rvg.plist SHA mismatch; refusing (re-stage, do not edit)"
grep -Fq "$INTERPRETER" "$STAGE_DIR/RVGMac.c" \
    || die "launcher source does not embed the verified interpreter path; refusing"
grep -Fq "/rvd-mac/agent/rvg_mac.py" "$STAGE_DIR/RVGMac.c" \
    || die "launcher source does not exec the external agent path; refusing (not the v2 loader)"
grep -Fq 'VERSION = "1.2.0"' "$STAGE_DIR/rvg_mac.py" \
    || die "staged rvg_mac.py is not version 1.2.0; refusing"
log "staged SHAs verified (agent 1.2.0, v2 loader, Info.plist, LaunchAgent plist)"

# ---- gates: toolchain, interpreter, signing identity ----
command -v cc >/dev/null 2>&1 || die "cc not found; Xcode command-line tools required"
command -v codesign >/dev/null 2>&1 || die "codesign not found; refusing"
INTERP_CHECK="$INTERPRETER"
# Sim-only interpreter override (honored ONLY when MACAPP_SIM is set):
if [ -n "${MACAPP_SIM:-}" ]; then
  INTERP_CHECK="${MACAPP_INTERPRETER:-$INTERP_CHECK}"
fi
[ -x "$INTERP_CHECK" ] || die "verified interpreter missing or not executable: $INTERP_CHECK (Xcode moved it; re-verify from live ps, do not guess)"
IDENT_TMP="$(mktemp)"
security find-identity -v -p codesigning >"$IDENT_TMP" 2>&1 \
    || die "cannot list code-signing identities"
IDENT_COUNT="$(grep -c -F "$SIGN_IDENTITY" "$IDENT_TMP" || true)"
rm -f "$IDENT_TMP"
[ "$IDENT_COUNT" = "1" ] || die "signing identity '$SIGN_IDENTITY' must appear exactly once (found $IDENT_COUNT); it already exists from v1 (System keychain) — do NOT create a second one"
log "signing identity present exactly once: $SIGN_IDENTITY"

# ---- gates: live v1 state (bundle MUST exist; this is an upgrade) ----
[ -d "$BUNDLE_DIR" ] || die "bundle missing: $BUNDLE_DIR (expected the v1 bundle; this is an upgrade script, not a fresh install)"
FOUND_ID="$(plutil -extract CFBundleIdentifier raw "$BUNDLE_DIR/Contents/Info.plist" 2>/dev/null || echo MISSING)"
[ "$FOUND_ID" = "$BUNDLE_ID" ] || die "bundle at $BUNDLE_DIR has identifier '$FOUND_ID', not '$BUNDLE_ID'; refusing (foreign bundle)"
[ -f "$LIVE_PLIST" ] || die "live user LaunchAgent plist missing: $LIVE_PLIST (expected the v1 install state)"
[ ! -e "$DAEMON_PLIST" ] || die "root daemon plist still present: $DAEMON_PLIST (expected absent); resolve before upgrading"
LIVE_BUNDLE_VERSION="$(plutil -extract CFBundleShortVersionString raw "$BUNDLE_DIR/Contents/Info.plist" 2>/dev/null || echo UNKNOWN)"
[ "$LIVE_BUNDLE_VERSION" = "1.1.0" ] || log "WARN: live bundle version is '$LIVE_BUNDLE_VERSION', expected '1.1.0' (v1); continuing (the snapshot + rollback still apply)"
LIVE_PLIST_SHA="$(sha_of "$LIVE_PLIST")"
log "live v1 state confirmed: bundle $LIVE_BUNDLE_VERSION ($BUNDLE_ID), plist sha=$LIVE_PLIST_SHA, daemon absent"

# ---- snapshot (whole v1 bundle + live plist + external script if any
#      + stale /opt source if any; bare-filename manifest + self-check) ----
BACKUP_DIR="$BACKUP_PARENT/app-v2-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR" || die "cannot create backup dir $BACKUP_DIR"
ditto "$BUNDLE_DIR" "$BACKUP_DIR/RVG Mac.app" || die "cannot snapshot live v1 bundle"
cp "$LIVE_PLIST" "$BACKUP_DIR/com.seth.rvg.plist" || die "cannot snapshot live plist"
if [ -f "$LIVE_AGENT_SCRIPT" ]; then
    mkdir -p "$BACKUP_DIR/agent"
    cp "$LIVE_AGENT_SCRIPT" "$BACKUP_DIR/agent/rvg_mac.py" || die "cannot snapshot existing external agent script"
    log "snapshotted pre-existing external agent script (re-install state)"
fi
if [ -f "$OPT_DIR/rvg_mac.py" ]; then
    cp "$OPT_DIR/rvg_mac.py" "$BACKUP_DIR/opt-rvg_mac.py" || die "cannot snapshot stale /opt source"
    log "snapshotted stale /opt/rvg-mac/rvg_mac.py (v2-era leftover, untouched by v1)"
fi
{
    echo "backup created: $(date)"
    echo "console user: $CONSOLE_USER"
    echo "live bundle version: $LIVE_BUNDLE_VERSION"
    echo "live plist sha: $LIVE_PLIST_SHA"
    echo "daemon plist: absent (v1 state)"
    echo "staged v2 source sha: $EXPECTED_SRC_SHA"
    echo "staged v2 launcher sha: $EXPECTED_LAUNCHER_SHA"
} > "$BACKUP_DIR/backup-meta.txt"
(cd "$BACKUP_DIR" && shasum -a 256 "RVG Mac.app/Contents/Info.plist" "RVG Mac.app/Contents/MacOS/RVGMac" "RVG Mac.app/Contents/Resources/rvg_mac.py" com.seth.rvg.plist > backup-manifest.sha256) \
    || die "cannot write backup manifest"
(cd "$BACKUP_DIR" && shasum -a 256 -c backup-manifest.sha256 >/dev/null) \
    || die "backup manifest self-check failed; refusing to proceed"
log "snapshot verified: $BACKUP_DIR/backup-manifest.sha256 (whole v1 bundle + plist)"

# ---- build the v2 bundle (launcher + Info.plist ONLY; no agent script
#      inside — the agent lives outside it now) ----
rm -rf "$BUILD_DIR"
APP_BUILD_DIR="$BUILD_DIR/RVG Mac.app"
mkdir -p "$APP_BUILD_DIR/Contents/MacOS" \
    || die "cannot create bundle skeleton"
cp "$STAGE_DIR/Info.plist" "$APP_BUILD_DIR/Contents/Info.plist" \
    || die "cannot stage Info.plist"
chmod 644 "$APP_BUILD_DIR/Contents/Info.plist" \
    || die "cannot chmod staged Info.plist"
plutil -lint "$APP_BUILD_DIR/Contents/Info.plist" >/dev/null \
    || die "staged Info.plist fails plutil -lint"
BUILD_LOG="$BACKUP_DIR/build.log"
cc -O2 -Wall -Wextra -o "$APP_BUILD_DIR/Contents/MacOS/RVGMac" "$STAGE_DIR/RVGMac.c" >"$BUILD_LOG" 2>&1 \
    || die "launcher compile failed; see $BUILD_LOG"
chmod 755 "$APP_BUILD_DIR/Contents/MacOS/RVGMac" \
    || die "cannot chmod launcher"
file "$APP_BUILD_DIR/Contents/MacOS/RVGMac" | grep -q "Mach-O" \
    || die "launcher is not a Mach-O executable; refusing to sign"
[ ! -e "$APP_BUILD_DIR/Contents/Resources" ] || die "v2 bundle must not contain a Resources dir (agent lives outside the bundle)"
log "v2 launcher compiled and verified Mach-O (bundle has no Resources — loader layout)"

# ---- sign (self-signed certificate ONLY; ad-hoc is refused by design) ----
codesign --force --sign "$SIGN_IDENTITY" --identifier "$BUNDLE_ID" --timestamp=none "$APP_BUILD_DIR" >>"$BUILD_LOG" 2>&1 \
    || die "codesign failed; see $BUILD_LOG (check the certificate trust setting: Code Signing must be Always Trust)"
codesign --verify --deep --strict --verbose=2 "$APP_BUILD_DIR" >>"$BUILD_LOG" 2>&1 \
    || die "codesign --verify failed on the built bundle; see $BUILD_LOG"
codesign -d -r- "$APP_BUILD_DIR" >"$BACKUP_DIR/designated-requirement.txt" 2>&1 \
    || die "cannot read designated requirement from the built bundle"
grep -Fq "identifier \"$BUNDLE_ID\"" "$BACKUP_DIR/designated-requirement.txt" \
    || die "designated requirement does not name $BUNDLE_ID; refusing to place"
grep -Fq "certificate leaf" "$BACKUP_DIR/designated-requirement.txt" \
    || die "designated requirement has no certificate leaf (ad-hoc seal?); refusing to place"
log "bundle signed and verified; requirement: $(grep -o 'designated =>.*' "$BACKUP_DIR/designated-requirement.txt" | head -1)"

# ---- place the external agent script FIRST (the launcher needs it
#      the moment the job restarts) ----
mkdir -p "$AGENT_DIR" || die "cannot create agent dir $AGENT_DIR"
cp "$STAGE_DIR/rvg_mac.py" "$LIVE_AGENT_SCRIPT" || die "cannot place external agent script"
chmod 644 "$LIVE_AGENT_SCRIPT" || die "cannot chmod external agent script"
[ "$(sha_of "$LIVE_AGENT_SCRIPT")" = "$EXPECTED_SRC_SHA" ] \
    || die "placed external agent script SHA mismatch; run rollback-v2.sh --go"
python3 -m py_compile "$LIVE_AGENT_SCRIPT" || die "placed external agent script fails py_compile; run rollback-v2.sh --go"
log "external agent script placed and SHA-verified: $LIVE_AGENT_SCRIPT (1.2.0)"

# ---- replace the bundle (old v1 bundle removed only after the new
#      v2 bundle is built + signed and the external script is placed) ----
rm -rf "$BUNDLE_DIR" || die "cannot remove old v1 bundle; run rollback-v2.sh --go"
[ ! -e "$BUNDLE_DIR" ] || die "old bundle dir still present after removal"
ditto "$APP_BUILD_DIR" "$BUNDLE_DIR" \
    || { die "ditto failed placing the v2 bundle; old bundle already removed — run rollback-v2.sh --go to restore v1"; }
[ ! -e "$BUNDLE_DIR/Contents/Resources/rvg_mac.py" ] \
    || die "placed v2 bundle unexpectedly contains a bundled agent script; run rollback-v2.sh --go"
codesign --verify --deep --strict "$BUNDLE_DIR" >/dev/null 2>&1 \
    || die "placed bundle fails codesign --verify; run rollback-v2.sh --go"
log "v2 bundle placed: $BUNDLE_DIR (no bundled script — loader layout)"

# ---- swap the LaunchAgent job to the v2 plist ----
launchctl bootout "gui/501/$JOB_LABEL" >/dev/null 2>&1 || true
for _ in 1 2 3 4 5; do
    launchctl print "gui/501/$JOB_LABEL" >/dev/null 2>&1 || break
    sleep 1
done
launchctl print "gui/501/$JOB_LABEL" >/dev/null 2>&1 && die "old job still loaded after bootout; run rollback-v2.sh --go"
cp "$STAGE_DIR/com.seth.rvg.plist" "$LIVE_PLIST" || die "cannot place new LaunchAgent plist"
chmod 644 "$LIVE_PLIST" || die "cannot chmod new LaunchAgent plist"
chown "$CONSOLE_USER" "$LIVE_PLIST" 2>/dev/null || log "WARN: chown user agent plist failed; continuing (root-owned plist is acceptable)"
plutil -lint "$LIVE_PLIST" >/dev/null || die "new LaunchAgent plist fails plutil -lint"
launchctl bootstrap "gui/501" "$LIVE_PLIST" \
    || die "launchctl bootstrap failed; run rollback-v2.sh --go"
for _ in 1 2 3 4 5; do
    launchctl print "gui/501/$JOB_LABEL" >/dev/null 2>&1 && break
    sleep 1
done
launchctl print "gui/501/$JOB_LABEL" >/dev/null 2>&1 \
    || die "new job is not loaded after bootstrap; run rollback-v2.sh --go"
launchctl print "gui/501/$JOB_LABEL" 2>/dev/null | grep -Fq "RVGMac" \
    || die "loaded job is not running the bundle executable; run rollback-v2.sh --go"
log "user LaunchAgent loaded in gui/501 running the v2 bundle"

rm -rf "$BUILD_DIR"
log "=== install complete (mechanical part) ==="
log "backup dir: $BACKUP_DIR"
log "external agent script: $LIVE_AGENT_SCRIPT (1.2.0)"
log "next: RE-GRANT Screen Recording + Accessibility by finger (new seal — FINGER-STEPS-v2.md step 3), then verify-v2.sh --with-input"
log "WARNING: this v2 bundle is a NEW seal; the v1 grants do NOT carry over. Re-grant before trusting screenshots/input."
log "After the re-grant + a passing verify, script updates self-apply with NO re-grant (that is the point of v2)."
