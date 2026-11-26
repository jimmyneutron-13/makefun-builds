#!/usr/bin/env python3
"""SPLIT pipeline, VM side step 1: prepare an anchored build payload for ONE
make.fun account and push it (encrypted) to the jimmyneutron-13 GitHub lane.

DIRECT-SUBMIT lane (user order 24 Nov 2026 17:22 WIB, "langsung upload submit
tanpa perlu melalui vm", risks accepted): the encrypted payload ALSO carries
the account credentials + keypair, the real system prompt, a gzipped snapshot
of the parent Muse session, and the muse-usage file, so the runner can
bundle/check/submit itself (split-submit-one.py). The whole tar is encrypted
with openssl aes-256-cbc (passphrase from ~/.gh-bundle-pass / the
BUNDLE_PASSPHRASE GitHub secret); plaintext credentials exist only inside the
runner during its run. This supersedes the 22 Nov "credentials never leave
the VM" split rule for this lane. This script:
  1. looks up the account in its origin ledger (~/workspace/makefun/accounts-*.jsonl)
  2. downloads a fresh public kit (https://make.fun/c/<slug>/kit)
  3. runs `makefun init` locally with the account's credentials + keypair
     (drand root commit + anchor), verifies the anchor wallet
  4. writes the build brief (same template as the VM spillover lane, paths
     rewritten to the fixed runner layout, credentials line removed)
  5. tars repo+brief+anchor+meta+news+render script+codex profile+credentials
     +keypair+system-prompt+parent-session.gz+usage, encrypts with openssl
     aes-256-cbc (passphrase from ~/.gh-bundle-pass, env only, never printed)
     -> payloads/<slug>/acct-<N>.enc
  6. commits + pushes the payload to the jimmyneutron-13 remote (origin)

Usage: split-prepare.py <N> [--slug news-image-20261123] [--title "..."]
       [--force] [--no-push]
Prints a final JSON line. Never prints secret values.
"""
import argparse, gzip, json, os, pathlib, re, shutil, subprocess, sys, tarfile, time

HOME = pathlib.Path.home()
GH = HOME / "workspace" / "gh-builds"
MAKEFUN = HOME / "workspace" / "makefun"
# NOTE: split-work must live OUTSIDE the gh-builds git repo: `makefun init`
# refuses to run in a subdirectory of an existing git repository.
SPLIT_WORK = MAKEFUN / "split-work"
PAYLOADS = GH / "payloads"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TITLES = {"news-image-20261121": "The Atlas Daily — Saturday 21 November 2026",
          "news-image-20261122": "The Atlas Daily — Sunday 22 November 2026",
          "news-image-20261123": "The Atlas Daily — Monday 23 November 2026",
          "news-image-20261120": "The Atlas Daily — Friday 20 November 2026"}
# Parent chain (mirrors the VM lane's proven finalize-zeroentry.py, chain fix
# 24 Nov ~13:05 WIB): the old single parent 3ec4af1d is post-compaction and no
# longer contains the user's verbatim prompt, so the bundle's run-record
# parent-chain check fails and the server records 0 sessions / provenance
# "none" and cancels the submission (GitHub runs #7-#9). The valid chain is
# coordinator 9054334f -> main agent b0074dc6, whose session holds the user's
# order verbatim (also the content of PROMPT_PERSON below).
PARENT_SESSIONS = [
    HOME / "agents" / "agent-9054334f-b912-4cb2-8d7d-3c0fd16a9d64" / "sessions" / "9054334f-b912-4cb2-8d7d-3c0fd16a9d64.jsonl",
    HOME / "agents" / "agent-b0074dc6-6068-4379-b297-71e10e7fc08c" / "sessions" / "b0074dc6-6068-4379-b297-71e10e7fc08c.jsonl",
]
PROMPT_PERSON = MAKEFUN / "prompt-person-news-image-20261123.txt"
SYSTEM_PROMPT = MAKEFUN / "system-prompt-real.txt"
# Fixed runner layout (GitHub-hosted ubuntu: RUNNER_TEMP=/home/runner/work/_temp;
# the workflow symlinks this path if RUNNER_TEMP differs).
RUNNER_BASE = "/home/runner/work/_temp/split-acct-{N}"

