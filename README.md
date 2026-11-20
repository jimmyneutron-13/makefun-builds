# Jalur build GitHub Actions — make.fun news-image-20261120

Pengganti jalur PC (PC mati lama). Setiap job GitHub Actions membangun + submit
1 entry untuk 1 akun, memakai pipeline yang sama dengan probe PC
(kit segar → init → Codex build → render → resume/commit → bundle → check → submit).

## Isi folder

- `gh-build-one.py` — driver per akun di runner (port `probe-pc-build-one.py`).
  Output: `result-<n>.json` + 1 baris JSON di stdout.
- `.github/workflows/build.yml` — workflow manual (`workflow_dispatch`),
  matrix dari `accounts.json`, maks 20 job paralel.
- `make-bundles.py` — membuat `accounts.json` + bundle terenkripsi di `bundles/`.
- `bundles/acct-<n>.enc` — keypair + credentials per akun (openssl aes-256-cbc).
- `bundles/shared.enc` — parent-session, system-prompt, snapshot usage, brief,
  daftar berita, script render, profil Codex.
- `accounts.json` — daftar akun jalur GitHub (n, handle, wallet), tanpa secret.
- `sync-results.py` — dijalankan di VM: menarik artifact `result-*.json` dari
  GitHub dan menulis baris `live` ke ledger friend/chain1000.

## Secret yang dibutuhkan (hanya 1)

`XINGCHI_API_KEY` — nilai key Xingchi muse-default (yang sama dengan di
`~/workspace/xingchiapi/credentials.json`). Secret ini dipakai ganda:
mendekripsi bundle dan memberi makan Codex. **User yang mengisi secret ini
sendiri di GitHub** (Settings → Secrets and variables → Actions). Jangan
pernah menempel nilainya di chat/file plaintext.

## Urutan langkah push (belum dijalankan)

1. Regenerate bundle segar tepat sebelum push: `python3 make-bundles.py --verify-one`
   (daftar akun berubah tiap ada akun yang live dari jalur lain).
2. Buat repo **private** di GitHub, lalu dari folder ini:
   `git init && git add -A && git commit -m "gh build lane" && git remote add origin git@github.com:<owner>/<repo>.git && git push -u origin main`
   (SSH key VM: `~/.ssh/id_ed25519.pub` harus ditambahkan ke GitHub dulu.)
3. Isi secret `XINGCHI_API_KEY` di repo.
4. Jalankan workflow dari tab Actions: input `accounts` = `all` atau daftar
   nomor akun (maks 256 job per run — dispatch ulang untuk sisanya).
5. Setelah run selesai, di VM: simpan token GitHub (fine-grained, akses
   Actions read) ke `~/.github-token` (mode 600), lalu
   `python3 sync-results.py --repo <owner>/<repo>`.

## Batasan

- Bounty target `news-image-20261120` tutup 22 Nov 2026 07:00 WIB; job yang
  mulai setelah itu akan gagal di submit (result tetap ter-upload).
- Plan Free: 20 job bersamaan, repo private 2.000 menit/bulan gratis lalu
  berbayar per menit. Satu akun ≈ 4–6 menit runner.
- IP runner adalah datacenter Azure — risiko keterkaitan akun lebih tinggi
  daripada PC rumahan. Ini jalur darurat selama PC mati.
- `parent-session.jsonl` dan snapshot usage di `shared.enc` adalah salinan
  saat bundle dibuat; regenerate bundle kalau sesi induk/usage berubah
  material sebelum push.
- Jangan pernah commit file plaintext keypair/credentials; hanya `*.enc`.
