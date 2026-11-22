#!/usr/bin/env python3
"""SPLIT pipeline, VM side step 1: prepare an anchored build payload for ONE
make.fun account and push it (encrypted) to the rasjdahw GitHub lane.

Credentials and keypair NEVER leave this VM. This script:
  1. looks up the account in its origin ledger (~/workspace/makefun/accounts-*.jsonl)
  2. downloads a fresh public kit (https://make.fun/c/<slug>/kit)
  3. runs `makefun init` locally with the account's credentials + keypair
     (drand root commit + anchor), verifies the anchor wallet
  4. writes the build brief (same template as the VM spillover lane, paths
     rewritten to the fixed runner layout, credentials line removed)
  5. tars repo+brief+anchor+meta+news+render script+codex profile,
     encrypts with openssl aes-256-cbc (passphrase from ~/.gh-bundle-pass,
     env only, never printed) -> payloads/<slug>/acct-<N>.enc
  6. commits + pushes the payload to the rasjdahw remote

Usage: split-prepare.py <N> [--slug news-image-20261121] [--title "..."]
       [--force] [--no-push]
Prints a final JSON line. Never prints secret values.
"""
import argparse, json, os, pathlib, re, shutil, subprocess, sys, tarfile, time

HOME = pathlib.Path.home()
GH = HOME / "workspace" / "gh-builds"
MAKEFUN = HOME / "workspace" / "makefun"
# NOTE: split-work must live OUTSIDE the gh-builds git repo: `makefun init`
# refuses to run in a subdirectory of an existing git repository.
SPLIT_WORK = MAKEFUN / "split-work"
PAYLOADS = GH / "payloads"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TITLES = {"news-image-20261121": "The Atlas Daily — Saturday 21 November 2026",
          "news-image-20261120": "The Atlas Daily — Friday 20 November 2026"}
# Fixed runner layout (GitHub-hosted ubuntu: RUNNER_TEMP=/home/runner/work/_temp;
# the workflow symlinks this path if RUNNER_TEMP differs).
RUNNER_BASE = "/home/runner/work/_temp/split-acct-{N}"

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
    ap.add_argument("--slug", default="news-image-20261121")
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

    # 1. fresh kit (public download, no auth)
    if not (kit / "cli" / "makefun.mjs").exists():
        kitdir.mkdir(parents=True, exist_ok=True)
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
    # credentials line: no credentials exist on the runner; Codex must not run makefun CLI
    brief = re.sub(r"CREDENTIALS:.*",
                   "CREDENTIALS: none on this machine — do NOT run any makefun CLI command "
                   "(init is already done; bundle/check/submit happen elsewhere). Never print or share any credential value.",
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
    with tarfile.open(tar_path, "w") as tf:
        tf.add(repo, arcname="repo")
        tf.add(brief_path, arcname="brief.txt")
        tf.add(work / "anchor.txt", arcname="anchor.txt")
        tf.add(meta_path, arcname="meta.json")
        if news_src.exists(): tf.add(news_src, arcname=news_name)
        if shoot_src.exists(): tf.add(shoot_src, arcname="shoot-news105.mjs")
        if toml_src.exists(): tf.add(toml_src, arcname="xingchi.config.toml")
    encrypt(tar_path, enc_path, passphrase())
    tar_path.unlink(missing_ok=True)
    result["payload"] = str(enc_path.relative_to(GH))
    result["payload_bytes"] = enc_path.stat().st_size
    log(f"payload encrypted: {result['payload']} ({result['payload_bytes']} bytes)")

    # 5. commit + push to rasjdahw
    if args.no_push:
        result["pushed"] = False
    else:
        rel = str(enc_path.relative_to(GH))
        r = run(["git", "add", rel], cwd=GH)
        r = run(["git", "commit", "-q", "-m", f"split payload {SLUG} acct-{N}"], cwd=GH)
        ssh_cmd = (f"ssh -p 443 -i {HOME}/.ssh/id_ed25519_rasjdahw -o IdentitiesOnly=yes "
                   f"-o ProxyCommand='python3 {GH}/connect-proxy.py %h %p' -o StrictHostKeyChecking=accept-new")
        r = run(["git", "push", "rasjdahw", "HEAD:main"], cwd=GH,
                env_extra={"GIT_SSH_COMMAND": ssh_cmd}, timeout=600)
        result["pushed"] = (r.returncode == 0)
        result["push_tail"] = (r.stdout + r.stderr)[-400:]
        log(f"push rasjdahw: {'ok' if result['pushed'] else 'FAILED'}")
        if not result["pushed"]:
            result["error"] = "git push failed"
            print(json.dumps(result)); sys.exit(1)

    result["ok"] = True
    print(json.dumps(result))

if __name__ == "__main__":
    main()
