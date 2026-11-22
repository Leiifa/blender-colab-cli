# Agent Runbook — `blender-colab-cli`

Use this toolkit to render Blender scenes on Google Colab GPUs directly from the terminal without opening a browser.

## Fixed Rules
- **Never leave a GPU VM running idle.** Always let `colab_render.py` auto-stop the VM (omit `--keep`), or explicitly run `python colab_render.py stop` when done.
- **Never run OAuth login or click Google consent screens on the user's behalf.** If `python colab_render.py status` shows not signed in or expired auth, stop and ask the user to sign in via `Colab_Accounts.bat` / `python colab_render.py gui`.
- **Never purchase Compute Units or change billing settings.**
- **Always `--dry-run` first** on a new `.blend` to verify `prepare_shot.py` resolves every texture, linked library, and external cache before allocating a billable VM.

## Standard Workflow
1. **Check active account & Compute Units:**
   ```bash
   python colab_render.py status
   ```
   If Compute Units are 0, list saved accounts (`python colab_render.py accounts`) and ask the user before switching (`python colab_render.py accounts --switch <email>`).
2. **Dry-run the shot:**
   ```bash
   python colab_render.py shot path/to/scene.blend --out renders/shot --f0 1 --f1 10 --samples 48 --dry-run
   ```
   Fix any `UNRESOLVED` asset paths before continuing.
3. **Render (single test frame first recommended for heavy shots):**
   ```bash
   python colab_render.py shot path/to/scene.blend --out renders/shot --f0 1 --f1 1 --samples 48 --gpu L4
   ```
4. **Verify outputs & confirm VM is stopped:**
   - Check that output frame count matches `f1 - f0 + 1` with no zero-byte files.
   - Run `colab sessions` to confirm no running VMs remain.