def find_local_kit(slug):
    """Newest verified unpacked kit copy on this VM (same public bundle the
    IPNext lane already used to go live), or None. The kit endpoint
    https://make.fun/c/<slug>/kit returned 503 overloaded on 25 Nov 2026,
    so a fresh download is not available; reusing a local copy of the same
    bundle (bundle_sha256 444e0a0c67da6736, harness@969cfe34) is the fallback.
    The kit is public and account-independent; only its cli/makefun.mjs and
    RULES.txt/competition.yaml are used. Never read starter code from it."""
    best = None
    for d in sorted(MAKEFUN.glob(f"{slug}-kit-*")):
        inner = d / f"{slug}-kit"
        k = inner / "cli" / "makefun.mjs"
        if not k.exists():
            continue
        # Newest kit copy by directory mtime (kit-1542, used by IPNext wave 6
        # to go live 04:48 WIB, beats older copies: harness versions drift
        # across downloads even when bundle_sha256 matches).
        mtime = d.stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, inner)
    return best[1] if best else None

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def run(cmd, cwd=None, env_extra=None, timeout=1800):
    env = dict(os.environ)
    if env_extra: env.update(env_extra)
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)

def passphrase():
    pf = HOME / ".gh-bundle-pass"
    if not pf.exists():
        raise SystemExit("~/.gh-bundle-pass missing")
    return pf.read_text().strip()

def encrypt(tar_path, enc_path, pw):
    env = dict(os.environ); env["GH_BUNDLE_PASS"] = pw
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000",
                        "-in", str(tar_path), "-out", str(enc_path), "-pass", "env:GH_BUNDLE_PASS"],
                       env=env, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"openssl encrypt failed: {r.stderr[-300:]}")

def find_account(n):
    for lf in sorted(MAKEFUN.glob("accounts-*.jsonl")):
        for line in lf.read_text().splitlines():
            try: d = json.loads(line)
            except Exception: continue
            if d.get("n") == n and d.get("status") == "ok":
                return d, lf
    return None, None

def already_live(ledger, n, slug):
    for line in ledger.read_text().splitlines():
        try: d = json.loads(line)
        except Exception: continue
        if d.get("n") == n and d.get("status") == "live" and (d.get("entry") or {}).get("bounty") == slug:
            return True
    return False

