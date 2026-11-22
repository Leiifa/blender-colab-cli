# blender-colab-cli

Use **Google Colab GPUs (T4, L4, A100, H100)** directly from your terminal or AI coding agent — **no browser tab, no Google Drive mount, and no Drive sync required** — complete with:

1. **Colab CLI Studio GUI (`colab_accounts.py` / `Colab_Accounts.bat`)** — zero-CMD desktop app with 3 tabs:
   - **Accounts & CUs:** manage multiple Google accounts in your OS keyring, view live Compute Unit balances/burn rates, switch accounts in 1 click, and stop active VMs.
   - **Live Monitor:** auto-refreshing GPU VM status, real-time render progress bar + ETA, latest rendered PNG frame thumbnail preview, and live scrolling render log — **automatically monitors renders started by AI agents or terminals too** (via `~/.config/colab-cli/active_render.json`).
   - **Launch Render:** pick a `.blend` or `jobs.json`, GPU, frame range, and parallel workers to dry-run or render without ever opening CMD.
2. **Headless Blender Cloud Renderer (`colab_render.py`, `prepare_shot.py`, `cloud_render.py`)** — bundles local scenes/assets, renders with parallel GPU workers, streams finished frames directly to disk, and auto-stops the VM.
3. **AI-Agent Friendly (`AGENTS.md`, `CLAUDE.md`, `SKILL.md`, `--json` output)** — ready for Claude Code, Codex, Gemini, Cursor, and other coding agents to safely offload heavy GPU jobs without leaving idle VMs running.

Built on top of Google's official [`google-colab-cli`](https://github.com/googlecolab/google-colab-cli) (v0.8+, works natively on Windows, macOS, and Linux).

---

## Features

### For any GPU-heavy `colab` CLI workload
- **Multi-account GUI & CLI (`colab_accounts.py` / `Colab_Accounts.bat`):**
  - Add multiple Google accounts via OAuth2 (includes a **Copy URL** button for Incognito / separate browser profiles).
  - Stores OAuth tokens securely in your OS keyring (Windows Credential Manager / macOS Keychain / Linux SecretService) — never plaintext on disk.
  - Displays live **Compute Unit (CU) balances**, **hourly burn rate**, and **active VM counts** across all saved accounts.
  - 1-click account switching (or `python colab_accounts.py switch <email>`) with per-account `sessions.json` isolation and a safety prompt if the previous account still has running VMs.
  - One-click **Stop Active VMs** button (`python colab_accounts.py stop-vms`) to stop idle billing immediately.
  - Machine-readable `--json` output (`python colab_accounts.py list --json`, `active --json`) for scripts and AI agents.
  - Once switched, standard `colab` commands (`colab run --gpu A100 train.py`, `colab new --gpu L4`, `colab exec`, `colab ssh`) automatically use the newly active account.

### For Blender cloud rendering
- **Zero-browser rendering:** Provisions a Colab GPU VM, installs Linux Blender (auto-matched to your local version), uploads a portable shot bundle, renders with Cycles (OptiX → CUDA fallback + OIDN denoising) or EEVEE, streams finished frames straight to your local disk, and **automatically stops the VM** when done.
- **Resumable by design:** Every finished frame downloads immediately and is verified on local disk. If a Colab runtime disconnects or runs out of Compute Units mid-sequence, switch accounts and re-run the exact same command — existing frames are skipped automatically.
- **Portable asset bundling (`prepare_shot.py`):** Automatically packs images/fonts/sounds into a temporary `.blend`, collects external image sequences, UDIM tiles, movies, VDB volumes, caches, and linked `.blend` libraries, and rewrites paths to be Linux-relative before uploading. Bundles >16 MiB are uploaded in chunks automatically.
- **Parallel GPU workers:** Runs multiple Blender worker processes on one GPU (`--par 3` default) coordinated via lock files to maximize GPU utilization.

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

### 3. Use Colab GPUs for any workload OR Blender

**Any Python / ML / GPU script (via `colab` CLI):**
```bash
# Check active account & Compute Units
python colab_accounts.py active

# Run any Python script on an L4 or A100 GPU (auto-creates & tears down VM)
colab run --gpu L4 train.py

# Switch accounts when Compute Units run low
python colab_accounts.py list
python colab_accounts.py switch second-account@gmail.com
```

