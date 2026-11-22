# blender-colab-cli

Render Blender scenes on **Google Colab GPUs (T4, L4, A100, H100)** straight from your terminal — **no browser tab, no Google Drive mount, and no Drive sync required** — plus a lightweight **desktop GUI** to manage multiple Google accounts and switch in 1 click when Compute Units run out.

Built on top of Google's official [`google-colab-cli`](https://github.com/googlecolab/google-colab-cli) (v0.8+, works natively on Windows, macOS, and Linux).

---

## Features

- **Zero-browser rendering:** Provisions a Colab GPU VM, installs Linux Blender (matching your version), uploads a portable shot bundle, renders with Cycles (OptiX → CUDA fallback + OIDN denoising) or EEVEE, streams finished frames straight to your local disk, and **automatically stops the VM** when done.
- **Resumable by design:** Every finished frame downloads immediately and is verified on local disk. If a Colab runtime disconnects or runs out of Compute Units mid-sequence, switch accounts and re-run the exact same command — existing frames are skipped automatically.
- **Portable asset bundling (`prepare_shot.py`):** Automatically packs images/fonts/sounds into a temporary `.blend`, collects external image sequences, UDIM tiles, movies, VDB volumes, caches, and linked `.blend` libraries, and rewrites paths to be Linux-relative before uploading. Bundles >16 MiB are uploaded in chunks automatically.
- **Parallel GPU workers:** Runs multiple Blender worker processes on one GPU (`--par 3` default) coordinated via lock files to maximize GPU utilization.
- **Account Switcher GUI (`colab_accounts.py` / `Colab_Accounts.bat`):**
  - Add multiple Google accounts via OAuth2 (includes a **Copy URL** button for Incognito / alternate browser profiles).
  - Stores OAuth tokens in your OS keyring (Windows Credential Manager / macOS Keychain / Linux SecretService) — never plaintext on disk.
  - Displays live **Compute Unit balances**, **hourly burn rate**, and **active VM counts** for every saved account.
  - 1-click account switching with per-account `sessions.json` isolation and a safety prompt if the previous account still has running VMs.
  - One-click **Stop Active VMs** button to prevent accidental idle billing.

---

## Quick Start

### 1. Install `google-colab-cli`
Using [`uv`](https://docs.astral.sh/uv/) (recommended):
```bash
uv tool install google-colab-cli
```
*(Or `pip install google-colab-cli`.)*

### 2. Sign in with your Google account(s)
- **Windows:** Double-click **`Colab_Accounts.bat`**
- **Any OS (terminal):**
  ```bash
  python colab_accounts.py
  ```
Click **`+ Add Account`**, open (or copy) the sign-in link, approve access, and paste the authorization code. You can add multiple Google accounts and switch between them anytime with a double-click.

*(Terminal-only alternative: `python colab_render.py login`)*

### 3. Dry-run a shot (checks assets & missing frames, no VM allocated)
```bash
python colab_render.py shot path/to/scene.blend --out renders/my_shot --f0 1 --f1 120 --samples 64 --dry-run
```

### 4. Render on a Colab GPU
```bash
# Single test frame on an L4 GPU (auto-stops VM when done)
python colab_render.py shot path/to/scene.blend --out renders/test --f0 1 --f1 1 --samples 32 --gpu L4

# Full animation sequence with 3 parallel Blender workers
python colab_render.py shot path/to/scene.blend --out renders/my_shot --prefix f_ --f0 1 --f1 120 --samples 64 --gpu L4 --par 3

# Keep the VM alive across multiple renders, then stop manually when finished
python colab_render.py shot shot1.blend --out renders/shot1 --f0 1 --f1 24 --gpu L4 --keep
python colab_render.py shot shot2.blend --out renders/shot2 --f0 1 --f1 24 --gpu L4 --keep
python colab_render.py stop
```

### 5. Batch queue (`jobs.json`)
Copy `jobs.example.json` to `jobs.json`, edit your shots, and run:
```bash
python colab_render.py queue jobs.json --gpu L4
python colab_render.py queue jobs.json --only "hero" --gpu A100
```

---

## Switching Accounts When Compute Units Run Out

1. Frames finished before disconnect are already saved in your local `--out` folder.
2. Open the Account Switcher GUI (`Colab_Accounts.bat` or `python colab_render.py gui`) and double-click another account with available Compute Units (or from CLI: `python colab_render.py accounts --switch other@gmail.com`).
3. Re-run the exact same render command — it automatically resumes from the first missing frame.

---

## CLI Reference

| Command | Description |
| --- | --- |
| `python colab_render.py gui` | Open the Account Switcher GUI |
| `python colab_render.py accounts` | List saved accounts with live CU balances and active VMs |
| `python colab_render.py accounts --switch <email>` | Switch active account (substring match supported) |
| `python colab_render.py status` | Show local Blender/CLI paths, active account, sessions, and CU balance |
| `python colab_render.py shot <file.blend> [opts]` | Prepare, upload, render, and download a single shot |
| `python colab_render.py queue <jobs.json> [opts]` | Run a batch queue of shots |
| `python colab_render.py stop [-s NAME]` | Stop a running Colab VM |

### Useful `shot` / `queue` flags
- `--gpu T4|L4|G4|A100|H100|CPU` — accelerator type (default: `L4`)
- `--high-mem` — request High-RAM shape (for GPUs that support it)
- `--par N` — parallel Blender processes per GPU (default: `3`)
- `--blender PATH` — path to local Blender binary (or set `BLENDER_EXE`)
- `--blender-version X.Y.Z` — Linux Blender version to install on Colab (default: auto-detected from local Blender, fallback `5.2.2`)
- `--keep` — keep VM running after completion (don't forget `colab_render.py stop` later!)
- `--skip-prepare` — upload `.blend` as-is without running `prepare_shot.py`
- `--dry-run` — check assets & missing frames without allocating a VM
- `--extra KEY=VAL` (repeatable) — passed to `cloud_render.py`:
  - `engine=cycles|eevee` — override the `.blend`'s render engine
  - `fmt=png|exr|v7` — `png` (default), `exr` (multilayer 32-bit ZIP), or `v7` (16-bit PNG + `_mist.png` + optional `keepexr`)
  - `camera=NAME` — render through a specific camera
  - `cams` — export per-frame camera JSON (`loc`, `lens`, `sensor`, `shift`, `res`)
  - `pct=50` — resolution percentage override
  - `hide=ObjA,ObjB` — hide specific objects from render
  - `reverse` — render from `f1` down to `f0`

---

## AI Agent Integration

See [`AGENTS.md`](AGENTS.md) for a concise runbook that lets coding agents (Claude Code, Codex, Gemini, etc.) safely inspect balances, dry-run scenes, render frames, and guarantee VMs are always shut down.

## License

MIT
