/* RVGMac launcher — v2 (loader pattern).
 *
 * Purpose: give the RVG Mac agent a real application identity. This tiny
 * native executable lives at Contents/MacOS/RVGMac inside the "RVG Mac"
 * .app bundle and execs the agent script under one fixed, verified
 * interpreter. No shell is involved, no PATH lookup happens, and there
 * is no fallback interpreter: if anything is off, this exits nonzero
 * with a message on stderr and the agent does not start.
 *
 * v2 CHANGE (loader pattern — the whole point of v2): the agent script
 * is NO LONGER inside the bundle. It lives OUTSIDE it, at
 * /Users/sethkellner/rvd-mac/agent/rvg_mac.py, and this launcher execs
 * that external script. The bundle now contains only this launcher +
 * Info.plist (+ icon, if any). The bundle's code signature therefore
 * covers only files that never change, so the seal never changes, so
 * the Screen Recording and Accessibility grants survive agent-script
 * updates. The agent self-updates the external script in place
 * (check -> download -> SHA-verify -> atomic replace -> restart);
 * no re-sign, no re-grant, no finger step for a script update.
 *
 * NO FALLBACK to a bundled script: the bundle carries no
 * Resources/rvg_mac.py at all in v2. If the external script is missing
 * or unreadable, this exits nonzero (fail closed). A missing external
 * script is a loud failure (the agent does not start; launchd retries),
 * never a silent run of a stale bundled copy. If the agent ever
 * crash-loops on launch, check the external script first
 * (~/rvd-mac/agent/rvg_mac.py) — the previous version is kept at
 * ~/rvd-mac/agent/rvg_mac.py.prev.
 *
 * Why exec (not spawn): exec replaces this process image, so the agent
 * keeps this process's PID, audit token, and launchd supervision, and
 * signals go straight to the agent. There is no wrapper process left
 * behind to confuse process attribution.
 *
 * The interpreter path below is the exact binary the live agent runs as
 * (verified from the live `ps` output, 2026-10-29/30: the /usr/bin/python3
 * stub re-execs to this Xcode Python.app binary). It is pinned on
 * purpose: a different interpreter would be a different identity chain.
 * If Xcode moves it, the BUILD fails closed (the install script checks
 * the path before compiling) instead of silently running something else.
 *
 * The external script path is hardcoded to the console user's home
 * (sethkellner), consistent with the agent's own hardcoded
 * CONSOLE_USER / RVD_DIR. It is NOT derived from $HOME (environment
 * variables can be manipulated) and NOT derived from the bundle
 * location (the script is deliberately outside the bundle). The
 * install script verifies the external script exists and matches its
 * staged SHA before the job is (re)started.
 *
 * TCC attribution note (UNPROVEN until the v2 pilot): the live v1
 * verify proved attribution works when the script is INSIDE the bundle
 * (launcher execs Python with the bundled script; screenshots 3/3).
 * With an external script the process chain is identical (launcher ->
 * Python -> screencapture) and the script is data to the interpreter,
 * so attribution *should* be the same — but "should" is not "proven."
 * The v2 pilot (FINGER-STEPS-v2.md step 5) is the arbiter: after
 * install + re-grant + verify PASS, a self-update to a newer script
 * version must leave screenshots working WITHOUT a re-grant. If it
 * does not, the loader pattern is dead and v1 (bundled script) is
 * restored via rollback-v2.sh.
 *
 * No dependencies beyond libSystem. No network. No writes.
 */

#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <limits.h>

/* Verified interpreter (live ps, PID 56000, 2026-10-29). Do not change
 * without re-verifying from a live process listing. */
static const char kInterpreter[] = "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python";

/* v2: the agent script lives OUTSIDE the bundle, in the console user's
 * agent dir. Hardcoded (not $HOME-derived) for the same reason the
 * interpreter is pinned: a different script would be a different agent.
 * The install script places this file and SHA-verifies it before the
 * job starts; the agent self-updates it in place (atomic replace). */
static const char kAgentScript[] = "/Users/sethkellner/rvd-mac/agent/rvg_mac.py";

int main(void) {
    if (access(kInterpreter, X_OK) != 0) {
        fprintf(stderr, "RVGMac: interpreter missing or not executable: %s\n", kInterpreter);
        return 1;
    }
    if (access(kAgentScript, R_OK) != 0) {
        fprintf(stderr, "RVGMac: external agent missing or unreadable: %s\n", kAgentScript);
        fprintf(stderr, "RVGMac: check ~/rvd-mac/agent/rvg_mac.py (previous version at rvg_mac.py.prev); no bundled fallback exists in v2\n");
        return 1;
    }

    execl(kInterpreter, kInterpreter, kAgentScript, (char *)NULL);
    perror("RVGMac: exec failed");
    return 1;
}
