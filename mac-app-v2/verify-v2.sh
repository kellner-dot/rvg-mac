#!/bin/bash
# RVG Mac .app wrapper verify — v2 (read-only checks, plus an optional
# two-move input test). No installs, no writes outside its own temp
# dir, no rollback behavior. Run as the console user (no sudo).
#
# Usage: bash verify-v2.sh [--with-input]
# --with-input sends EXACTLY two `move` operations, (400,400) then
# (800,400), and nothing else: no click, no key, no text. A human must
# eyeball the cursor: ok:true alone is not proof a move happened.
#
# v2 checks (loader layout): the bundle carries NO Resources/rvg_mac.py;
# the agent process runs the EXTERNAL script
# (~/rvd-mac/agent/rvg_mac.py); /rvd/status reports version 1.2.0,
# loaderLayout true, and the update fields. The bundled-script ps
# check from v1 (B1) is replaced: the exec'd process argv now shows
# the external script path (interpreter + external script), because
# the v2 launcher execs the external script.
#
# The token file is read for HTTP headers only, never printed, logged,
# or stored, and unset immediately after use. If it is unreadable, the
# HTTP checks are SKIPPED and the result is PARTIAL, never a false PASS.

set -u
PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

BUNDLE_ID="com.seth.rvgmac"
JOB_LABEL="com.seth.rvg"
BUNDLE_DIR="/Applications/RVG Mac.app"
AGENT_DIR="$HOME/rvd-mac/agent"
AGENT_SCRIPT="$AGENT_DIR/rvg_mac.py"
AGENT_URL="http://127.0.0.1:8899"
TOKEN_FILE="$HOME/rvd-mac/token.txt"
# Sim-only overrides (honored ONLY when MACAPP_SIM is set; a real run
# never silently uses an environment-redirected path):
if [ -n "${MACAPP_SIM:-}" ]; then
  BUNDLE_DIR="${MACAPP_BUNDLE_DIR:-$BUNDLE_DIR}"
  AGENT_DIR="${MACAPP_AGENT_DIR:-$AGENT_DIR}"
  AGENT_SCRIPT="${MACAPP_AGENT_SCRIPT:-$AGENT_SCRIPT}"
  AGENT_URL="${MACAPP_AGENT_URL:-$AGENT_URL}"
  TOKEN_FILE="${MACAPP_TOKEN_FILE:-$TOKEN_FILE}"
fi
WITH_INPUT=0
[ "${1:-}" = "--with-input" ] && WITH_INPUT=1

FAILURES=0
WARNINGS=0
pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
warn() { printf 'WARN: %s\n' "$*"; WARNINGS=$((WARNINGS + 1)); }

# ---- bundle identity ----
if [ -d "$BUNDLE_DIR" ]; then
    pass "bundle present: $BUNDLE_DIR"
else
    fail "bundle missing: $BUNDLE_DIR"
fi
if plutil -lint "$BUNDLE_DIR/Contents/Info.plist" >/dev/null 2>&1; then
    FOUND_ID="$(plutil -extract CFBundleIdentifier raw "$BUNDLE_DIR/Contents/Info.plist" 2>/dev/null || echo MISSING)"
    if [ "$FOUND_ID" = "$BUNDLE_ID" ]; then
        pass "Info.plist identifier matches: $BUNDLE_ID"
    else
        fail "Info.plist identifier is '$FOUND_ID', expected '$BUNDLE_ID'"
    fi
    FOUND_VER="$(plutil -extract CFBundleShortVersionString raw "$BUNDLE_DIR/Contents/Info.plist" 2>/dev/null || echo MISSING)"
    if [ "$FOUND_VER" = "1.2.0" ]; then
        pass "bundle version is 1.2.0 (v2)"
    else
        fail "bundle version is '$FOUND_VER', expected '1.2.0' (v2)"
    fi
else
    fail "Info.plist fails plutil -lint"
fi
if codesign --verify --deep --strict "$BUNDLE_DIR" >/dev/null 2>&1; then
    pass "codesign --verify passes on the placed bundle"
else
    fail "codesign --verify fails on the placed bundle"
fi
REQ="$(codesign -d -r- "$BUNDLE_DIR" 2>&1 || echo NONE)"
if printf '%s' "$REQ" | grep -Fq "identifier \"$BUNDLE_ID\""; then
    if printf '%s' "$REQ" | grep -Fq "certificate leaf"; then
        pass "designated requirement names $BUNDLE_ID with a certificate leaf"
    else
        fail "designated requirement has no certificate leaf (ad-hoc seal?)"
    fi
else
    fail "designated requirement does not name $BUNDLE_ID"
