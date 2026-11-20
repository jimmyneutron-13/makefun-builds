#!/usr/bin/env python3
"""GitHub Actions driver: build + submit ONE news-image-20261120 entry.
Port of ~/workspace/pc-build/probe-pc-build-one.py (proven on PC, acct 701 in 189.4s)
for an ubuntu-latest runner. Usage: gh-build-one.py <N> <handle> <wallet>

Expected layout under $GH_WORK_DIR (default ./work):
  .makefun/credentials-<N>          (decrypted per-account bundle)
  makefun-keypair-<N>.json          (decrypted per-account bundle)
  shared/parent-session.jsonl       (decrypted shared bundle)
  shared/system-prompt-real.txt
  shared/muse-usage-news-image-20261120.jsonl
  shared/brief-news-image-20261120-minimal-pc-template.txt
  shared/news-20261120-dense.md
  shared/shoot-news105.mjs
  shared/xingchi.config.toml
XINGCHI_API_KEY must be in env (GitHub secret). Node + codex from PATH.
Writes result-<N>.json in cwd and prints the same JSON as the last line.
Never prints secret values.
"""
import json, os, pathlib, re, subprocess, sys, time

T0 = time.time()
PHASES = {}
def mark(name):
    PHASES[name] = round(time.time() - T0, 1)

N = int(sys.argv[1]); HANDLE = sys.argv[2]; WALLET = sys.argv[3]
BASE = pathlib.Path(os.environ.get("GH_WORK_DIR", "work")).resolve()
SHARED = BASE / "shared"
SLUG = "news-image-20261120"
REPO = BASE / f"{SLUG}-bot-acct{N}"
KITDIR = BASE / f"{SLUG}-kit-{N}"
KIT = KITDIR / f"{SLUG}-kit"
INIT_LOG = BASE / f"init-{SLUG}-acct{N}.log"
BRIEF = BASE / f"brief-{SLUG}-acct{N}.txt"
BUILD_LOG = BASE / f"codex-news{N}.log"
RESUME_LOG = BASE / f"codex-news{N}-resume.log"
BUNDLE_OUT = BASE / f"transcripts-{SLUG}-acct{N}.tar.gz"
PARENT_SESSION = str(SHARED / "parent-session.jsonl")
USAGE_FILE = SHARED / "muse-usage-news-image-20261120.jsonl"
TITLE = "The Atlas Daily — Friday 20 November 2026"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
NODE = "node"
CODEX = "codex"
CREDS_FILE = BASE / ".makefun" / f"credentials-{N}"
KEYPAIR = BASE / f"makefun-keypair-{N}.json"
RESULT_FILE = pathlib.Path.cwd() / f"result-{N}.json"

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] acct{N}: {msg}", flush=True)

def finish(result):
    result["phases_sec"] = PHASES
    result["total_sec"] = round(time.time() - T0, 1)
    RESULT_FILE.write_text(json.dumps(result, indent=1))
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)