def main():
    ap = argparse.ArgumentParser(description="Prepare encrypted split-build payload for one account (VM side).")
    ap.add_argument("n", type=int)
    ap.add_argument("--slug", default="news-image-20261123")
    ap.add_argument("--title", default=None)
    ap.add_argument("--force", action="store_true", help="redo init/payload even if present")
    ap.add_argument("--no-push", action="store_true", help="build payload but do not commit/push")
    args = ap.parse_args()
    N, SLUG = args.n, args.slug
    TITLE = args.title or TITLES.get(SLUG, SLUG)
    result = {"n": N, "slug": SLUG, "ok": False}

    acct, ledger = find_account(N)
    if not acct:
        result["error"] = "account not in any ledger (not registered yet)"
        print(json.dumps(result)); sys.exit(1)
    HANDLE, WALLET = acct["handle"], acct["wallet"]
    result["handle"] = HANDLE
    if already_live(ledger, N, SLUG):
        result["error"] = f"already live in {ledger.name} for {SLUG}"
        print(json.dumps(result)); sys.exit(1)
    log(f"acct{N} {HANDLE} origin ledger {ledger.name}")

    work = SPLIT_WORK / SLUG / f"acct-{N}"
    repo = work / "repo"
    kitdir = work / "kit"
    kit = kitdir / f"{SLUG}-kit"
    init_log = work / "init.log"
    brief_path = work / "brief.txt"
    meta_path = work / "meta.json"
    work.mkdir(parents=True, exist_ok=True)

    # 1. fresh kit (public download, no auth) — or reuse a verified local
    # copy when the kit endpoint is down (25 Nov 2026: 503 overloaded, the
    # download returns a 2.8 KB JSON error, not a kit).
    if not (kit / "cli" / "makefun.mjs").exists():
        kitdir.mkdir(parents=True, exist_ok=True)
        local = find_local_kit(SLUG)
        if local is not None:
            shutil.copytree(local, kit, dirs_exist_ok=True)
            if (kit / "cli" / "makefun.mjs").exists():
                log(f"kit reused from local copy {local} (download endpoint down)")
            else:
                result["error"] = "local kit copy incomplete"
                print(json.dumps(result)); sys.exit(1)
        else:
            tarball = kitdir / "kit.tar.gz"
            r = None
            for attempt in range(4):
                r = run(["curl", "-sSL", "-A", UA, "-o", str(tarball), f"https://make.fun/c/{SLUG}/kit"], timeout=300)
                if r.returncode == 0 and tarball.exists() and tarball.stat().st_size > 100000:
                    break
                log(f"kit download attempt {attempt+1} failed, retrying in 30s")
                time.sleep(30)
            if r.returncode != 0 or not tarball.exists():
                result["error"] = "kit download failed"; result["tail"] = (r.stdout + r.stderr)[-500:]
                print(json.dumps(result)); sys.exit(1)
            r = run(["tar", "xzf", str(tarball), "-C", str(kitdir)], timeout=300)
            tarball.unlink(missing_ok=True)
            if not (kit / "cli" / "makefun.mjs").exists():
                result["error"] = "kit unpack failed"; result["tail"] = (r.stdout + r.stderr)[-500:]
                print(json.dumps(result)); sys.exit(1)
            log("kit ready")

    # 2. repo + init (credentials + keypair stay on this VM)
    if args.force and repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True, exist_ok=True)
    creds = HOME / ".makefun" / f"credentials-{N}"
    keypair = MAKEFUN / f"makefun-keypair-{N}.json"
    for p in (creds, keypair):
        if not p.exists():
            result["error"] = f"missing local file: {p.name}"
            print(json.dumps(result)); sys.exit(1)
    env = {"MAKEFUN_CREDENTIALS": str(creds)}
    m = None; out = ""
    if (repo / ".makefun" / "freshness.json").exists() and init_log.exists() and not args.force:
        m = re.search(r"makefun anchor ([0-9a-f]+) round (\d+)", init_log.read_text())
        if m: log("repo already anchored, skipping init")
    for attempt in range(5):
        if m: break
        r = run(["node", str(kit / "cli" / "makefun.mjs"), "init", "--competition", SLUG,
                 "--keypair", str(keypair)], cwd=repo, env_extra=env, timeout=600)
        out = r.stdout + r.stderr
        m = re.search(r"makefun anchor ([0-9a-f]+) round (\d+)", out)
        if m: break
        log(f"init attempt {attempt+1} failed (no anchor), retrying in 45s")
        time.sleep(45)
    if not m:
        result["error"] = "init failed (no anchor)"; result["tail"] = out[-800:]
        print(json.dumps(result)); sys.exit(1)
    ANCHOR, ROUND = m.group(0), m.group(2)
    init_log.write_text(ANCHOR + "\n")
    log(f"init ok: {ANCHOR}")
    fresh = repo / ".makefun" / "freshness.json"
    try:
        fw = json.loads(fresh.read_text()).get("wallet")
        if fw and fw != WALLET:
            result["error"] = f"anchor wallet mismatch: {fw} != {WALLET}"
            print(json.dumps(result)); sys.exit(1)
    except Exception as e:
        log(f"freshness read note: {e}")
    result["anchor"] = ANCHOR; result["round"] = ROUND

    # 3. brief: same template as VM spillover lane, paths rewritten to runner layout
    tpl_path = MAKEFUN / f"brief-{SLUG}-template.txt"
    if not tpl_path.exists():
        result["error"] = f"brief template missing: {tpl_path.name}"
        print(json.dumps(result)); sys.exit(1)
    tpl = tpl_path.read_text()
    runner_base = RUNNER_BASE.format(N=N)
    brief = (tpl.replace("{N}", str(N)).replace("{HANDLE}", HANDLE)
                .replace("{WALLET}", WALLET).replace("{ANCHOR}", ANCHOR).replace("{ROUND}", ROUND))
    # credentials line: no credentials exist on this machine for CODEX to use;
    # the driver (split-submit-one.py) submits with the payload credentials
    # after the build. Codex must not run any makefun CLI command itself.
    brief = re.sub(r"CREDENTIALS:.*",
                   "CREDENTIALS: none for you to use — do NOT run any makefun CLI command "
                   "(init is already done; bundle/check/submit are done by the driver after "
                   "your build, with credentials you must never ask for). Never print or share any credential value.",
                   brief)
    brief = (brief
             .replace(f"/home/hatch/workspace/makefun/{SLUG}-bot-acct{N}", f"{runner_base}/repo")
             .replace(f"/home/hatch/workspace/makefun/{SLUG}-kit-{N}/{SLUG}-kit", f"{runner_base}/kit/{SLUG}-kit")
             .replace(f"/home/hatch/workspace/makefun/news-{SLUG.split('-')[-1]}-dense.md", f"{runner_base}/news-{SLUG.split('-')[-1]}-dense.md")
             .replace("/home/hatch/workspace/makefun", runner_base)
             .replace("/home/hatch/.makefun", runner_base))
    brief_path.write_text(brief)

    # 4. meta + payload tar
    meta = {"n": N, "handle": HANDLE, "wallet": WALLET, "slug": SLUG, "title": TITLE,
            "anchor": ANCHOR, "round": ROUND, "ledger": ledger.name,
            "runner_base": runner_base}
    meta_path.write_text(json.dumps(meta, indent=1))
    (work / "anchor.txt").write_text(ANCHOR + "\n")

    payload_dir = PAYLOADS / SLUG
    payload_dir.mkdir(parents=True, exist_ok=True)
    enc_path = payload_dir / f"acct-{N}.enc"
    tar_path = work / "payload.tar"
    news_name = f"news-{SLUG.split('-')[-1]}-dense.md"
    news_src = MAKEFUN / news_name
    shoot_src = MAKEFUN / "shoot-tools" / "shoot-news105.mjs"
    toml_src = HOME / ".codex" / "xingchi.config.toml"
    # DIRECT-SUBMIT payload contents (user order 24 Nov 2026): the whole tar is
    # encrypted, so these ride inside it. Credentials/keypair are COPIES of the
    # VM files (originals stay put). Parent session is gzipped (27MB -> ~3MB).
    creds_copy = work / "credentials.json"
    keypair_copy = work / "keypair.json"
    shutil.copy2(creds, creds_copy)
    shutil.copy2(keypair, keypair_copy)
    os.chmod(creds_copy, 0o600); os.chmod(keypair_copy, 0o600)
    parent_gzs = []
    for i, ps in enumerate(PARENT_SESSIONS):
        pgz = work / ("parent-session.jsonl.gz" if i == 0 else f"parent-session-{i + 1}.jsonl.gz")
        if ps.exists():
            with open(ps, "rb") as f_in, gzip.open(pgz, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            parent_gzs.append(pgz)
            log(f"parent session {i + 1} snapshot gzipped: {pgz.stat().st_size} bytes")
        else:
            log(f"WARNING: parent session {i + 1} file missing, payload will lack it")
    usage_src = HOME / ".makefun" / f"muse-usage-{SLUG}.jsonl"
    with tarfile.open(tar_path, "w") as tf:
        tf.add(repo, arcname="repo")
        tf.add(brief_path, arcname="brief.txt")
        tf.add(work / "anchor.txt", arcname="anchor.txt")
        tf.add(meta_path, arcname="meta.json")
        if news_src.exists(): tf.add(news_src, arcname=news_name)
        # Kit rides inside the payload (25 Nov 2026: the runner would otherwise
        # have to download it fresh and hit the same 503 overloaded endpoint).
        # split-build-one.py / split-submit-one.py skip their download when
        # kit/<slug>-kit/cli/makefun.mjs already exists.
        if kit.exists(): tf.add(kit, arcname=f"kit/{SLUG}-kit")
        if shoot_src.exists(): tf.add(shoot_src, arcname="shoot-news105.mjs")
        if toml_src.exists(): tf.add(toml_src, arcname="xingchi.config.toml")
        tf.add(creds_copy, arcname="credentials.json")
        tf.add(keypair_copy, arcname="keypair.json")
        if SYSTEM_PROMPT.exists(): tf.add(SYSTEM_PROMPT, arcname="system-prompt-real.txt")
        for pgz in parent_gzs: tf.add(pgz, arcname=pgz.name)
        if PROMPT_PERSON.exists(): tf.add(PROMPT_PERSON, arcname="prompt-person.txt")
        if usage_src.exists(): tf.add(usage_src, arcname=f"muse-usage-{SLUG}.jsonl")
    encrypt(tar_path, enc_path, passphrase())
    tar_path.unlink(missing_ok=True)
    result["payload"] = str(enc_path.relative_to(GH))
    result["payload_bytes"] = enc_path.stat().st_size
    log(f"payload encrypted: {result['payload']} ({result['payload_bytes']} bytes)")

    # 5. commit + push to origin (jimmyneutron-13). SSH via ssh.github.com:443
    # through the runtime proxy; default key is registered on jimmyneutron-13
    # (verified 24 Nov 2026: git ls-remote origin works with this command).
    # The old rasjdahw remote is suspended; do not push there.
    if args.no_push:
        result["pushed"] = False
    else:
        rel = str(enc_path.relative_to(GH))
        r = run(["git", "add", rel], cwd=GH)
        r = run(["git", "commit", "-q", "-m", f"split payload {SLUG} acct-{N}"], cwd=GH)
        ssh_cmd = (f"ssh -p 443 -i {HOME}/.ssh/id_ed25519 -o IdentitiesOnly=yes "
                   f"-o ProxyCommand='python3 {GH}/connect-proxy.py %h %p' -o StrictHostKeyChecking=accept-new")
        r = run(["git", "push", "origin", "HEAD:main"], cwd=GH,
                env_extra={"GIT_SSH_COMMAND": ssh_cmd}, timeout=600)
        result["pushed"] = (r.returncode == 0)
        result["push_tail"] = (r.stdout + r.stderr)[-400:]
        log(f"push origin: {'ok' if result['pushed'] else 'FAILED'}")
        if not result["pushed"]:
            result["error"] = "git push failed"
            print(json.dumps(result)); sys.exit(1)

    result["ok"] = True
    print(json.dumps(result))

if __name__ == "__main__":
    main()