fi
if file "$BUNDLE_DIR/Contents/MacOS/RVGMac" 2>/dev/null | grep -q "Mach-O"; then
    pass "launcher is a Mach-O executable"
else
    fail "launcher is not a Mach-O executable"
fi
# v2 loader layout: the bundle must NOT contain a bundled agent script.
if [ -e "$BUNDLE_DIR/Contents/Resources/rvg_mac.py" ]; then
    fail "bundle contains Resources/rvg_mac.py (v1 layout); v2 must have no bundled script"
else
    pass "bundle has no Resources/rvg_mac.py (v2 loader layout)"
fi

# ---- external agent script ----
if [ -f "$AGENT_SCRIPT" ]; then
    pass "external agent script present: $AGENT_SCRIPT"
    if [ -r "$AGENT_SCRIPT" ]; then
        pass "external agent script is readable"
    else
        fail "external agent script is not readable"
    fi
    if grep -Fq 'VERSION = "1.2.0"' "$AGENT_SCRIPT" 2>/dev/null; then
        pass "external agent script is version 1.2.0"
    else
        warn "external agent script VERSION is not 1.2.0 (it may have self-updated to a newer version — check /rvd/status)"
    fi
    if python3 -m py_compile "$AGENT_SCRIPT" 2>/dev/null; then
        pass "external agent script passes py_compile"
    else
        fail "external agent script fails py_compile"
    fi
else
    fail "external agent script missing: $AGENT_SCRIPT (the v2 launcher cannot start without it)"
fi

# ---- launchd state ----
if launchctl print "gui/501/$JOB_LABEL" >/dev/null 2>&1; then
    pass "user agent gui/501/$JOB_LABEL is running"
else
    fail "user agent gui/501/$JOB_LABEL is not loaded"
fi
if launchctl print "system/$JOB_LABEL" >/dev/null 2>&1; then
    fail "root daemon system/$JOB_LABEL is present (expected absent)"
else
    pass "root daemon system/$JOB_LABEL is absent"
fi
if launchctl print "gui/501/com.kavi.rvd-mac" >/dev/null 2>&1; then
    fail "stale job gui/501/com.kavi.rvd-mac is loaded (expected absent)"
else
    pass "stale job gui/501/com.kavi.rvd-mac is absent"
fi
# v2: the exec'd process argv shows interpreter + EXTERNAL script path
# (the v2 launcher execs the external script; B1's lesson applies: grep
# for the path that actually appears in argv, not the launcher path).
if ps -axo command 2>/dev/null | grep -F "rvd-mac/agent/rvg_mac.py" | grep -v grep >/dev/null; then
    pass "agent process runs the external script (rvd-mac/agent/rvg_mac.py)"
else
    fail "no process runs the external script (ps check)"
fi
if ps -axo command 2>/dev/null | grep -F "RVG Mac.app/Contents/Resources/rvg_mac.py" | grep -v grep >/dev/null; then
    fail "a process runs the BUNDLED script (v1 layout); v2 must run the external script"
else
    pass "no process runs the bundled script (v1 layout absent)"
fi

# ---- HTTP checks (token for headers only) ----
TOKEN=""
HTTP_OK=0
if [ -r "$TOKEN_FILE" ]; then
    TOKEN="$(cat "$TOKEN_FILE" 2>/dev/null || echo "")"
