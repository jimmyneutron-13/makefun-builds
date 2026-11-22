# Split pipeline — Actions coding-only, credentials stay on the VM

User order 22 Nov 2026 11:54 WIB: make.fun account credentials/logins and
bundling/submission stay on the VM; GitHub Actions only generates code.
This lane runs alongside the existing all-in-Actions lane
(`gh-build-one.py` + `build.yml`), which is unchanged.

## Why this is compatible with make.fun's transcript rules

- `makefun init` needs the account keypair (drand root commit carries the
  account wallet in `.makefun/freshness.json`), so init runs on the VM.
  The anchor mechanism is unchanged: driver init → anchor in brief → the
  Codex session quotes `makefun anchor <sha> round <n>` before its first
  counted write, after the drand round was emitted.
- The kit is a public download (`https://make.fun/c/<slug>/kit`), so the
  runner needs no auth to read RULES.txt / competition.yaml.
- `makefun bundle` and `makefun check` are local-only (no credentials).
  Bundle discovers Codex sessions from `$CODEX_HOME` (default `~/.codex`),
  so runner session files can be staged into a temp CODEX_HOME on the VM.
- Only `makefun submit` needs credentials + keypair → VM only.

## Flow

1. **VM — prepare** (`split-prepare.py <N> [--slug ...]`):
   ledger lookup → fresh kit → `makefun init` (local creds+keypair, anchor,
   wallet verified) → brief from the same template as the VM spillover lane
   (`brief-<slug>-template.txt`, paths rewritten to the fixed runner layout
   `/home/runner/work/_temp/split-acct-<N>/`, credentials line removed) →
   tar (repo, brief.txt, anchor.txt, meta.json, news file,
   shoot-news105.mjs, xingchi.config.toml) → openssl aes-256-cbc encrypt
   (passphrase `~/.gh-bundle-pass`, env only) →
   `payloads/<slug>/acct-<N>.enc` → commit + push to `rasjdahw` remote.
   Local state (repo, kit, brief, meta) stays in `split-work/<slug>/acct-<N>/`
   (gitignored) for the finish step.
2. **GitHub Actions — build** (`.github/workflows/build-split.yml`,
   workflow_dispatch: slug + comma-separated account numbers):
   per account — checkout, decrypt payload, install Node 22 + Codex 0.170.0 +
   Playwright Chromium, run `split-build-one.py <N>`:
   kit download → Codex build (14-slot Xingchi pool, slot number only in log)
   → render entry.png → resume Codex (structural verify + commit via /tmp git
   bundle, same as the old lane) → package `entry.bundle` + Codex session
   files + `result-<N>.json` → encrypt → artifact `split-result-<N>`.
   Any failure still uploads an encrypted result with the error.
3. **VM — finish** (`split-finish.py <N>... [--slug ...] [--dry-run]`):
   newest `split-result-<N>` artifact via GitHub API (token
   `~/.gh-rasjdahw-token`) → decrypt → fetch commit into the local anchored
   repo (sha must match the runner's) → stage sessions into
   `split-work/.../codex-home/` → `makefun bundle` (CODEX_HOME staged,
   parent-session + system-prompt + muse-usage from VM files, same flags as
   finalize-spillover) → `makefun check` → `makefun submit` (local
   creds+keypair) → server verify → live record appended to the account's
   origin ledger + `finished.json` state. Idempotent: live/finished accounts
   are skipped. `--dry-run` stops before bundle/check/submit.

## Usage (once Xingchi is back)

```bash
cd ~/workspace/gh-builds
python3 split-prepare.py 918                      # per account (VM)
# dispatch build-split.yml in rasjdahw/makefun-builds: slug + accounts list
python3 split-finish.py 918                       # after the run completes
```

## Remaining risks (only provable with a 1-account pilot after Xingchi recovers)

1. **Session discovery**: does `makefun bundle` on the VM find the runner's
   Codex sessions via the staged `CODEX_HOME`? Expected yes (discovery is
   `$CODEX_HOME`-based), but unproven end-to-end.
2. **Line attribution across machines**: the build session's cwd is the
   runner path (`/home/runner/work/_temp/split-acct-<N>/repo`). If the
   authorship check binds counted lines to session cwd paths, bundling on
   the VM (repo at a different path) could lose attribution. The old lane
   bundles on the same machine that built, so this is the main unknown.
3. **Anchor timing**: init on the VM, build minutes-to-hours later on the
   runner. The quote still lands after the drand round, but any unpublished
   freshness window between root commit and first write would only show up
   in the pilot.
4. **Brief path fidelity**: `--prompt-file` on the VM is byte-identical to
   what Codex saw (split-prepare stores the rewritten brief), so no drift.

Pilot plan: one spillover account (918 prepared 22 Nov 2026), dispatch once
Xingchi recovers, finish on VM, compare bundle session count + check result
against the VM spillover lane before any mass use. Fallback if the chain
breaks: the existing all-in-Actions lane (credentials inside encrypted
bundles, passphrase only in Actions secrets).
