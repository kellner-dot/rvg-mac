"""Publish an RVG Mac agent version as a Gitea release on
kellner-dot/rvg-mac (PUBLIC).

Usage:
  python3 publish_release_v2.py [--version 1.2.0]
      [--script rvg_mac.py] [--manifest rvg_mac.py.sha256]

Defaults publish v1.2.0 from the staged files. For the v2 pilot, publish
v1.2.1 from the pilot files (after the v2 install + verify PASS):
  python3 publish_release_v2.py --version 1.2.1 \\
      --script pilot/rvg_mac-1.2.1.py --manifest pilot/rvg_mac-1.2.1.py.sha256

Single invocation: preconditions -> sync the mac-app-v2/ package into
the rvg-mac worktree -> commit + push to rvg-mac main -> tag v<version>
at that commit -> push tag -> create release via Gitea API -> upload
the agent script + SHA-256 manifest assets -> verify via the exact
channel the Mac agent's updater checks (anonymous GET releases/latest)
-> SHA-match the downloaded asset -> shred creds.

WHY rvg-mac (not kavibot-readiness): the release channel must be
readable ANONYMOUSLY (the agent checks with no token, no secrets —
mirroring the Razr's rvg-android channel). kellner-dot/rvg-android is
public, so the Razr's anonymous check works. kellner-dot/kavibot-readiness
is PRIVATE, so an anonymous GET returns 404 (proven Nov 6, 2026: the
v1.2.0 release was created there first, id 33, and anonymous
releases/latest 404'd while authenticated 200'd). kellner-dot/rvg-mac
is the dedicated PUBLIC home for the Mac agent (created Oct 30, 2026;
v1.0.0-era code), so it is the release channel.

Channel (the v2 agent's self-update, rvg_mac.py _update_fetch_latest):
  GET https://kavivpn.tail1cffd9.ts.net/api/v1/repos/kellner-dot/rvg-mac/releases/latest
Anonymous read, no auth. Assets rvg_mac.py + rvg_mac.py.sha256, version
compare strictly greater, SHA-256 manifest gate.

The Mac agent (v2, loader layout) checks this channel on launch + once
daily and self-updates the external script at
~/rvd-mac/agent/rvg_mac.py. The v1 agent (bundled script, no update
code) never checks, so publishing v1.2.0 is safe before the v2 install:
nothing will self-update until the v2 agent is installed and running.

Seth's word: Nov 6, 2026 ("Build Phase 2: loader pattern for true
zero-finger updates"). Token fetched transiently per run, never printed;
creds file shredded. The release body carries the agent SHA so the
channel is auditable from the release page alone.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.expanduser("~/workspace/gh-mirror"))
from gitea import fetch_gitea_token

# Source of truth for the staged files (development home).
SRC_REPO = os.path.expanduser("~/workspace/kavibot-readiness")
PKG = os.path.join(SRC_REPO, "round-2026-10-28", "rvg-fix", "mac-app-v2")
# Release channel repo (public) + its local worktree.
REL_REPO = "kellner-dot/rvg-mac"
REL_WORKTREE = os.path.expanduser("~/workspace/rvg-mac")

ap = argparse.ArgumentParser()
ap.add_argument("--version", default="1.2.0")
ap.add_argument("--script", default="rvg_mac.py")
ap.add_argument("--manifest", default="rvg_mac.py.sha256")
args = ap.parse_args()

VERSION = args.version
TAG = "v" + VERSION
SCRIPT = args.script if os.path.isabs(args.script) else os.path.join(PKG, args.script)
MANIFEST = args.manifest if os.path.isabs(args.manifest) else os.path.join(PKG, args.manifest)
CREDS = "/dev/shm/rvgmac-v2-publish-creds.txt"
PROXY = "http://hatch-egress-proxy:3130"
API = "https://kavivpn.tail1cffd9.ts.net/api/v1"
DL = "/dev/shm/rvgmac-v2-verify-download.py"
DL_MAN = "/dev/shm/rvgmac-v2-verify-download.sha256"


def run(cmd, env=None, cwd=SRC_REPO, timeout=180):
    p = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                       capture_output=True, text=True)
    print("$ %s -> rc=%d" % (" ".join(cmd), p.returncode))
    if p.stdout.strip():
        print(p.stdout.strip()[-600:])
    if p.stderr.strip():
        print("ERR: " + p.stderr.strip()[-400:])
    return p


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))


def api_req(method, path, token, body=None, raw_bytes=None, ctype=None):
    headers = {"Authorization": "token %s" % token,
               "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif raw_bytes is not None:
        data = raw_bytes
        headers["Content-Type"] = ctype or "application/octet-stream"
    req = urllib.request.Request(API + path, data=data, headers=headers,
                                 method=method)
    try:
        with opener().open(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def anon_get(path):
    req = urllib.request.Request(API + path,
                                 headers={"Accept": "application/json"},
                                 method="GET")
    try:
        with opener().open(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def anon_download(url, out_path):
    req = urllib.request.Request(url,
                                 headers={"User-Agent": "KaviSelfUpdate/1.0"},
                                 method="GET")
    with opener().open(req, timeout=300) as resp:
        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)


def fail(msg):
    print("FAIL: " + msg)
    sys.exit(1)


# ---- 0. preconditions (no creds) ----
print("== preconditions ==")
for f in (SCRIPT, MANIFEST):
    if not os.path.isfile(f):
        fail("missing staged file: %s" % f)
with open(SCRIPT) as f:
    src = f.read()
if 'VERSION = "%s"' % VERSION not in src:
    fail("staged rvg_mac.py is not version %s" % VERSION)
if 'UPDATE_REPO = "%s"' % REL_REPO not in src:
    fail("staged agent UPDATE_REPO is not %s (wrong release channel)" % REL_REPO)
got_sha = sha256(SCRIPT)
got_size = os.path.getsize(SCRIPT)
print("agent sha=%s size=%d" % (got_sha, got_size))
with open(MANIFEST) as f:
    man_text = f.read().strip()
man_parts = man_text.split()
if len(man_parts) < 2 or man_parts[0] != got_sha or \
        man_parts[1].lstrip("*") != "rvg_mac.py":
    fail("manifest %s does not match the staged script SHA" % MANIFEST)
print("manifest matches staged script SHA")
p = run(["git", "rev-parse", "HEAD"])
src_commit = p.stdout.strip()
if p.returncode != 0 or not src_commit:
    fail("cannot resolve source HEAD commit")
print("source HEAD commit (kavibot-readiness): %s" % src_commit)
p = run(["git", "status", "--short", "round-2026-10-28/rvg-fix/mac-app-v2/"])
if p.stdout.strip():
    fail("mac-app-v2/ has uncommitted changes; commit first")
if not os.path.isdir(os.path.join(REL_WORKTREE, ".git")):
    fail("rvg-mac worktree missing: %s (clone it first)" % REL_WORKTREE)
p = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REL_WORKTREE)
if p.stdout.strip() != "main":
    fail("rvg-mac worktree is on branch %r, expected 'main'" % p.stdout.strip())
p = run(["git", "status", "--short"], cwd=REL_WORKTREE)
if p.stdout.strip():
    fail("rvg-mac worktree is dirty; resolve first")
status, body_bytes = anon_get("/repos/%s/releases/tags/%s" % (REL_REPO, TAG))
print("existing release %s: http=%d" % (TAG, status))
if status == 200:
    fail("release %s already exists on %s" % (TAG, REL_REPO))

BODY = (
    "RVG Mac agent v" + VERSION + " — loader layout + self-update (Phase 2).\n\n"
    "The agent script now lives OUTSIDE the .app bundle, at\n"
    "~/rvd-mac/agent/rvg_mac.py, and the bundle launcher execs it from\n"
    "there. The bundle contains only the launcher + Info.plist, so its\n"
    "seal never changes and the Screen Recording / Accessibility grants\n"
    "survive script updates. The agent self-updates the external script\n"
    "(Gitea releases/latest check on launch + daily, SHA-256 manifest\n"
    "verify, atomic replace, restart via launchd KeepAlive). Install is\n"
    "a one-time rebuild + re-sign + re-grant (install-v2.sh); after that,\n"
    "script updates need no finger step. The TCC-attribution pilot\n"
    "(external-script update preserves the grants) is the arbiter — see\n"
    "FINGER-STEPS-v2.md step 5. If the pilot fails, rollback-v2.sh\n"
    "restores the v1 bundled layout.\n\n"
    "Source: kellner-dot/kavibot-readiness %s (mac-app-v2/).\n"
    "Agent SHA-256: %s\n"
    "Agent size: %d bytes\n" % (src_commit[:12], got_sha, got_size)
)

token = fetch_gitea_token()
assert len(token) == 40 and all(c in "0123456789abcdef" for c in token), "bad token shape"
try:
    env = dict(os.environ)
    env["https_proxy"] = PROXY
    env["http_proxy"] = PROXY
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    with open(CREDS, "w") as f:
        os.chmod(CREDS, 0o600)
        f.write("https://kavi7:%s@kavivpn.tail1cffd9.ts.net\n" % token)
    cfg = ["-c", "credential.helper=store --file=%s" % CREDS]

    # ---- 1. sync the package into rvg-mac + commit + push ----
    print("== sync package into rvg-mac ==")
    dest = os.path.join(REL_WORKTREE, "mac-app-v2")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.copytree(PKG, dest, ignore=shutil.ignore_patterns("__pycache__"))
    p = run(["git", "add", "mac-app-v2"], cwd=REL_WORKTREE, env=env)
    if p.returncode != 0:
        fail("git add mac-app-v2 failed")
    p = run(["git", "status", "--short"], cwd=REL_WORKTREE)
    if not p.stdout.strip():
        # Already synced: the copy produced no diff, which means the
        # committed mac-app-v2/ content in rvg-mac is byte-identical to
        # the staged package. (The pilot files were synced during the
        # v1.2.0 publish, when the whole package was copied.) That is
        # safe to release from, but only after verifying the committed
        # content is the right version/channel and its manifest matches
        # the staged agent SHA. Proceed from the existing HEAD.
        print("sync produced no changes; verifying committed content matches staged files")
        rel_script = os.path.join(
            "mac-app-v2", os.path.relpath(SCRIPT, PKG))
        rel_manifest = os.path.join(
            "mac-app-v2", os.path.relpath(MANIFEST, PKG))
        q = run(["git", "show", "HEAD:%s" % rel_script], cwd=REL_WORKTREE)
        if q.returncode != 0:
            fail("committed agent script %s missing from rvg-mac HEAD" % rel_script)
        if 'VERSION = "%s"' % VERSION not in q.stdout:
            fail("committed agent script is not version %s" % VERSION)
        if 'UPDATE_REPO = "%s"' % REL_REPO not in q.stdout:
            fail("committed agent UPDATE_REPO is not %s" % REL_REPO)
        q = run(["git", "show", "HEAD:%s" % rel_manifest], cwd=REL_WORKTREE)
        if q.returncode != 0:
            fail("committed manifest %s missing from rvg-mac HEAD" % rel_manifest)
        man_parts = q.stdout.strip().split()
        if len(man_parts) < 2 or man_parts[0] != got_sha or \
                man_parts[1].lstrip("*") != "rvg_mac.py":
            fail("committed manifest does not match staged agent SHA")
        print("committed content matches staged files (version %s, repo %s, SHA %s)"
              % (VERSION, REL_REPO, got_sha[:12]))
        p = run(["git", "rev-parse", "HEAD"], cwd=REL_WORKTREE)
        rel_commit = p.stdout.strip()
        print("using existing rvg-mac HEAD: %s" % rel_commit)
        # The remote must already have this HEAD (it was pushed by the
        # earlier publish). Verify; do not push a no-op.
        p = run(["git"] + cfg + ["ls-remote", "origin", "HEAD"],
                cwd=REL_WORKTREE, env=env)
        remote_head = p.stdout.split()[0] if p.stdout.split() else ""
        print("remote HEAD=%s local HEAD=%s"
              % (remote_head[:12], rel_commit[:12]))
        if remote_head != rel_commit:
            fail("remote rvg-mac HEAD %s != local HEAD %s; push main first"
                 % (remote_head, rel_commit))
    else:
        p = run(["git", "-c", "user.name=kavi7", "-c", "user.email=kavi7@local",
                 "commit", "-m",
                 "RVG Mac agent v%s (loader + self-update); source kavibot-readiness %s"
                 % (VERSION, src_commit[:12])],
                cwd=REL_WORKTREE, env=env)
        if p.returncode != 0:
            fail("rvg-mac commit failed")
        p = run(["git", "rev-parse", "HEAD"], cwd=REL_WORKTREE)
        rel_commit = p.stdout.strip()
        print("rvg-mac commit: %s" % rel_commit)
        p = run(["git"] + cfg + ["push", "origin", "main"], cwd=REL_WORKTREE, env=env)
        if p.returncode != 0:
            fail("rvg-mac push main failed")

    # ---- 2. tag + push ----
    print("== tag + push ==")
    p = run(["git", "tag", "-l", TAG], cwd=REL_WORKTREE)
    if p.stdout.strip() == TAG:
        print("local tag %s already exists; verifying it points at the sync commit" % TAG)
        p = run(["git", "rev-list", "-n", "1", TAG], cwd=REL_WORKTREE)
        if p.stdout.strip() != rel_commit:
            fail("local tag %s points elsewhere" % TAG)
    else:
        p = run(["git", "tag", "-a", TAG, rel_commit,
                 "-m", "RVG Mac agent v" + VERSION], cwd=REL_WORKTREE)
        if p.returncode != 0:
            fail("tag create failed")
    p = run(["git"] + cfg + ["push", "origin", TAG], cwd=REL_WORKTREE, env=env)
    if p.returncode != 0:
        fail("tag push failed")
    p = run(["git"] + cfg + ["ls-remote", "origin", "refs/tags/" + TAG + "^{}"], cwd=REL_WORKTREE, env=env)
    remote_sha = p.stdout.split()[0] if p.stdout.split() else ""
    print("tag peeled remote=%s expect=%s" % (remote_sha[:12], rel_commit[:12]))
    if remote_sha != rel_commit:
        fail("remote tag SHA mismatch")

    # ---- 3. create release ----
    print("== create release ==")
    status, resp = api_req(
        "POST", "/repos/%s/releases" % REL_REPO,
        token, body={
            "tag_name": TAG,
            "target_commitish": rel_commit,
            "name": "RVG Mac agent v" + VERSION,
            "body": BODY,
            "draft": False,
            "prerelease": False})
    print("create release http=%d" % status)
    if status not in (201,):
        print(resp.decode("utf-8", "replace")[:500])
        fail("release create failed")
    rel = json.loads(resp)
    rel_id = rel["id"]
    print("release id=%s tag=%s draft=%s prerelease=%s" %
          (rel_id, rel.get("tag_name"), rel.get("draft"), rel.get("prerelease")))

    # ---- 4. upload assets ----
    print("== upload assets ==")
    for asset_path, asset_name in ((SCRIPT, "rvg_mac.py"),
                                   (MANIFEST, "rvg_mac.py.sha256")):
        with open(asset_path, "rb") as f:
            data = f.read()
        status, resp = api_req(
            "POST",
            "/repos/%s/releases/%s/assets?name=%s"
            % (REL_REPO, rel_id, asset_name), token, raw_bytes=data)
        print("upload %s http=%d" % (asset_name, status))
        if status not in (201,):
            print(resp.decode("utf-8", "replace")[:500])
            fail("asset upload failed: %s" % asset_name)
        asset = json.loads(resp)
        print("asset id=%s name=%s size=%s" % (
            asset.get("id"), asset.get("name"), asset.get("size")))
        if asset.get("size") != len(data):
            fail("uploaded asset size mismatch: %s" % asset_name)
finally:
    subprocess.run(["shred", "-u", CREDS], capture_output=True)
    print("creds shredded: exists=%s" % os.path.exists(CREDS))

# ---- 5. verify via the exact channel the agent checks (anonymous) ----
print("== verify via releases/latest (anonymous) ==")
status, body_bytes = anon_get("/repos/%s/releases/latest" % REL_REPO)
if status != 200:
    fail("releases/latest http=%d (repo %s must be PUBLIC for the anonymous check)" % (status, REL_REPO))
latest = json.loads(body_bytes)
tag = latest.get("tag_name", "")
print("latest tag=%s name=%s" % (tag, latest.get("name")))
if tag != TAG:
    fail("latest tag is %s, not %s" % (tag, TAG))
assets = latest.get("assets", [])
by_name = {a.get("name"): a for a in assets}
print("assets: %s" % [(a.get("name"), a.get("size")) for a in assets])
if "rvg_mac.py" not in by_name or "rvg_mac.py.sha256" not in by_name:
    fail("expected both assets rvg_mac.py and rvg_mac.py.sha256")
anon_download(by_name["rvg_mac.py"]["browser_download_url"], DL)
anon_download(by_name["rvg_mac.py.sha256"]["browser_download_url"], DL_MAN)
dl_sha = sha256(DL)
dl_size = os.path.getsize(DL)
print("downloaded agent sha=%s size=%d" % (dl_sha, dl_size))
with open(DL_MAN) as f:
    dl_man = f.read().strip()
subprocess.run(["shred", "-u", DL], capture_output=True)
subprocess.run(["shred", "-u", DL_MAN], capture_output=True)
if dl_sha != got_sha or dl_size != got_size:
    fail("downloaded agent SHA/size mismatch")
if dl_man.split()[0] != got_sha:
    fail("downloaded manifest SHA mismatch")
print("RVG-MAC-V2-RELEASE-OK tag=%s release_id=%s asset_sha_match=true" % (TAG, rel_id))