def run(cmd, cwd=None, env_extra=None, timeout=1800, stdin_file=None):
    env = dict(os.environ)
    if env_extra: env.update(env_extra)
    kw = dict(cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if stdin_file: kw["stdin"] = open(stdin_file)
    return subprocess.run(cmd, **kw)

result = {"n": N, "handle": HANDLE, "ok": False, "machine": "github-actions"}

if not os.environ.get("XINGCHI_API_KEY"):
    result["error"] = "XINGCHI_API_KEY env missing (set the GitHub secret)"
    finish(result)
for p in (CREDS_FILE, KEYPAIR, SHARED / "parent-session.jsonl", SHARED / "system-prompt-real.txt"):
    if not p.exists():
        result["error"] = f"missing decrypted file: {p.name}"
        finish(result)

# Codex xingchi profile (no secret inside; key comes from env)
codex_home = pathlib.Path.home() / ".codex"
codex_home.mkdir(parents=True, exist_ok=True)
prof = codex_home / "xingchi.config.toml"
if not prof.exists():
    prof.write_text((SHARED / "xingchi.config.toml").read_text())

# 1. fresh kit
if not (KIT / "cli" / "makefun.mjs").exists():
    KITDIR.mkdir(parents=True, exist_ok=True)
    tarball = KITDIR / "kit.tar.gz"
    r = None
    for attempt in range(4):
        r = run(["curl", "-sSL", "-A", UA, "-o", str(tarball), f"https://make.fun/c/{SLUG}/kit"], timeout=300)
        if r.returncode == 0 and tarball.exists() and tarball.stat().st_size > 100000:
            break
        log(f"kit download attempt {attempt+1} failed, retrying in 30s")
        time.sleep(30)
    if r.returncode != 0 or not tarball.exists():
        result["error"] = "kit download failed"; result["tail"] = (r.stdout + r.stderr)[-500:]
        finish(result)
    r = run(["tar", "xzf", str(tarball), "-C", str(KITDIR)], timeout=300)
    tarball.unlink(missing_ok=True)
    if not (KIT / "cli" / "makefun.mjs").exists():
        result["error"] = "kit unpack failed"; result["tail"] = (r.stdout + r.stderr)[-500:]
        finish(result)
    log("kit ready")
mark("kit_done")

# 2. repo + init
REPO.mkdir(parents=True, exist_ok=True)
env = {"MAKEFUN_CREDENTIALS": str(CREDS_FILE)}
m = None; out = ""
if (REPO / ".makefun" / "freshness.json").exists() and INIT_LOG.exists():
    m = re.search(r"makefun anchor ([0-9a-f]+) round (\d+)", INIT_LOG.read_text())
    if m: log("repo already anchored, skipping init")
for attempt in range(5):
    if m: break
    r = run([NODE, str(KIT / "cli" / "makefun.mjs"), "init", "--competition", SLUG,
             "--keypair", str(KEYPAIR)], cwd=REPO, env_extra=env, timeout=600)
    out = r.stdout + r.stderr
    m = re.search(r"makefun anchor ([0-9a-f]+) round (\d+)", out)
    if m: break
    log(f"init attempt {attempt+1} failed (no anchor), retrying in 45s")
    time.sleep(45)
if not m:
    result["error"] = "init failed (no anchor)"; result["tail"] = out[-800:]
    finish(result)
ANCHOR, ROUND = m.group(0), m.group(2)
INIT_LOG.write_text(ANCHOR + "\n")
log(f"init ok: {ANCHOR}")
fresh = REPO / ".makefun" / "freshness.json"
try:
    fw = json.loads(fresh.read_text()).get("wallet")
    if fw and fw != WALLET:
        result["error"] = f"anchor wallet mismatch: {fw} != {WALLET}"
        finish(result)
except Exception as e:
    log(f"freshness read note: {e}")
mark("init_done")

# 3. codex trust entry
cfg = codex_home / "config.toml"
entry = f'[projects."{REPO}"]\ntrust_level = "trusted"\n'
existing = cfg.read_text() if cfg.exists() else ""
if entry.split("\n")[0] not in existing:
    with open(cfg, "a") as f: f.write("\n" + entry)
    log("codex trust added")

# 4. brief (rewrite PC paths to runner paths)
tpl = (SHARED / "brief-news-image-20261120-minimal-pc-template.txt").read_text()
brief = (tpl.replace("{N}", str(N)).replace("{HANDLE}", HANDLE)
            .replace("{WALLET}", WALLET).replace("{ANCHOR}", ANCHOR).replace("{ROUND}", ROUND)
            .replace("/home/rizky/makefun-probe", str(BASE))
            .replace("/home/rizky/.makefun", str(BASE / ".makefun")))
BRIEF.write_text(brief)

# 5. codex build
need_build = True
if (REPO / "composition.html").exists() and BUILD_LOG.exists() and re.search(r"session id: [0-9a-f-]{36}", BUILD_LOG.read_text(errors="replace")):
    need_build = False
    log("previous codex build present, skipping to render")
if need_build:
    env_full = dict(os.environ); env_full["MAKEFUN_CREDENTIALS"] = str(CREDS_FILE)
    log("codex build starting")
    with open(BUILD_LOG, "w") as lf, open(BRIEF) as bf:
        r = subprocess.run([CODEX, "exec", "-p", "xingchi", "--approve-for-me", "--add-dir", str(BASE), "-C", str(REPO)],
                           cwd=REPO, env=env_full, stdin=bf, stdout=lf, stderr=subprocess.STDOUT, timeout=3600)
    log(f"codex build exit {r.returncode}")
    if not (REPO / "composition.html").exists():
        result["error"] = "codex build produced no composition.html"
        result["tail"] = BUILD_LOG.read_text(errors="replace")[-800:]
        finish(result)
mark("build_done")

# 6. render entry.png (playwright-core + chromium from workflow setup)
r = run([NODE, str(SHARED / "shoot-news105.mjs"), str(REPO / "composition.html"), str(REPO / "entry.png")],
        cwd=SHARED, timeout=300)
log(f"render: {r.stdout.strip()[:200]} {r.stderr.strip()[:200]}")
if not (REPO / "entry.png").exists():
    result["error"] = "render produced no entry.png"; finish(result)
mark("render_done")

# 7. resume Codex to verify + commit (structural, --no-visual style)
m = re.search(r"session id: ([0-9a-f-]{36})", BUILD_LOG.read_text(errors="replace"))
if not m:
    result["error"] = "no session id in build log"; finish(result)
SID = m.group(1); result["codex_session"] = SID
resume_prompt = f"""The driver has rendered entry.png (1024x1024) in this repo. IMPORTANT: do NOT open or view the image file itself (image viewing is crashing this session). Verify structurally instead, then commit:
1. Run your verify script: PNG signature/chunks, exactly 1024x1024, at most 1 MiB, story-block count equals banner.
2. Run main.ts twice (second run from cwd=/), byte-identical outputs, entry.png present in both.
3. If a structural check fails, fix it with a genuine edit and tell the driver it needs a re-render (do NOT commit yet). If all structural checks pass, commit exactly one commit on top of existing history via the /tmp git bundle method from your brief (/tmp/bundle-news{N}/entry.bundle). Report: commit sha, anchor round {ROUND}, story count (verified), entry.png size + dimensions, determinism result, your 3 lead stories. Do NOT submit."""
r = run([CODEX, "exec", "-p", "xingchi", "--approve-for-me", "--add-dir", str(BASE), "-C", str(REPO),
         "resume", SID, resume_prompt], cwd=REPO, timeout=2400)
RESUME_LOG.write_text(r.stdout + r.stderr)
log(f"resume exit {r.returncode}")
bundle_path = pathlib.Path(f"/tmp/bundle-news{N}/entry.bundle")
if not bundle_path.exists():
    result["error"] = "no git bundle after resume (needs re-render or build incomplete)"
    result["resume_tail"] = (r.stdout + r.stderr)[-800:]
    finish(result)
run(["git", "fetch", str(bundle_path), "HEAD"], cwd=REPO, timeout=120)
run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=REPO, timeout=120)
sha = run(["git", "rev-parse", "HEAD"], cwd=REPO).stdout.strip()
result["commit"] = sha
log(f"commit {sha}")
mark("resume_done")

