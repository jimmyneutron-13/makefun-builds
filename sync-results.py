#!/usr/bin/env python3
"""VM-side: pull result-*.json artifacts from GitHub Actions and append
'live' rows to the correct make.fun ledger (friend 701-879, chain1000 880-1279).

Token: env GH_TOKEN, else ~/.github-token (must be mode 600). This script does
NOT create tokens. Usage:
  python3 sync-results.py --repo OWNER/REPO [--run-id ID] [--dry-run]
Without --run-id it scans the 20 most recent workflow runs.
Already-live accounts are skipped. Failed results are reported, not appended.
"""
import argparse, io, json, os, pathlib, sys, urllib.request, zipfile

HOME = pathlib.Path.home()
MAKEFUN = HOME / "workspace" / "makefun"
LEDGERS = [(879, MAKEFUN / "accounts-friend20261121.jsonl"),
           (10**9, MAKEFUN / "accounts-chain1000.jsonl")]

def token():
    t = os.environ.get("GH_TOKEN")
    if t: return t
    f = HOME / ".github-token"
    if f.exists():
        if oct(f.stat().st_mode)[-3:] != "600":
            print(f"WARNING: {f} is not mode 600", file=sys.stderr)
        return f.read_text().strip()
    print("FATAL: no GH_TOKEN env and no ~/.github-token file", file=sys.stderr)
    sys.exit(2)

def api(path, tok, raw=False):
    req = urllib.request.Request(f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "gh-builds-sync"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if raw else json.loads(data)

def ledger_for(n):
    for cap, path in LEDGERS:
        if n <= cap: return path
    return LEDGERS[-1][1]

def live_set():
    s = set()
    for _, path in LEDGERS:
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    d = json.loads(line)
                    if d.get("status") == "live": s.add(d["n"])
                except Exception: pass
    return s

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="OWNER/REPO")
    ap.add_argument("--run-id", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    tok = token()
    if args.run_id:
        run_ids = [args.run_id]
    else:
        runs = api(f"/repos/{args.repo}/actions/runs?per_page=20", tok)
        run_ids = [r["id"] for r in runs.get("workflow_runs", [])]
    live = live_set()
    added = failed = skipped = 0
    for rid in run_ids:
        arts = api(f"/repos/{args.repo}/actions/runs/{rid}/artifacts?per_page=100", tok)
        for art in arts.get("artifacts", []):
            if not art["name"].startswith("result-"): continue
            blob = api(f"/repos/{args.repo}/actions/artifacts/{art['id']}/zip", tok, raw=True)
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                for name in zf.namelist():
                    if not (name.startswith("result-") and name.endswith(".json")): continue
                    res = json.loads(zf.read(name))
                    n = res.get("n")
                    if n in live:
                        skipped += 1; continue
                    if res.get("ok") and res.get("submission"):
                        row = {"n": n, "handle": res.get("handle"), "status": "live",
                               "submission": res.get("submission"), "claim": res.get("claim"),
                               "server_status": res.get("server_status"), "via": "github-actions"}
                        if args.dry_run:
                            print(f"DRY-RUN would append live acct {n} -> {ledger_for(n).name}")
                        else:
                            with open(ledger_for(n), "a") as f:
                                f.write(json.dumps(row) + "\n")
                        live.add(n); added += 1
                    else:
                        failed += 1
                        print(f"acct {n}: not ok ({res.get('error', 'unknown')}) — left for retry")
    print(f"done: added={added} failed={failed} skipped_already_live={skipped}")

if __name__ == "__main__":
    main()