fi
if [ -n "$TOKEN" ]; then
    TMPD="$(mktemp -d)"
    CODE="$(curl -sS -m 15 -o "$TMPD/status.json" -w '%{http_code}' -H "X-RVD-Token: $TOKEN" "$AGENT_URL/rvd/status" 2>/dev/null || echo 000)"
    # v2 line check: any 1.2.x (the agent self-updates, so the version
    # may be newer than the installed 1.2.0 — e.g. 1.2.1 after the pilot).
    # The bundle version check above is the fixed 1.2.0 (the bundle
    # never changes); the agent version is allowed to have moved on.
    LIVE_VER="$(grep -o '"version": *"[^"]*"' "$TMPD/status.json" 2>/dev/null | head -1 | sed 's/.*"version": *"\([^"]*\)".*/\1/')"
    if [ "$CODE" = "200" ] && grep -q '"ok": *true' "$TMPD/status.json" 2>/dev/null \
        && printf '%s' "$LIVE_VER" | grep -q '^1\.2\.'; then
        pass "GET /rvd/status 200 ok:true version $LIVE_VER (v2 line): $(grep -o '"quartz": *[a-z]*' "$TMPD/status.json" | head -1)"
    else
        fail "GET /rvd/status returned HTTP $CODE version '$LIVE_VER' (expected 200 ok:true version 1.2.x)"
    fi
    if grep -q '"loaderLayout": *true' "$TMPD/status.json" 2>/dev/null; then
        pass "/rvd/status loaderLayout true (external script)"
    else
        fail "/rvd/status loaderLayout is not true (expected the v2 loader layout)"
    fi
    if grep -q '"updateAvailable"' "$TMPD/status.json" 2>/dev/null \
        && grep -q '"updateVersion"' "$TMPD/status.json" 2>/dev/null \
        && grep -q '"updateError"' "$TMPD/status.json" 2>/dev/null \
        && grep -q '"lastUpdateCheckMs"' "$TMPD/status.json" 2>/dev/null \
        && grep -q '"lastUpdateResult"' "$TMPD/status.json" 2>/dev/null; then
        pass "/rvd/status carries the self-update fields (updateAvailable/updateVersion/updateError/lastUpdateCheckMs/lastUpdateResult)"
    else
        fail "/rvd/status is missing one or more self-update fields"
    fi
    BADCODE="$(curl -sS -m 15 -o /dev/null -w '%{http_code}' -H "X-RVD-Token: probe-invalid-token" "$AGENT_URL/rvd/status" 2>/dev/null || echo 000)"
    if [ "$BADCODE" = "401" ]; then
        pass "bad-token probe correctly 401 (auth enforced, errors honest)"
    else
        fail "bad-token probe returned HTTP $BADCODE (expected 401)"
    fi
    SHOT_FAIL=0
    for i in 1 2 3; do
        CODE="$(curl -sS -m 20 -o "$TMPD/shot$i.bin" -w '%{http_code}' -H "X-RVD-Token: $TOKEN" "$AGENT_URL/rvd/shot" 2>/dev/null || echo 000)"
        MAGIC="$(head -c 4 "$TMPD/shot$i.bin" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
        if [ "$CODE" = "200" ] && { [ "$MAGIC" = "89504e47" ] || [ "$MAGIC" = "ffd8ffe0" ] || [ "$MAGIC" = "ffd8ffe1" ]; }; then
            SZ="$(wc -c <"$TMPD/shot$i.bin" | tr -d ' ')"
            printf 'shot %s: HTTP 200, %s bytes, valid image magic\n' "$i" "$SZ"
        else
            printf 'shot %s: HTTP %s, magic %s (expected 200 + PNG/JPEG magic)\n' "$i" "$CODE" "$MAGIC"
            SHOT_FAIL=1
        fi
    done
    if [ "$SHOT_FAIL" = "0" ]; then
        pass "screenshots 3/3 valid images"
    else
        fail "screenshots 0/3 or partial valid images (Screen Recording grant missing or capture failing)"
    fi
    if [ "$WITH_INPUT" = "1" ]; then
        printf 'EYEBALL CHECK: watch the cursor. Two moves follow: (400,400) then (800,400).\n'
        R1="$(curl -sS -m 15 -H "X-RVD-Token: $TOKEN" -H 'Content-Type: application/json' \
            -d '{"type":"move","x":400,"y":400}' "$AGENT_URL/rvd/input" 2>/dev/null || echo '{}')"
        if printf '%s' "$R1" | grep -q '"ok": *true'; then
            pass "move 1 response ok:true (eyeball confirmation still required)"
        else
            fail "move 1 response not ok:true: $R1"
        fi
        R2="$(curl -sS -m 15 -H "X-RVD-Token: $TOKEN" -H 'Content-Type: application/json' \
            -d '{"type":"move","x":800,"y":400}' "$AGENT_URL/rvd/input" 2>/dev/null || echo '{}')"
        if printf '%s' "$R2" | grep -q '"ok": *true'; then
            pass "move 2 response ok:true (eyeball confirmation still required)"
        else
            fail "move 2 response not ok:true: $R2"
        fi
        printf 'EYEBALL: confirm you SAW the cursor move. ok:true alone is not proof.\n'
    fi
    rm -rf "$TMPD"
    HTTP_OK=1
else
    warn "token file unreadable; HTTP checks SKIPPED (result PARTIAL)"
fi
unset TOKEN

if [ "$HTTP_OK" = "0" ]; then
    printf 'VERIFY RESULT: PARTIAL (%s failure(s), %s warning(s))\n' "$FAILURES" "$WARNINGS"
elif [ "$FAILURES" = "0" ]; then
    printf 'VERIFY RESULT: PASS (%s warning(s))\n' "$WARNINGS"
else
    printf 'VERIFY RESULT: FAIL (%s failure(s), %s warning(s))\n' "$FAILURES" "$WARNINGS"
fi
[ "$FAILURES" = "0" ]
