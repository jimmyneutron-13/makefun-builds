#!/usr/bin/env python3
"""SPLIT pipeline, VM side step 2: finish split-built entries.

For each account N: download the newest `split-result-<N>` artifact from the
rasjdahw/makefun-builds Actions runs (GitHub API, token ~/.gh-rasjdahw-token,
never printed), decrypt it, fetch the built commit into the local anchored
repo (created by split-prepare.py), then run bundle -> check -> submit ->
verify locally with the account's own credentials + keypair, and append the
live record to the account's origin ledger.

Idempotent: accounts already live in their ledger for the slug, or with a
finished.json state file recording a submission, are skipped.

Usage: split-finish.py <N> [<N> ...] [--slug news-image-20261121] [--dry-run]
--dry-run stops before submit (and before ledger writes); a missing artifact
is reported, not an error. Never prints secret values.
"""
import argparse, json, os, pathlib, shutil, subprocess, sys, tarfile, time, zipfile

HOME = pathlib.Path.home()
GH = HOME / "workspace" / "gh-builds"
MAKEFUN = HOME / "workspace" / "makefun"
# Must match split-prepare.py (outside the gh-builds git repo; makefun init
# refuses subdirectories of an existing repository).
SPLIT_WORK = MAKEFUN / "split-work"
REPO_SLUG = "rasjdahw/makefun-builds"
PARENT_SESSION = "/home/hatch/agents/agent-3ec4af1d-5ebf-4d29-a262-b348c9e24464/sessions/3ec4af1d-5ebf-4d29-a262-b348c9e24464.jsonl"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

def log(n, msg):
    print(f"[{time.strftime('%H:%M:%S')}] acct{n}: {msg}", flush=True)

def run(cmd, cwd=None, env_extra=None, timeout=1800):
    env = dict(os.environ)
    if env_extra: env.update(env_extra)
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)

def passphrase():
    pf = HOME / ".gh-bundle-pass"
    if not pf.exists():
        raise SystemExit("~/.gh-bundle-pass missing")
    return pf.read_text().strip()

def gh_token():
    tf = HOME / ".gh-rasjdahw-token"
    if not tf.exists():
        raise SystemExit("~/.gh-rasjdahw-token missing")
    return tf.read_text().strip()

def gh_api(path):
    """GET GitHub API JSON via curl (proxy from env). Token never printed."""
    r = subprocess.run(["curl", "-s", "-m", "60", f"https://api.github.com{path}",
                        "-H", f"Authorization: Bearer {gh_token()}",
                        "-H", "Accept: application/vnd.github+json",
                        "-H", f"User-Agent: {UA}"],
                       capture_output=True, text=True, timeout=90)
    try:
        return json.loads(r.stdout)
    except Exception:
        return None

def find_artifact(n):
    data = gh_api(f"/repos/{REPO_SLUG}/actions/artifacts?name=split-result-{n}&per_page=20")
    if not data:
        return None
    arts = [a for a in data.get("artifacts", []) if not a.get("expired")]
    if not arts:
        return None
    arts.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    return arts[0]

def download_artifact(art, dest_zip):
    r = subprocess.run(["curl", "-sL", "-m", "600", "-o", str(dest_zip),
                        art["archive_download_url"],
                        "-H", f"Authorization: Bearer {gh_token()}",
                        "-H", f"User-Agent: {UA}"],
                       capture_output=True, text=True, timeout=660)
    return r.returncode == 0 and dest_zip.exists() and dest_zip.stat().st_size > 0

def decrypt(enc_path, tar_path, pw):
    env = dict(os.environ); env["GH_BUNDLE_PASS"] = pw
    r = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000",
                        "-in", str(enc_path), "-out", str(tar_path), "-pass", "env:GH_BUNDLE_PASS"],
                       env=env, capture_output=True, text=True, timeout=600)
    return r.returncode == 0

def already_live(ledger, n, slug):
    if not ledger.exists(): return False
    for line in ledger.read_text().splitlines():
        try: d = json.loads(line)
        except Exception: continue
        if d.get("n") == n and d.get("status") == "live" and (d.get("entry") or {}).get("bounty") == slug:
            return True
    return False

