#!/usr/bin/env python3
"""SPLIT pipeline, runner side: DIRECT submit (no VM round-trip).

Runs on a GitHub-hosted runner right after split-build-one.py, in the same
job. The payload (prepared on the VM by split-prepare.py, user order 24 Nov
2026 "langsung upload submit tanpa perlu melalui vm", risks accepted) carries
inside its encrypted tar: the account credentials + keypair, the real system
prompt, a snapshot of the parent Muse session, and the muse-usage file.
This script:
  1. decrypts split-result-<N>.enc (produced by split-build-one.py) and reads
     result-<N>.json (build must be ok)
  2. verifies the payload repo is at the built commit
  3. stages the build's Codex sessions into a temp CODEX_HOME
  4. runs kit bundle -> check -> submit -> API verify with the payload
     credentials (never printed), using the same flags as the VM finish
     (split-finish.py). The bundle's --provider/--model come from the
     workflow's `provider` dispatch input (env PROVIDER): seek100 ->
     seek100/deepseek-v4.1-flash-free, xingchi -> xingchi/gpt-6.1-sol.
     They MUST match the actual runner build: the kit's bundle refuses a
     declared model that contradicts what the top-level Codex sessions
     record (contradictedAgentFacts) — run #6 (45507032850, xingchi builds)
     failed exactly here because this script hardcoded seek100.
  5. writes submitted-<N>.json to cwd (submission, claim, server_status) and
     exits 0 on success, 1 on failure. The workflow uploads the JSON as an
     artifact either way; the encrypted result artifact is still uploaded too,
     so a failed direct submit can fall back to the VM finish.

Usage: split-submit-one.py <N>
Env: SPLIT_BASE (payload dir), BUNDLE_PASSPHRASE (to decrypt the result).
Never prints secret values.
"""
import gzip, json, os, pathlib, shutil, subprocess, sys, tarfile, time, urllib.request, uuid
from datetime import datetime

N = int(sys.argv[1])
BASE = pathlib.Path(os.environ.get("SPLIT_BASE", f"/home/runner/work/_temp/split-acct-{N}")).resolve()
REPO = BASE / "repo"
META = json.loads((BASE / "meta.json").read_text())
SLUG = META["slug"]; HANDLE = META["handle"]; WALLET = META["wallet"]; TITLE = META["title"]
KIT = BASE / "kit" / f"{SLUG}-kit"
ENC = pathlib.Path.cwd() / f"split-result-{N}.enc"
OUT_JSON = pathlib.Path.cwd() / f"submitted-{N}.json"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

result = {"n": N, "handle": HANDLE, "slug": SLUG, "ok": False}

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] acct{N} submit: {msg}", flush=True)

def finish(**kw):
    result.update(kw)
    OUT_JSON.write_text(json.dumps(result, indent=1))
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)

def run(cmd, cwd=None, env_extra=None, timeout=900):
    env = dict(os.environ)
    if env_extra: env.update(env_extra)
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)

def decrypt(enc_path, tar_path, pw):
    env = dict(os.environ); env["GH_BUNDLE_PASS"] = pw
    r = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000",
                        "-in", str(enc_path), "-out", str(tar_path), "-pass", "env:GH_BUNDLE_PASS"],
                       env=env, capture_output=True, text=True, timeout=600)
    return r.returncode == 0

