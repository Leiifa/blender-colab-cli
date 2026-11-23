# Agent Runbook — `blender-colab-cli`

Use this toolkit to offload heavy GPU jobs (Blender rendering, PyTorch, FFmpeg/NVENC, Whisper, diffusion, etc.) to Google Colab directly from the terminal via `google-colab-cli` — with zero browser tabs and safe multi-account management.

## Hard Safety Rules
- **Never leave a GPU VM running idle.** Always let `colab_render.py` / `colab run` auto-stop the VM (omit `--keep`), or explicitly run `python colab_render.py stop` / `python colab_accounts.py stop-vms` when done.
- **Never complete OAuth sign-in or consent screens on the user's behalf.** If `python colab_accounts.py active --json` reports not signed in or an expired token, stop and ask the user to sign in using `Colab_Accounts.bat` or `python colab_accounts.py`.
- **Never buy Compute Units or modify billing plans.**
- **Always confirm with the user before switching accounts** if you are changing which Google account is billed, unless the user explicitly instructed you to auto-switch when CUs run out.

## Checking Accounts & Compute Units (Machine-Readable)
```bash
# Active account email, live CU balance, hourly rate, and active VMs
python colab_accounts.py active --json

# All saved accounts and their live balances
python colab_accounts.py list --json

# Switch active account (updates OS keyring & isolates sessions.json)
python colab_accounts.py switch <email-or-substring>

# Emergency stop all running VMs on the active account
python colab_accounts.py stop-vms
```

## Workflow A: General GPU Jobs (Non-Blender)
1. Check `python colab_accounts.py active --json` to confirm Compute Units > 0.
2. Open the Live Monitor GUI for the user (detached, single-instance, returns immediately):
   ```bash
   python colab_accounts.py monitor
   ```
3. Prefer ephemeral execution so VMs self-terminate even on error:
   ```bash
   colab run --gpu L4 my_script.py [args...]
   ```
3. For multi-step jobs requiring uploads/downloads:
   ```bash
   colab new -s job1 --gpu L4
   colab upload -s job1 ./input.bin /content/input.bin
   colab exec -s job1 --timeout 0 -f run_gpu_task.py
   colab download -s job1 /content/output.bin ./output.bin
   colab stop -s job1
   ```
4. Always pass `--timeout 0` on `colab exec` for long-running GPU steps (default timeout is 30 s without output).
5. Confirm `colab sessions` shows 0 remaining active VMs.

## Workflow B: Blender Rendering
1. **Check status:** `python colab_render.py status`
2. **Always dry-run first** on any new `.blend` so `prepare_shot.py` verifies all textures, linked libraries, and caches before allocating a VM:
   ```bash
   python colab_render.py shot path/to/scene.blend --out renders/shot --f0 1 --f1 10 --samples 48 --dry-run
   ```
   Fix any `UNRESOLVED` asset lines before rendering.
3. **Launch render (detached fire-and-forget by default):**
   ```bash
   python colab_render.py shot path/to/scene.blend --out renders/shot --f0 1 --f1 120 --samples 48 --gpu L4
   ```
   - `shot` and `queue` **detach immediately** and exit `0` after validating inputs, spawning the background worker, and opening the **Live Monitor** GUI (disable GUI with `--no-gui` or `COLAB_NO_GUI=1`).
   - Frames stream directly into `--out` as each finishes, existing frames are skipped on resume, and the VM **auto-stops on completion, error, or 15-minute idle hang** (unless `--keep` is passed).
   - **Do NOT monitor, poll, `sleep`, or keep your turn running waiting for the render.** Report the launched shot, GPU, and output folder to the user and end your turn — the user tracks progress in the Live Monitor GUI.
4. **Only pass `--wait` when this turn actually needs the finished frames** (for example, a 1-frame lookdev test you must inspect immediately, or a follow-up assembly step the user explicitly asked you to run right after):
   ```bash
   python colab_render.py shot path/to/scene.blend --out renders/test --f0 1 --f1 1 --samples 48 --gpu L4 --wait
   ```