# 8. bundle transcripts
r = run([NODE, "cli/makefun.mjs", "bundle", "--repo", str(REPO), "--out", str(BUNDLE_OUT),
         "--system-prompt", str(SHARED / "system-prompt-real.txt"),
         "--prompt-file", str(BRIEF),
         "--prompt-origin", "handed-off",
         "--prompt-via", "parent agent handoff",
         "--harness", "codex", "--harness-version", "0.170.0", "--provider", "xingchi",
         "--model", "gpt-6.1-sol", "--effort", "default", "--mcp", "none",
         "--parent-session", PARENT_SESSION,
         "--muse-usage", str(USAGE_FILE),
         "--no-family",
         "--json"], cwd=KIT, timeout=900)
log(f"bundle exit {r.returncode}")
if r.returncode != 0 or not BUNDLE_OUT.exists():
    result["error"] = "bundle failed"; result["bundle_tail"] = (r.stdout + r.stderr)[-1200:]
    finish(result)
mark("bundle_done")

# 9. check
r = run([NODE, "cli/makefun.mjs", "check", "--kit", ".", "--repo", str(REPO),
         "--transcripts", str(BUNDLE_OUT), "--json"], cwd=KIT, timeout=900)
log(f"check exit {r.returncode}")
if r.returncode != 0:
    result["error"] = "check failed"; result["check_tail"] = (r.stdout + r.stderr)[-2000:]
    finish(result)
mark("check_done")

# 10. submit
r = run([NODE, "cli/makefun.mjs", "submit", "--competition", SLUG,
         "--source", str(REPO), "--commit", sha, "--bundle", str(BUNDLE_OUT),
         "--keypair", str(KEYPAIR),
         "--title", TITLE, "--json"], cwd=KIT, env_extra=env, timeout=900)
log(f"submit exit {r.returncode}")
out = r.stdout + r.stderr
try:
    sj = {}
    dec = json.JSONDecoder(); idx = 0
    while True:
        i = out.find("{", idx)
        if i < 0: break
        try:
            obj, _ = dec.raw_decode(out[i:])
            if isinstance(obj, dict) and ("submission_id" in obj or "claim_code" in obj):
                sj = obj; break
        except Exception:
            pass
        idx = i + 1
    if not sj: raise ValueError("no submit JSON object found")
    result["submission"] = sj.get("id") or sj.get("submission_id") or sj.get("submission", {}).get("id")
    result["claim"] = sj.get("claim_code") or sj.get("claim") or sj.get("submission", {}).get("claim_code")
except Exception:
    result["submit_tail"] = out[-1500:]
if r.returncode != 0:
    result["error"] = "submit failed"; result["submit_tail"] = out[-1500:]
    finish(result)
mark("submit_done")

# 11. verify server status
if result.get("submission"):
    import urllib.request
    creds = json.load(open(CREDS_FILE))
    key = creds["keys"]["https://make.fun"]["key"]
    req = urllib.request.Request(f"https://make.fun/api/submissions/{result['submission']}",
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
            result["server_status"] = body.get("status")
            result["server_title"] = body.get("title")
    except Exception as e:
        result["verify_error"] = str(e)[:200]

result["ok"] = bool(result.get("submission"))
log(f"done ok={result['ok']}")
finish(result)