try:
    pw = "".join((os.environ.get("BUNDLE_PASSPHRASE") or "").split())
    if not pw:
        finish(error="BUNDLE_PASSPHRASE missing")
    # Bundle must declare the ACTUAL build provider/model. The kit's bundle
    # refuses a declared model that contradicts what the top-level Codex
    # sessions record (contradictedAgentFacts in cli/makefun.mjs): run #6
    # (xingchi builds, sessions recorded gpt-6.1-sol) failed here because
    # this script hardcoded seek100/deepseek-v4.1-flash-free. The workflow
    # passes PROVIDER (its `provider` dispatch input) to this step.
    PROVIDER = (os.environ.get("PROVIDER") or "seek100").strip().lower()
    _pm = {"seek100": ("seek100", "deepseek-v4.1-flash-free"),
           "xingchi": ("xingchi", "gpt-6.1-sol")}
    if PROVIDER not in _pm:
        finish(error=f"unknown PROVIDER {PROVIDER!r} (want seek100 or xingchi)")
    BUNDLE_PROVIDER, BUNDLE_MODEL = _pm[PROVIDER]
    log(f"bundle provider={BUNDLE_PROVIDER} model={BUNDLE_MODEL}")
    if not ENC.exists():
        finish(error="split-result enc missing (build step produced nothing)")
    work = BASE / "submit-work"
    if work.exists(): shutil.rmtree(work)
    work.mkdir(parents=True)
    if not decrypt(ENC, work / "result.tar", pw):
        finish(error="decrypt of split-result failed")
    with tarfile.open(work / "result.tar") as tf:
        tf.extractall(work / "out")
    out = work / "out"
    rj = out / f"result-{N}.json"
    if not rj.exists():
        finish(error="result json missing in decrypted tar")
    build_result = json.loads(rj.read_text())
    result["build"] = {k: build_result.get(k) for k in ("ok", "commit", "codex_session", "session_files", "error")}
    if not build_result.get("ok"):
        finish(error=f"runner build failed: {build_result.get('error')}")
    commit = build_result.get("commit")
    log(f"runner build ok, commit {commit}")

    # repo must be at the built commit (build step already reset it there)
    sha = run(["git", "rev-parse", "HEAD"], cwd=REPO).stdout.strip()
    if commit and sha != commit:
        bundle_file = out / "entry.bundle"
        if not bundle_file.exists():
            finish(error=f"commit mismatch local {sha} != runner {commit} and no entry.bundle to fetch")
        r = run(["git", "fetch", str(bundle_file), "HEAD"], cwd=REPO, timeout=300)
        run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=REPO, timeout=300)
        sha = run(["git", "rev-parse", "HEAD"], cwd=REPO).stdout.strip()
        if sha != commit:
            finish(error=f"commit mismatch after fetch: local {sha} != runner {commit}")
    result["commit"] = sha
    log(f"repo at {sha}")

    # stage build sessions into a temp CODEX_HOME (preserve ~/.codex layout)
    codex_home = work / "codex-home"
    (codex_home / "sessions").mkdir(parents=True)
    sess_src = out / "sessions"
    n_sess = 0
    if sess_src.exists():
        for sf in sess_src.rglob("*.jsonl"):
            dest = codex_home / "sessions" / sf.relative_to(sess_src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sf, dest); n_sess += 1
    result["session_files"] = n_sess
    log(f"{n_sess} codex session files staged")
    if n_sess == 0:
        finish(error="no codex session files in result (bundle would fail)")

    # parent session snapshot (gzipped in payload) + system prompt + usage
    parent = BASE / "parent-session.jsonl"
    if not parent.exists() and (BASE / "parent-session.jsonl.gz").exists():
        with gzip.open(BASE / "parent-session.jsonl.gz", "rb") as f_in, open(parent, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    if not parent.exists():
        finish(error="parent-session snapshot missing from payload")
    sys_prompt = BASE / "system-prompt-real.txt"
    if not sys_prompt.exists():
        finish(error="system-prompt-real.txt missing from payload")
    # muse-usage rows for the bundle. The kit refuses --muse-usage when it
    # holds no usage row (run #7 failed here for all 3 accounts: the
    # payload's usage file was empty because the VM's file for this slug
    # has no rows, and the runner build's usage only exists after the
    # build). Generate the rows on the runner from the staged Codex
    # sessions: each `token_usage_record` line becomes one row (agent_id =
    # Codex session id, model = the actual build model), mirroring the VM
    # lane's muse.db rows. Payload rows, if any (parent session), are kept
    # first.
    usage_file = BASE / f"muse-usage-{SLUG}.jsonl"
    usage_rows = []
    if usage_file.exists():
        usage_rows = [ln.strip() for ln in usage_file.read_text(errors="replace").splitlines() if ln.strip()]
    n_codex_rows = 0
    for sf in sorted(codex_home.rglob("*.jsonl")):
        sess_id = None
        try:
            lines = sf.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for ln in lines:
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            pay = rec.get("payload") or {}
            if not sess_id and isinstance(pay, dict):
                sess_id = pay.get("session_id") or pay.get("id")
            if rec.get("type") != "token_usage_record":
                continue
            u = (pay.get("usage") or {}) if isinstance(pay, dict) else {}
            if "input_tokens" not in u or "output_tokens" not in u:
                continue
            ts = rec.get("timestamp") or ""
            try:
                created = int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp())
            except Exception:
                created = int(time.time())
            row = {
                "agent_id": sess_id or sf.stem,
                "message_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex:{sess_id}:{rec.get('ordinal')}")),
                "created_at": created,
                "input_tokens": int(u["input_tokens"]),
                "output_tokens": int(u["output_tokens"]),
                "model": BUNDLE_MODEL,
            }
            usage_rows.append(json.dumps(row))
            n_codex_rows += 1
    usage_file.write_text("\n".join(usage_rows) + ("\n" if usage_rows else ""))
    log(f"muse-usage rows: {len(usage_rows)} total ({n_codex_rows} from codex sessions, {len(usage_rows) - n_codex_rows} from payload)")
    if not usage_rows:
        finish(error="no usage rows: payload usage file empty and no token_usage_record in codex sessions")
    creds_file = BASE / "credentials.json"
    keypair_file = BASE / "keypair.json"
    for p in (creds_file, keypair_file):
        if not p.exists():
            finish(error=f"payload credential missing: {p.name}")
    bundle_out = work / f"transcripts-{SLUG}-acct{N}.tar.gz"

    # bundle (same flags as VM split-finish.py, provider/model = actual runner build)
    r = run(["node", "cli/makefun.mjs", "bundle", "--repo", str(REPO), "--out", str(bundle_out),
             "--system-prompt", str(sys_prompt),
             "--prompt-file", str(BASE / "brief.txt"),
             "--prompt-origin", "handed-off",
             "--prompt-via", "parent agent handoff",
             "--harness", "codex", "--harness-version", "0.170.0", "--provider", BUNDLE_PROVIDER,
             "--model", BUNDLE_MODEL, "--effort", "default", "--mcp", "none",
             "--parent-session", str(parent),
             "--muse-usage", str(usage_file),
             "--no-family",
             "--json"], cwd=KIT, env_extra={"CODEX_HOME": str(codex_home)}, timeout=900)
    log(f"bundle exit {r.returncode}")
    if r.returncode != 0 or not bundle_out.exists():
        finish(error="bundle failed", bundle_tail=(r.stdout + r.stderr)[-1200:])
    try:
        bj, _ = json.JSONDecoder().raw_decode(r.stdout[r.stdout.find("{"):])
        result["bundle_sessions"] = len(bj.get("manifest", {}).get("sessions", []))
        log(f"bundle sessions: {result['bundle_sessions']}")
    except Exception as e:
        log(f"bundle json parse note: {e}")

    # check
    r = run(["node", "cli/makefun.mjs", "check", "--kit", ".", "--repo", str(REPO),
             "--transcripts", str(bundle_out), "--json"], cwd=KIT, timeout=900)
    log(f"check exit {r.returncode}")
    if r.returncode != 0:
        finish(error="check failed", check_tail=(r.stdout + r.stderr)[-2000:])

    # submit (payload credentials + keypair; never printed)
    env = {"MAKEFUN_CREDENTIALS": str(creds_file)}
    r = run(["node", "cli/makefun.mjs", "submit", "--competition", SLUG,
             "--source", str(REPO), "--commit", sha, "--bundle", str(bundle_out),
             "--keypair", str(keypair_file),
             "--title", TITLE, "--json"], cwd=KIT, env_extra=env, timeout=900)
    log(f"submit exit {r.returncode}")
    out_text = r.stdout + r.stderr
    sj = {}
    try:
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
        finish(error="submit failed (no submission id parsed)", submit_tail=out_text[-1500:])
    if r.returncode != 0 or not result.get("submission"):
        finish(error="submit failed", submit_tail=out_text[-1500:])

    # verify server status
    creds = json.load(open(creds_file))
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
    log(f"DONE live submission={result.get('submission')} claim={result.get('claim')} status={result.get('server_status')}")
    result["ok"] = True
    finish()
except SystemExit:
    raise
except Exception as e:
    finish(error=f"driver exception: {type(e).__name__}: {str(e)[:300]}")
