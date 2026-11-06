#!/bin/bash
# RVG Mac .app wrapper rollback — v2.
#
# Restores the v1 install state: the whole v1 bundle (ditto'd back from
# the install snapshot, SHA-verified against the backup manifest) and
# the v1 user LaunchAgent plist, and removes the v2 external agent
# script (moved into the backup dir first, so it is preserved, not
# deleted). The root daemon plist stays absent (v1 state); the stale
# crash-looping job's plist file is left exactly as found (this install
# never touched it).
#
# The bundle is deleted ONLY after its Info.plist proves it carries
# this wrapper's bundle identifier. A foreign or partial bundle is
# never deleted by this script: it dies first and a human resolves it.
#
# GRANTS NOTE: the restored v1 bundle is the EXACT bundle that was
# granted (same seal, snapshotted whole by ditto), so its Screen
# Recording + Accessibility grants should still be in place — no
# re-grant is expected after a rollback. Verify screenshots after the
# rollback; if they fail, re-grant for the v1 bundle (tccutil reset +
# toggles, FINGER-STEPS-v2.md step 3) and tell kavi7.
#
# Usage: sudo bash rollback-v2.sh --go [backup-dir]
# With no backup dir given, the newest app-v2-backup-* dir is used.
# Run in the FOREGROUND (see install-v2.sh header for why).

set -u
PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

BUNDLE_ID="com.seth.rvgmac"
JOB_LABEL="com.seth.rvg"

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
LIVE_AGENT_SCRIPT="$AGENT_DIR/rvg_mac.py"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

[ "${1:-}" = "--go" ] || { echo "usage: $0 --go [backup-dir] (refuses to run without --go)" >&2; exit 2; }
if [ -z "${MACAPP_SIM:-}" ]; then
    [ "$(id -u)" = "0" ] || die "must run as root (sudo); refusing without root"
    [ "$CONSOLE_USER" = "sethkellner" ] || die "console user is '$CONSOLE_USER', expected 'sethkellner'; refusing"
fi

if [ -n "${2:-}" ]; then
    BACKUP_DIR="$2"
else
    BACKUP_DIR="$(ls -dt "$BACKUP_PARENT"/app-v2-backup-* 2>/dev/null | head -1)"
fi
[ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ] || die "no backup dir found under $BACKUP_PARENT (looked for app-v2-backup-*)"
[ -f "$BACKUP_DIR/backup-manifest.sha256" ] || die "backup manifest missing in $BACKUP_DIR; refusing (nothing restored)"
[ -d "$BACKUP_DIR/RVG Mac.app" ] || die "backup v1 bundle missing in $BACKUP_DIR; refusing (nothing restored)"
log "rollback backup dir: $BACKUP_DIR"

# ---- pre-flight: every manifest lookup must hit, every SHA must match.
# NOTHING is restored until all of it checks out. ----
manifest_hash() {
    # $1 = path relative to the backup dir; prints the manifest SHA or dies.
    local line
    line="$(grep -F "  $1" "$BACKUP_DIR/backup-manifest.sha256" | head -1)"
    [ -n "$line" ] || die "manifest lookup missed for '$1'; refusing (nothing restored)"
    printf '%s' "$line" | awk '{print $1}'
}
sha_of() { shasum -a 256 "$1" | awk '{print $1}'; }
[ "$(sha_of "$BACKUP_DIR/RVG Mac.app/Contents/Info.plist")" = "$(manifest_hash "RVG Mac.app/Contents/Info.plist")" ] \
    || die "backup v1 bundle Info.plist SHA mismatch; refusing (nothing restored)"
[ "$(sha_of "$BACKUP_DIR/RVG Mac.app/Contents/MacOS/RVGMac")" = "$(manifest_hash "RVG Mac.app/Contents/MacOS/RVGMac")" ] \
    || die "backup v1 bundle launcher SHA mismatch; refusing (nothing restored)"
[ "$(sha_of "$BACKUP_DIR/RVG Mac.app/Contents/Resources/rvg_mac.py")" = "$(manifest_hash "RVG Mac.app/Contents/Resources/rvg_mac.py")" ] \
    || die "backup v1 bundled agent SHA mismatch; refusing (nothing restored)"
[ "$(sha_of "$BACKUP_DIR/com.seth.rvg.plist")" = "$(manifest_hash com.seth.rvg.plist)" ] \
    || die "backup plist SHA mismatch; refusing (nothing restored)"
log "pre-flight verified: all manifest lookups hit, all backup SHAs match"

# ---- stop the v2 job ----
launchctl bootout "gui/501/$JOB_LABEL" >/dev/null 2>&1 || true
for _ in 1 2 3 4 5; do
    launchctl print "gui/501/$JOB_LABEL" >/dev/null 2>&1 || break
    sleep 1
