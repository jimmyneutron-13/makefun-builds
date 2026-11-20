#!/usr/bin/env python3
"""Build encrypted bundles for the GitHub Actions make.fun build lane.

- accounts.json: GitHub-lane accounts (friend batch not-yet-live + chain1000),
  read from the VM ledgers. No secrets.
- bundles/acct-<n>.enc: tar of (makefun-keypair-<n>.json, .makefun/credentials-<n>)
  encrypted with openssl aes-256-cbc -salt -pbkdf2.
- bundles/shared.enc: tar of shared/ (parent-session.jsonl, system-prompt-real.txt,
  muse-usage-news-image-20261120.jsonl snapshot, brief template,
  news-20261120-dense.md, shoot-news105.mjs, xingchi.config.toml).

Passphrase = Xingchi Hermes api_key (user key), read in-process from
~/xingchiapi/credentials-hermes.json on PC and passed to openssl via env only.
The value is never printed, logged, or written to disk in plaintext.
On GitHub, the same value is stored as the XINGCHI_API_KEY secret, so the
workflow decrypts with `-pass env:XINGCHI_API_KEY` — one secret total.

Usage: python3 make-bundles.py [--verify-one]
"""
import json, os, pathlib, subprocess, sys, tarfile, tempfile

HOME = pathlib.Path.home()
GH = HOME / "gh-bundles-pc"
MAKEFUN = HOME / "makefun-probe"  # keypairs live here on PC
BUNDLES = GH / "bundles"
PARENT_SESSION = GH / "shared-src" / "parent-session.jsonl"
USAGE_SRC = GH / "shared-src" / "muse-usage-news-image-20261120.jsonl"

def passphrase():
    d = json.load(open(HOME / "xingchiapi" / "credentials-hermes.json"))
    return d["api_key"]

def encrypt(tar_path, enc_path, pw):
    env = dict(os.environ); env["GH_BUNDLE_PASS"] = pw
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000",
                        "-in", str(tar_path), "-out", str(enc_path), "-pass", "env:GH_BUNDLE_PASS"],
                       env=env, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"openssl encrypt failed for {enc_path.name}: {r.stderr[-300:]}")

def lane_accounts():
    out = []
    pc_done = set()
    pc_state = HOME / "makefun-probe" / "friend-pc-build-state.json"
    if pc_state.exists():
        st = json.loads(pc_state.read_text())
        pc_done = {int(k) for k, v in st.items() if v.get("status") == "done"}
    for fname in ("accounts-friend20261121.jsonl", "accounts-chain1000.jsonl"):
        rows = [json.loads(l) for l in open(GH / "ledgers" / fname) if l.strip()]
        ok = {r["n"]: r for r in rows if r.get("status") == "ok"}
        live = {r["n"] for r in rows if r.get("status") == "live"} | pc_done
        for n in sorted(ok):
            if n not in live:
                out.append({"n": n, "handle": ok[n]["handle"], "wallet": ok[n]["wallet"]})
    return out

def main():
    BUNDLES.mkdir(parents=True, exist_ok=True)
    accts = lane_accounts()
    (GH / "accounts.json").write_text(json.dumps(accts, indent=1))
    print(f"accounts.json: {len(accts)} accounts")
    pw = passphrase()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        # shared bundle
        shared = td / "shared"; shared.mkdir()
        copies = [
            (PARENT_SESSION, "parent-session.jsonl"),
            (GH / "shared-src" / "system-prompt-real.txt", "system-prompt-real.txt"),
            (USAGE_SRC, "muse-usage-news-image-20261120.jsonl"),
            (GH / "shared-src" / "brief-news-image-20261120-minimal-pc-template.txt",
             "brief-news-image-20261120-minimal-pc-template.txt"),
            (GH / "shared-src" / "news-20261120-dense.md", "news-20261120-dense.md"),
            (GH / "shared-src" / "shoot-news105.mjs", "shoot-news105.mjs"),
            (GH / "shared-src" / "xingchi.config.toml", "xingchi.config.toml"),
        ]
        for src, name in copies:
            if not src.exists():
                print(f"FATAL: missing shared source {src}"); sys.exit(2)
            (shared / name).write_bytes(src.read_bytes())
        tar_path = td / "shared.tar"
        with tarfile.open(tar_path, "w") as tf:
            tf.add(shared, arcname="shared")
        encrypt(tar_path, BUNDLES / "shared.enc", pw)
        print("shared.enc written")
        # per-account bundles
        for a in accts:
            n = a["n"]
            kp = MAKEFUN / f"makefun-keypair-{n}.json"
            cr = HOME / ".makefun" / f"credentials-{n}"
            if not kp.exists() or not cr.exists():
                print(f"FATAL: missing keypair/credentials for acct {n}"); sys.exit(2)
            tar_path = td / f"acct-{n}.tar"
            with tarfile.open(tar_path, "w") as tf:
                tf.add(kp, arcname=f"makefun-keypair-{n}.json")
                tf.add(cr, arcname=f".makefun/credentials-{n}")
            encrypt(tar_path, BUNDLES / f"acct-{n}.enc", pw)
            tar_path.unlink()
        print(f"acct bundles written: {len(accts)}")
    encs = sorted(BUNDLES.glob("*.enc"))
    print(f"total .enc files: {len(encs)} (expected {len(accts) + 1})")
    if "--verify-one" in sys.argv:
        verify_one(pw)

def verify_one(pw):
    """Decrypt shared.enc into /tmp, list tar contents, then delete plaintext."""
    with tempfile.TemporaryDirectory() as td:
        tar_path = pathlib.Path(td) / "shared.tar"
        env = dict(os.environ); env["GH_BUNDLE_PASS"] = pw
        r = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000",
                            "-in", str(BUNDLES / "shared.enc"), "-out", str(tar_path),
                            "-pass", "env:GH_BUNDLE_PASS"],
                           env=env, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            print(f"VERIFY FAIL: decrypt exit {r.returncode}: {r.stderr[-300:]}"); sys.exit(1)
        with tarfile.open(tar_path) as tf:
            names = tf.getnames()
        print(f"VERIFY OK: shared.enc decrypts, tar members: {names}")
    print("plaintext temp dir deleted")

if __name__ == "__main__":
    main()
