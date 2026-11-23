---
name: blender-colab-cli
description: Offload Blender renders and general GPU-heavy jobs to Google Colab via google-colab-cli with multi-account Compute Unit switching and automatic VM teardown. Use when asked to render .blend files on Colab GPUs, run heavy GPU scripts remotely, check Colab Compute Unit balances, switch Colab accounts, or stop idle Colab VMs.
---

# Skill: Colab GPU & Blender CLI Operator

Operate headless Google Colab GPU jobs (Blender rendering or any Python/ML/video GPU workload) using `colab_accounts.py`, `colab_render.py`, and Google's `colab` CLI.

## Quick Checks
```bash
# Check active Google account, live Compute Unit balance, and running VMs (JSON)
python colab_accounts.py active --json

# List all saved Google accounts and their live CU balances (JSON)
python colab_accounts.py list --json

# Switch active account (updates OS keyring + isolates sessions.json)
python colab_accounts.py switch <email-or-substring>

# Stop all active VMs on the active account
python colab_accounts.py stop-vms
```

## Running Blender Renders
1. Dry-run first to verify `prepare_shot.py` resolves every texture, UDIM, cache, and linked `.blend`:
   ```bash
   python colab_render.py shot <scene.blend> --out <dir> --f0 <start> --f1 <end> --samples <N> --dry-run
   ```
2. Render on a Colab GPU (L4 default; supports `T4`, `L4`, `G4`, `A100`, `H100`, `CPU`):
   ```bash
   python colab_render.py shot <scene.blend> --out <dir> --f0 <start> --f1 <end> --samples <N> --gpu L4
   ```
   - **Detaches by default:** validates paths, spawns a background worker, auto-opens the **Live Monitor** GUI (disable with `--no-gui` or `COLAB_NO_GUI=1`), and exits `0` immediately.
   - **Do NOT poll or monitor the render** after launching — report the launched job + output folder and hand off to the user.
   - Pass `--wait` **only** when you genuinely need the rendered frames in the same turn (e.g., a 1-frame test check or an immediate post-render assembly step).
   - Streams each completed frame straight to `<dir>`, skips existing frames on resume, and auto-stops the Colab VM on completion or error unless `--keep` is passed.
3. For batch queues (`jobs.json`), use:
   ```bash
   python colab_render.py queue jobs.json --gpu L4
   ```

## Running General GPU Workloads (Non-Blender)
- Open the Live Monitor GUI for the user before provisioning a GPU (single-instance, detached, non-blocking):
  ```bash
  python colab_accounts.py monitor
  ```
- Ephemeral one-shot script (provisions GPU, runs script, auto-stops VM):
  ```bash
  colab run --gpu L4 script.py [args...]
  ```
- Persistent session with file transfer (always pass `--timeout 0` for long steps and `colab stop` when done):
  ```bash
  colab new -s work --gpu L4
  colab upload -s work ./input.dat /content/input.dat
  colab exec -s work --timeout 0 -f task.py
  colab download -s work /content/out.dat ./out.dat
  colab stop -s work
  ```

## Safety Rules
- Never leave a GPU VM running idle (`colab sessions` should be empty when finished).
- Never complete Google OAuth/consent or billing screens yourself — tell the user to open `Colab_Accounts.bat` / `python colab_accounts.py`.
- Ask the user before switching Google accounts unless they already authorized auto-switching on CU depletion.
