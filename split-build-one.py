#!/usr/bin/env python3
"""SPLIT pipeline, runner side: build ONE make.fun entry (coding only).

Runs on a GitHub-hosted runner. NO make.fun credentials or keypair exist on
this machine: `makefun init` was already done on the VM (the payload repo
carries the drand root commit + anchor), and bundle/check/submit happen back
on the VM. This driver only:
  1. downloads the public kit (for RULES.txt / competition.yaml)
  2. runs the Codex build session (brief quotes the anchor first)
  3. renders entry.png (playwright-core, installed by the workflow)
  4. resumes Codex to verify + commit (git bundle method, as in gh-build-one.py)
  5. packages entry.bundle + Codex session files + result-<N>.json into an
     encrypted tar (BUNDLE_PASSPHRASE secret) for upload as an artifact

Usage: split-build-one.py <N>
Payload dir: $SPLIT_BASE (default /home/runner/work/_temp/split-acct-<N>)
containing repo/, brief.txt, anchor.txt, meta.json, news file,
shoot-news105.mjs, xingchi.config.toml.
Writes split-result-<N>.enc in cwd. Always exits 0 after packaging (the
result JSON inside carries ok/error). Never prints secret values.
"""
import json, os, pathlib, re, shutil, signal, subprocess, sys, tarfile, time

T0 = time.time()
PHASES = {}
def mark(name):
    PHASES[name] = round(time.time() - T0, 1)

N = int(sys.argv[1])
BASE = pathlib.Path(os.environ.get("SPLIT_BASE", f"/home/runner/work/_temp/split-acct-{N}")).resolve()
REPO = BASE / "repo"
BRIEF = BASE / "brief.txt"
META = json.loads((BASE / "meta.json").read_text())
SLUG = META["slug"]; HANDLE = META["handle"]; WALLET = META["wallet"]
ANCHOR = META["anchor"]; ROUND = META["round"]
KITDIR = BASE / "kit"
KIT = KITDIR / f"{SLUG}-kit"
BUILD_LOG = BASE / f"codex-split{N}.log"
RESUME_LOG = BASE / f"codex-split{N}-resume.log"
OUT_ENC = pathlib.Path.cwd() / f"split-result-{N}.enc"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
NODE = "node"
CODEX = "codex"

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] acct{N}: {msg}", flush=True)

def run(cmd, cwd=None, env_extra=None, timeout=1800, stdin_file=None):
    env = dict(os.environ)
    if env_extra: env.update(env_extra)
    stdin = open(stdin_file) if stdin_file else None
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=stdin,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try: os.killpg(p.pid, signal.SIGKILL)
        except Exception: pass
        try: out, err = p.communicate(timeout=10)
        except Exception: out, err = "", ""
        err = (err or "") + f"\n[TIMEOUT after {timeout}s, process group killed]"
        return subprocess.CompletedProcess(cmd, -9, out, err)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)

result = {"n": N, "handle": HANDLE, "ok": False, "machine": "github-actions-split", "anchor": ANCHOR}

def package_and_exit():
    """Collect outputs, encrypt, write OUT_ENC, print result JSON, exit 0."""
    result["phases_sec"] = PHASES
    result["total_sec"] = round(time.time() - T0, 1)
    outdir = BASE / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"result-{N}.json").write_text(json.dumps(result, indent=1))
    for f in (BUILD_LOG, RESUME_LOG):
        if f.exists(): shutil.copy2(f, outdir / f.name)
    # git bundle of the finished repo (full history; VM repo shares the root)
    if result.get("commit"):
        r = run(["git", "bundle", "create", str(outdir / "entry.bundle"), "HEAD"], cwd=REPO, timeout=300)
        if r.returncode != 0:
            log(f"git bundle create failed: {(r.stdout + r.stderr)[-300:]}")
    # Codex session files created during this run (preserve ~/.codex layout)
    sess_root = pathlib.Path.home() / ".codex" / "sessions"
    n_sess = 0
    if sess_root.exists():
        for sf in sess_root.rglob("*.jsonl"):
            try:
                if sf.stat().st_mtime >= T0 - 120:
                    dest = outdir / "sessions" / sf.relative_to(sess_root)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sf, dest); n_sess += 1
            except Exception:
                pass
    result["session_files"] = n_sess
    (outdir / f"result-{N}.json").write_text(json.dumps(result, indent=1))
    tar_path = BASE / f"split-result-{N}.tar"
    with tarfile.open(tar_path, "w") as tf:
        tf.add(outdir, arcname=".")
    pw = "".join((os.environ.get("BUNDLE_PASSPHRASE") or "").split())
    env = dict(os.environ); env["GH_BUNDLE_PASS"] = pw
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000",
                        "-in", str(tar_path), "-out", str(OUT_ENC), "-pass", "env:GH_BUNDLE_PASS"],
                       env=env, capture_output=True, text=True, timeout=600)
    tar_path.unlink(missing_ok=True)
    log(f"packaged {OUT_ENC.name} (encrypt rc={r.returncode}, sessions={n_sess})")
    print(json.dumps(result))
    sys.exit(0)