done
launchctl print "gui/501/$JOB_LABEL" >/dev/null 2>&1 && die "v2 job still loaded after bootout; refusing (nothing restored)"

# ---- remove the v2 bundle, ONLY if it proves to be ours ----
if [ -e "$BUNDLE_DIR" ]; then
    FOUND_ID="$(plutil -extract CFBundleIdentifier raw "$BUNDLE_DIR/Contents/Info.plist" 2>/dev/null || echo MISSING)"
    [ "$FOUND_ID" = "$BUNDLE_ID" ] \
        || die "bundle at $BUNDLE_DIR has identifier '$FOUND_ID', not '$BUNDLE_ID'; refusing to delete a foreign bundle (nothing restored)"
    rm -rf "$BUNDLE_DIR" || die "cannot remove bundle dir $BUNDLE_DIR"
    [ ! -e "$BUNDLE_DIR" ] || die "bundle dir still present after removal"
    log "v2 bundle removed: $BUNDLE_DIR"
else
    log "bundle already absent: $BUNDLE_DIR"
fi

# ---- move the v2 external agent script aside (preserved, not deleted) ----
if [ -f "$LIVE_AGENT_SCRIPT" ]; then
    mkdir -p "$BACKUP_DIR/removed-agent"
    cp "$LIVE_AGENT_SCRIPT" "$BACKUP_DIR/removed-agent/rvg_mac.py" || die "cannot preserve external agent script"
    rm -f "$LIVE_AGENT_SCRIPT" || die "cannot remove external agent script"
    log "v2 external agent script preserved at $BACKUP_DIR/removed-agent/rvg_mac.py and removed from $AGENT_DIR"
    # Also remove the .prev + update staging leftovers so a later v2
    # attempt starts clean (the preserved copy above is the record).
    rm -f "$AGENT_DIR/rvg_mac.py.prev" "$AGENT_DIR/rvg_mac.py.tmp" 2>/dev/null || true
else
    log "no external agent script present (already absent)"
fi

# ---- restore the v1 bundle + plist, SHA-verified after each restore ----
ditto "$BACKUP_DIR/RVG Mac.app" "$BUNDLE_DIR" || die "cannot restore v1 bundle"
[ "$(sha_of "$BUNDLE_DIR/Contents/Info.plist")" = "$(manifest_hash "RVG Mac.app/Contents/Info.plist")" ] \
    || die "restored v1 bundle Info.plist SHA mismatch"
[ "$(sha_of "$BUNDLE_DIR/Contents/MacOS/RVGMac")" = "$(manifest_hash "RVG Mac.app/Contents/MacOS/RVGMac")" ] \
    || die "restored v1 bundle launcher SHA mismatch"
[ "$(sha_of "$BUNDLE_DIR/Contents/Resources/rvg_mac.py")" = "$(manifest_hash "RVG Mac.app/Contents/Resources/rvg_mac.py")" ] \
    || die "restored v1 bundled agent SHA mismatch"
codesign --verify --deep --strict "$BUNDLE_DIR" >/dev/null 2>&1 \
    || die "restored v1 bundle fails codesign --verify"
log "v1 bundle restored and SHA-verified"
cp "$BACKUP_DIR/com.seth.rvg.plist" "$LIVE_PLIST" || die "cannot restore live plist"
chmod 644 "$LIVE_PLIST" || die "cannot chmod restored plist"
[ "$(sha_of "$LIVE_PLIST")" = "$(manifest_hash com.seth.rvg.plist)" ] \
    || die "restored plist SHA mismatch"
chown "$CONSOLE_USER" "$LIVE_PLIST" 2>/dev/null || log "WARN: chown user agent plist failed; continuing (root-owned plist is acceptable)"
log "v1 plist restored and SHA-verified"

# ---- re-bootstrap the v1 job ----
launchctl bootstrap "gui/501" "$LIVE_PLIST" \
    || die "launchctl bootstrap failed for the restored job"
for _ in 1 2 3 4 5; do
    launchctl print "gui/501/$JOB_LABEL" >/dev/null 2>&1 && break
    sleep 1
done
launchctl print "gui/501/$JOB_LABEL" >/dev/null 2>&1 \
    || die "restored job is not loaded after bootstrap"
launchctl print "gui/501/$JOB_LABEL" 2>/dev/null | grep -Fq "RVGMac" \
    || log "WARN: restored job program line does not show RVGMac; check launchctl print output"
if [ -e "$DAEMON_PLIST" ]; then
    log "WARN: root daemon plist is present (expected absent in v1 state); left untouched"
else
    log "root daemon plist still absent (v1 state)"
fi

log "=== rollback complete ==="
log "state: v1 bundle + v1 user LaunchAgent restored; v2 external agent script preserved in the backup dir; stale job file untouched"
log "next: run verify-v1.sh --with-input (screenshots should still work — the restored v1 bundle carries its original seal/grants)"