**Blender rendering (`colab_render.py`):**
```bash
# Dry-run first (checks assets & missing frames, no VM allocated)
python colab_render.py shot path/to/scene.blend --out renders/my_shot --f0 1 --f1 120 --samples 64 --dry-run

# Single test frame on an L4 GPU (auto-stops VM when done)
python colab_render.py shot path/to/scene.blend --out renders/test --f0 1 --f1 1 --samples 32 --gpu L4

# Full animation sequence with 3 parallel Blender workers
python colab_render.py shot path/to/scene.blend --out renders/my_shot --prefix f_ --f0 1 --f1 120 --samples 64 --gpu L4 --par 3

# Keep VM alive across multiple renders, then stop manually
python colab_render.py shot shot1.blend --out renders/shot1 --f0 1 --f1 24 --gpu L4 --keep
python colab_render.py shot shot2.blend --out renders/shot2 --f0 1 --f1 24 --gpu L4 --keep
python colab_render.py stop

# Batch queue (copy jobs.example.json -> jobs.json)
python colab_render.py queue jobs.json --gpu L4
python colab_render.py queue jobs.json --only "hero" --gpu A100
```

---

## Using With AI Coding Agents

This repo ships agent-ready instructions out of the box:
- [`AGENTS.md`](AGENTS.md) — universal agent runbook (Codex, Antigravity, Cursor, OpenCode, etc.)
- [`CLAUDE.md`](CLAUDE.md) — Claude Code entry point
- [`SKILL.md`](SKILL.md) — Agent Skill file you can drop into your agent's `skills/` directory

Agents can query balances cleanly with JSON:
```bash
python colab_accounts.py list --json
python colab_accounts.py active --json
```
Safety guardrails built into the runbooks ensure agents always `--dry-run` Blender shots first, never click OAuth/billing screens, and always shut down VMs when finished.

---

## CLI Reference

| Command | Description |
| --- | --- |
| `python colab_accounts.py` / `Colab_Accounts.bat` | Open the Account Switcher GUI |
| `python colab_accounts.py list [--json]` | List saved accounts with live CU balances and active VMs |
| `python colab_accounts.py active [--json]` | Show active account email, CU balance, burn rate, and active VMs |
| `python colab_accounts.py switch <email>` | Switch active account (substring match supported) |
| `python colab_accounts.py stop-vms [email]` | Unassign/stop all running VMs on the active (or specified) account |
| `python colab_render.py status` | Show local Blender/CLI paths, active account, sessions, and CU balance |
| `python colab_render.py shot <file.blend> [opts]` | Prepare, upload, render, and download a single Blender shot |
| `python colab_render.py queue <jobs.json> [opts]` | Run a batch queue of Blender shots |
| `python colab_render.py stop [-s NAME]` | Stop a running Colab VM |

### Useful `shot` / `queue` flags
- `--gpu T4|L4|G4|A100|H100|CPU` — accelerator type (default: `L4`)
- `--high-mem` — request High-RAM shape (for GPUs that support it)
- `--par N` — parallel Blender processes per GPU (default: `3`)
- `--blender PATH` — path to local Blender binary (or set `BLENDER_EXE`)
- `--blender-version X.Y.Z` — Linux Blender version to install on Colab (default: auto-detected from local Blender, fallback `5.2.2`)
- `--keep` — keep VM running after completion (stop later with `colab_render.py stop`)
- `--skip-prepare` — upload `.blend` as-is without running `prepare_shot.py`
- `--dry-run` — check assets & missing frames without allocating a VM
- `--extra KEY=VAL` (repeatable) — passed to `cloud_render.py`:
  - `engine=cycles|eevee` — override the `.blend`'s render engine
  - `fmt=png|exr|v7` — `png` (default), `exr` (multilayer 32-bit ZIP), or `v7` (16-bit PNG + `_mist.png` + optional `keepexr`)
  - `camera=NAME` — render through a specific camera
  - `cams` — export per-frame camera JSON (`loc`, `lens`, `sensor`, `shift`, `res`)
  - `pct=50` — resolution percentage override
  - `hide=ObjA,ObjB` — hide specific objects (or `Prefix*`) from render
  - `reverse` — render from `f1` down to `f0`

## License

MIT