def fail(msg, **kw):
    result["error"] = msg
    result.update(kw)
    log(f"FAIL: {msg}")
    package_and_exit()

try:
    codex_home = pathlib.Path.home() / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    # Provider: seek100 (default, 100milionseek via local LiteLLM proxy) or
    # xingchi (fallback when Xingchi recovers). Selected by the workflow
    # dispatch input `provider`; IPNext/Muse tokens are NEVER used here.
    PROVIDER = (os.environ.get("PROVIDER") or "seek100").strip().lower()
    if PROVIDER == "seek100":
        # Codex speaks Responses API to 127.0.0.1:4100; LiteLLM translates
        # to Chat Completions at 100milionseek.top. The proxy was started
        # by the workflow step; this script only needs the per-run proxy
        # auth key (LITELLM_API_KEY). The upstream SEEK100 key never
        # enters this script — only its slot number is logged.
        if not os.environ.get("LITELLM_API_KEY"):
            fail("seek100 selected but LITELLM_API_KEY env missing (LiteLLM proxy step did not run?)")
        repo_prof = pathlib.Path.cwd() / "seek100.config.toml"
        prof = codex_home / "seek100.config.toml"
        if repo_prof.exists():
            prof.write_text(repo_prof.read_text())
        elif not prof.exists():
            fail("seek100.config.toml missing from repo checkout")
        PROFILE = "seek100"
        log(f"using seek100 via LiteLLM proxy (SEEK100 slot {os.environ.get('SEEK100_SLOT', '?')}/8)")
    elif PROVIDER == "xingchi":
        # Xingchi key pool: same 14-slot logic as gh-build-one.py (slot number only in log)
        _pool = []
        for _slot in range(1, 15):
            _name = "XINGCHI_API_KEY" if _slot == 1 else f"XINGCHI_API_KEY_{_slot}"
            _val = "".join((os.environ.get(_name) or "").split())
            if _val: _pool.append((_slot, _val))
        if _pool:
            _slot, _val = _pool[N % len(_pool)]
            os.environ["XINGCHI_API_KEY"] = _val
            log(f"using Xingchi key slot {_slot}/{len(_pool)}")
        if not os.environ.get("XINGCHI_API_KEY"):
            fail("XINGCHI_API_KEY env missing (set the GitHub secret)")

        # Codex xingchi profile (no secret inside; key comes from env)
        prof = codex_home / "xingchi.config.toml"
        if not prof.exists() and (BASE / "xingchi.config.toml").exists():
            prof.write_text((BASE / "xingchi.config.toml").read_text())
        PROFILE = "xingchi"
    else:
        fail(f"unknown PROVIDER {PROVIDER!r} (want seek100 or xingchi)")

    # 1. fresh kit (public, no auth)
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
            fail("kit download failed", tail=(r.stdout + r.stderr)[-500:])
        r = run(["tar", "xzf", str(tarball), "-C", str(KITDIR)], timeout=300)
        tarball.unlink(missing_ok=True)
        if not (KIT / "cli" / "makefun.mjs").exists():
            fail("kit unpack failed", tail=(r.stdout + r.stderr)[-500:])
        log("kit ready")
    mark("kit_done")

    # sanity: repo arrived anchored with the right wallet
    fresh = REPO / ".makefun" / "freshness.json"
    if not fresh.exists():
        fail("payload repo has no .makefun/freshness.json (init missing)")
    try:
        fw = json.loads(fresh.read_text()).get("wallet")
        if fw and fw != WALLET:
            fail(f"anchor wallet mismatch: {fw} != {WALLET}")
    except Exception as e:
        log(f"freshness read note: {e}")

    # 2. codex trust entry
    cfg = codex_home / "config.toml"
    entry = f'[projects."{REPO}"]\ntrust_level = "trusted"\n'
    existing = cfg.read_text() if cfg.exists() else ""
    if entry.split("\n")[0] not in existing:
        with open(cfg, "a") as f: f.write("\n" + entry)
        log("codex trust added")

    # 3. codex build (NO makefun credentials on this machine)
    log("codex build starting")
    try:
        with open(BUILD_LOG, "w") as lf, open(BRIEF) as bf:
            r = subprocess.run([CODEX, "exec", "-p", PROFILE, "--approve-for-me", "--add-dir", str(BASE), "-C", str(REPO)],
                               cwd=REPO, env=dict(os.environ), stdin=bf, stdout=lf, stderr=subprocess.STDOUT, timeout=3600)
        log(f"codex build exit {r.returncode}")
    except subprocess.TimeoutExpired:
        # Stuck build (e.g. Reconnecting loop) must NOT crash the driver:
        # fall through to the resume-continue loop, which resumes the same
        # session instead of discarding an hour of work (11/20 jobs in run
        # 45376162734 died exactly here: "driver exception: TimeoutExpired").
        log("codex build timed out after 3600s (stuck/reconnect loop), treating as truncation")
    # Resume-continue loop (added 24 Nov 2026): pilot #2 (acct 1371) died with
    # "Incomplete response returned, reason: max_output_tokens" after 444k
    # tokens of planning and no composition.html. A truncated turn is
    # recoverable: resume the same session and tell it to stop planning and
    # write the files. Up to 2 continue turns before giving up.
    for _attempt in range(2):
        if (REPO / "composition.html").exists():
            break
        _m = re.search(r"session id: ([0-9a-f-]{36})", BUILD_LOG.read_text(errors="replace"))
        if not _m:
            break
        _sid = _m.group(1)
        _cont = ("Your previous turn was cut off before you finished. Continue exactly where you left off: "
                 "stop planning and write composition.html NOW with the layout you already decided, then main.ts "
                 "and the verify script per the brief. Keep code tight; do not start new helper tools. "
                 "Do NOT commit yet and do NOT submit — the driver renders entry.png and resumes you.")
        log(f"resume-continue attempt {_attempt + 1} session {_sid}")
        try:
            with open(BUILD_LOG, "a") as lf:
                r = subprocess.run([CODEX, "exec", "-p", PROFILE, "--approve-for-me", "--add-dir", str(BASE), "-C", str(REPO),
                                    "resume", _sid, _cont],
                                   cwd=REPO, env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, timeout=3600)
            log(f"resume-continue exit {r.returncode}")
        except subprocess.TimeoutExpired:
            log(f"resume-continue attempt {_attempt + 1} timed out after 3600s")
    if not (REPO / "composition.html").exists():
        fail("codex build produced no composition.html", tail=BUILD_LOG.read_text(errors="replace")[-800:])
    mark("build_done")

    # 4. render entry.png
    r = run([NODE, str(BASE / "shoot-news105.mjs"), str(REPO / "composition.html"), str(REPO / "entry.png")],
            cwd=BASE, timeout=300)
    log(f"render: {r.stdout.strip()[:200]} {r.stderr.strip()[:200]}")
    if not (REPO / "entry.png").exists():
        fail("render produced no entry.png")
    mark("render_done")

    # 5. resume Codex to verify + commit (structural, --no-visual style)
    m = re.search(r"session id: ([0-9a-f-]{36})", BUILD_LOG.read_text(errors="replace"))
    if not m:
        fail("no session id in build log")
    SID = m.group(1); result["codex_session"] = SID
    resume_prompt = f"""The driver has rendered entry.png (1024x1024) in this repo. IMPORTANT: do NOT open or view the image file itself (image viewing is crashing this session). Verify structurally instead, then commit:
1. Run your verify script: PNG signature/chunks, exactly 1024x1024, at most 1 MiB, story-block count equals banner.
2. Run main.ts twice (second run from cwd=/), byte-identical outputs, entry.png present in both.
3. If a structural check fails, fix it with a genuine edit and tell the driver it needs a re-render (do NOT commit yet). If all structural checks pass, commit exactly one commit on top of existing history via the /tmp git bundle method from your brief (/tmp/bundle-news{N}/entry.bundle). Report: commit sha, anchor round {ROUND}, story count (verified), entry.png size + dimensions, determinism result, your 3 lead stories. Do NOT submit."""
    r = run([CODEX, "exec", "-p", PROFILE, "--approve-for-me", "--add-dir", str(BASE), "-C", str(REPO),
             "resume", SID, resume_prompt], cwd=REPO, timeout=2400)
    RESUME_LOG.write_text(r.stdout + r.stderr)
    log(f"resume exit {r.returncode}")
    bundle_path = pathlib.Path(f"/tmp/bundle-news{N}/entry.bundle")
    if not bundle_path.exists():
        fail("no git bundle after resume (needs re-render or build incomplete)",
             resume_tail=(r.stdout + r.stderr)[-800:])
    run(["git", "fetch", str(bundle_path), "HEAD"], cwd=REPO, timeout=120)
    run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=REPO, timeout=120)
    sha = run(["git", "rev-parse", "HEAD"], cwd=REPO).stdout.strip()
    result["commit"] = sha
    result["ok"] = True
    log(f"commit {sha}")
    mark("resume_done")
except SystemExit:
    raise
except Exception as e:
    result["error"] = f"driver exception: {type(e).__name__}: {str(e)[:300]}"
    log(result["error"])

package_and_exit()