def finish_one(n, slug, dry_run):
    result = {"n": n, "slug": slug, "ok": False}
    work = SPLIT_WORK / slug / f"acct-{n}"
    meta_path = work / "meta.json"
    if not meta_path.exists():
        result["error"] = "no split-work meta (run split-prepare.py first)"
        log(n, result["error"]); return result
    meta = json.loads(meta_path.read_text())
    HANDLE, WALLET, TITLE = meta["handle"], meta["wallet"], meta["title"]
    ledger = MAKEFUN / meta["ledger"]
    result["handle"] = HANDLE; result["ledger"] = str(ledger)
    state_path = work / "finished.json"
    if state_path.exists():
        st = json.loads(state_path.read_text())
        if st.get("submission"):
            result.update({"ok": True, "skipped": "already finished", "submission": st["submission"], "claim": st.get("claim")})
            log(n, "already finished (state file), skipping"); return result
    if already_live(ledger, n, slug):
        result.update({"ok": True, "skipped": "already live in ledger"})
        log(n, "already live in ledger, skipping"); return result

    # 1. artifact
    art = find_artifact(n)
    if not art:
        result["error"] = "no split-result artifact found (workflow not dispatched yet?)"
        log(n, result["error"]); return result
    result["artifact_id"] = art["id"]; result["artifact_created"] = art.get("created_at")
    log(n, f"artifact {art['id']} created {art.get('created_at')}")
    dl = work / "download"
    if dl.exists(): shutil.rmtree(dl)
    dl.mkdir(parents=True)
    if not download_artifact(art, dl / "artifact.zip"):
        result["error"] = "artifact download failed"; log(n, result["error"]); return result
    with zipfile.ZipFile(dl / "artifact.zip") as zf:
        zf.extractall(dl)
    enc = dl / f"split-result-{n}.enc"
    if not enc.exists():
        result["error"] = "encrypted result missing in artifact zip"; log(n, result["error"]); return result
    if not decrypt(enc, dl / "result.tar", passphrase()):
        result["error"] = "decrypt failed"; log(n, result["error"]); return result
    out = dl / "out"
    out.mkdir(exist_ok=True)
    with tarfile.open(dl / "result.tar") as tf:
        tf.extractall(out)
    rj = out / f"result-{n}.json"
    if not rj.exists():
        result["error"] = "result json missing in decrypted tar"; log(n, result["error"]); return result
    build_result = json.loads(rj.read_text())
    result["build"] = {k: build_result.get(k) for k in ("ok", "commit", "codex_session", "session_files", "error")}
    if not build_result.get("ok"):
        result["error"] = f"runner build failed: {build_result.get('error')}"
        log(n, result["error"]); return result
    log(n, f"runner build ok, commit {build_result.get('commit')}")

    # 2. fetch built commit into the local anchored repo
    repo = work / "repo"
    bundle_file = out / "entry.bundle"
    if not bundle_file.exists():
        result["error"] = "entry.bundle missing in result tar"; log(n, result["error"]); return result
    r = run(["git", "fetch", str(bundle_file), "HEAD"], cwd=repo, timeout=300)
    if r.returncode != 0:
        result["error"] = "git fetch bundle failed"; result["tail"] = (r.stdout + r.stderr)[-600:]
        log(n, result["error"]); return result
    run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=repo, timeout=300)
    sha = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    result["commit"] = sha
    if build_result.get("commit") and sha != build_result["commit"]:
        result["error"] = f"commit mismatch: local {sha} != runner {build_result['commit']}"
        log(n, result["error"]); return result
    log(n, f"repo at {sha}")

    # 3. sessions -> temp CODEX_HOME (preserve ~/.codex layout)
    codex_home = work / "codex-home"
    if codex_home.exists(): shutil.rmtree(codex_home)
    (codex_home / "sessions").mkdir(parents=True)
    sess_src = out / "sessions"
    n_sess = 0
    if sess_src.exists():
        for sf in sess_src.rglob("*.jsonl"):
            dest = codex_home / "sessions" / sf.relative_to(sess_src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sf, dest); n_sess += 1
    result["session_files"] = n_sess
    log(n, f"{n_sess} codex session files staged")
    if n_sess == 0:
        result["error"] = "no codex session files in result (bundle would fail)"
        log(n, result["error"]); return result

    if dry_run:
        result["ok"] = True; result["dry_run"] = "stopped before bundle/check/submit"
        log(n, "DRY-RUN: would now bundle + check + submit")
        return result

    # 4. bundle transcripts (local only; CODEX_HOME points at staged sessions)
    kit = work / "kit" / f"{slug}-kit"
    bundle_out = work / f"transcripts-{slug}-acct{n}.tar.gz"
    usage_file = HOME / ".makefun" / f"muse-usage-{slug}.jsonl"
    usage_file.touch(exist_ok=True)
    r = run(["node", "cli/makefun.mjs", "bundle", "--repo", str(repo), "--out", str(bundle_out),
             "--system-prompt", str(MAKEFUN / "system-prompt-real.txt"),
             "--prompt-file", str(work / "brief.txt"),
             "--prompt-origin", "handed-off",
             "--prompt-via", "parent agent handoff",
             "--harness", "codex", "--harness-version", "0.170.0", "--provider", "xingchi",
             "--model", "gpt-6.1-sol", "--effort", "default", "--mcp", "none",
             "--parent-session", PARENT_SESSION,
             "--muse-usage", str(usage_file),
             "--no-family",
             "--json"], cwd=kit, env_extra={"CODEX_HOME": str(codex_home)}, timeout=900)
    log(n, f"bundle exit {r.returncode}")
    if r.returncode != 0 or not bundle_out.exists():
        result["error"] = "bundle failed"; result["bundle_tail"] = (r.stdout + r.stderr)[-1200:]
        log(n, result["error"]); return result
    try:
        bj, _ = json.JSONDecoder().raw_decode(r.stdout[r.stdout.find("{"):])
        result["bundle_sessions"] = len(bj.get("manifest", {}).get("sessions", []))
        log(n, f"bundle sessions: {result['bundle_sessions']}")
    except Exception as e:
        log(n, f"bundle json parse note: {e}")

    # 5. check
    r = run(["node", "cli/makefun.mjs", "check", "--kit", ".", "--repo", str(repo),
             "--transcripts", str(bundle_out), "--json"], cwd=kit, timeout=900)
    log(n, f"check exit {r.returncode}")
    if r.returncode != 0:
        result["error"] = "check failed"; result["check_tail"] = (r.stdout + r.stderr)[-2000:]
        log(n, result["error"]); return result

    # 6. submit (credentials + keypair, local only)
    env = {"MAKEFUN_CREDENTIALS": str(HOME / ".makefun" / f"credentials-{n}")}
    r = run(["node", "cli/makefun.mjs", "submit", "--competition", slug,
             "--source", str(repo), "--commit", sha, "--bundle", str(bundle_out),
             "--keypair", str(MAKEFUN / f"makefun-keypair-{n}.json"),
             "--title", TITLE, "--json"], cwd=kit, env_extra=env, timeout=900)
    log(n, f"submit exit {r.returncode}")
    out_text = r.stdout + r.stderr
    try:
        sj = {}
        dec = json.JSONDecoder(); idx = 0
        while True:
            i = out_text.find("{", idx)
            if i < 0: break
            try:
                obj, _ = dec.raw_decode(out_text[i:])
                if isinstance(obj, dict) and ("submission_id" in obj or "claim_code" in obj):
                    sj = obj; break
            except Exception:
                pass
            idx = i + 1
        if not sj: raise ValueError("no submit JSON object found")
        result["submission"] = sj.get("id") or sj.get("submission_id") or sj.get("submission", {}).get("id")
        result["claim"] = sj.get("claim_code") or sj.get("claim") or sj.get("submission", {}).get("claim_code")
    except Exception:
        result["submit_tail"] = out_text[-1500:]
    if r.returncode != 0 or not result.get("submission"):
        result["error"] = "submit failed"; result["submit_tail"] = out_text[-1500:]
        log(n, result["error"]); return result

    # 7. verify server status
    import urllib.request
    creds = json.load(open(HOME / ".makefun" / f"credentials-{n}"))
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

    # 8. state + ledger
    result["ok"] = True
    state_path.write_text(json.dumps({k: result.get(k) for k in ("submission", "claim", "commit", "server_status")}, indent=1))
    rec = {"n": n, "handle": HANDLE, "entry": {"bounty": slug, "submission": result.get("submission"),
           "claim": result.get("claim"), "commit": sha, "server_status": result.get("server_status")},
           "status": "live", "at_wib": time.strftime("%d %b %H:%M")}
    with open(ledger, "a") as f: f.write(json.dumps(rec) + "\n")
    log(n, f"DONE live submission={result.get('submission')} claim={result.get('claim')} -> {ledger.name}")
    return result

def main():
    ap = argparse.ArgumentParser(description="Finish split-built entries on the VM (bundle/check/submit).")
    ap.add_argument("ns", nargs="+", type=int, help="account numbers")
    ap.add_argument("--slug", default="news-image-20261121")
    ap.add_argument("--dry-run", action="store_true", help="stop before bundle/check/submit; no ledger writes")
    args = ap.parse_args()
    results = []
    for n in args.ns:
        try:
            results.append(finish_one(n, args.slug, args.dry_run))
        except Exception as e:
            results.append({"n": n, "ok": False, "error": f"exception: {type(e).__name__}: {str(e)[:300]}"})
    print(json.dumps(results, indent=1))
    sys.exit(0 if all(r.get("ok") for r in results) else 1)

if __name__ == "__main__":
    main()
