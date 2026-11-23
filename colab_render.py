"""Render Blender shots on Google Colab via the official `colab` CLI (no browser / no Drive needed).

Prepares portable shot bundles locally via `prepare_shot.py`, provisions a Colab GPU session,
installs Linux Blender on the VM (cached per session), renders via `cloud_render.py`,
streams finished frames directly to local disk as each frame finishes, and stops the VM when done.

Examples:
  # 1. Open the Account Switcher GUI to add/manage Google accounts
  python colab_render.py gui

  # 2. Check active account, sessions, and Compute Unit balance
  python colab_render.py status

  # 3. Dry-run a shot (checks assets & missing frames, no VM allocated)
  python colab_render.py shot path/to/scene.blend --out renders/shot1 --f0 1 --f1 120 --samples 64 --dry-run

  # 4. Render a single test frame on an L4 GPU
  python colab_render.py shot path/to/scene.blend --out renders/test --f0 1 --f1 1 --samples 32 --gpu L4

  # 5. Render a jobs.json queue
  python colab_render.py queue jobs.json --gpu L4

  # 6. Keep VM alive across multiple shots, then stop manually
  python colab_render.py shot shot1.blend --out renders/shot1 --f0 1 --f1 24 --keep
  python colab_render.py stop
"""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
PREPARE_SCRIPT = SCRIPT_DIR / "prepare_shot.py"
CLOUD_RENDER_SCRIPT = SCRIPT_DIR / "cloud_render.py"
ACCOUNTS_SCRIPT = SCRIPT_DIR / "colab_accounts.py"

DEFAULT_BLENDER_VERSION = "5.2.2"
DEFAULT_SESSION = "blender-render"
CHUNK_BYTES = 16 * 1024 * 1024  # 16 MiB chunks keep base64 JSON PUTs well below server limits

FRAME_LINE_RE = re.compile(r"(?:\[\d+\]\s+)?FRAME\s+(\S+)\s+([\d.]+)s(?:\s+\|\s+(.+))?")
BLENDER_VER_RE = re.compile(r"Blender\s+(\d+\.\d+\.\d+)")
CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

MONITOR_DIR = Path.home() / ".config" / "colab-cli"
JOBS_DIR = MONITOR_DIR / "jobs"
INBOX_DIR = MONITOR_DIR / "inbox"
GUI_HEARTBEAT_PATH = MONITOR_DIR / "gui_heartbeat.json"


class _RenderTelemetry:
    """Writes per-run live render progress & logs to ~/.config/colab-cli/jobs/ so the GUI monitors multiple concurrent CLI/agent/GUI runs."""

    def __init__(self, *, enabled: bool = True, run_id: Optional[str] = None) -> None:
        self.enabled = enabled
        self.run_id = run_id or f"{int(time.time())}_{os.getpid()}"
        self.json_path = JOBS_DIR / f"{self.run_id}.json"
        self.log_path = JOBS_DIR / f"{self.run_id}.log"
        self.state: dict = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "status": "starting",
            "job_name": "",
            "job_index": 0,
            "job_count": 0,
            "blend": "",
            "out_dir": "",
            "gpu": "",
            "session": "",
            "par": 1,
            "total_frames": 0,
            "done_frames": 0,
            "rendered_this_run": 0,
            "last_frame": "",
            "last_frame_sec": 0.0,
            "avg_frame_sec": 0.0,
            "last_frame_path": "",
            "started_at": time.time(),
            "updated_at": time.time(),
            "error": "",
        }
        self._frame_times: list[float] = []
        if self.enabled:
            JOBS_DIR.mkdir(parents=True, exist_ok=True)
            self._prune_old()
            try:
                if not self.log_path.exists():
                    self.log_path.write_text("", encoding="utf-8")
            except OSError:
                pass
            self._flush_json()

    def _prune_old(self, keep: int = 20) -> None:
        try:
            files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[keep:]:
                for ext in (".json", ".log"):
                    p = old.with_suffix(ext)
                    if p.exists():
                        p.unlink(missing_ok=True)
        except OSError:
            pass

    def _flush_json(self) -> None:
        if not self.enabled:
            return
        self.state["updated_at"] = time.time()
        try:
            tmp = self.json_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
            os.replace(tmp, self.json_path)
        except OSError:
            pass

    def update(self, **kwargs) -> None:
        self.state.update(kwargs)
        self._flush_json()

    def log(self, line: str) -> None:
        if sys.stdout is not None:
            try:
                print(line, flush=True)
            except OSError:
                pass
        if not self.enabled:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def record_frame(self, frame_label: str, sec: float, done_frames: int, frame_path: str = "") -> None:
        self._frame_times.append(sec)
        if len(self._frame_times) > 20:
            self._frame_times = self._frame_times[-20:]
        self.state["done_frames"] = done_frames
        self.state["rendered_this_run"] = self.state.get("rendered_this_run", 0) + 1
        self.state["last_frame"] = frame_label
        self.state["last_frame_sec"] = sec
        self.state["avg_frame_sec"] = sum(self._frame_times) / len(self._frame_times)
        if frame_path:
            self.state["last_frame_path"] = frame_path
        self._flush_json()


def _find_colab() -> str:
    exe = shutil.which("colab")
    if exe:
        return exe
    candidate = Path.home() / ".local" / "bin" / ("colab.exe" if os.name == "nt" else "colab")
    if candidate.is_file():
        return str(candidate)
    raise SystemExit(
        "ERROR: `colab` CLI not found. Install it with: uv tool install google-colab-cli"
    )


def _find_blender(explicit: Optional[str] = None) -> str:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        raise SystemExit(f"ERROR: Specified Blender binary not found: {explicit}")
    env_exe = os.environ.get("BLENDER_EXE")
    if env_exe and Path(env_exe).is_file():
        return env_exe
    exe = shutil.which("blender")
    if exe:
        return exe
    common: list[str] = []
    if os.name == "nt":
        common.extend(sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender *\blender.exe"), reverse=True))
        common.extend([r"D:\Blender\blender.exe", r"C:\Blender\blender.exe"])
    elif sys.platform == "darwin":
        common.append("/Applications/Blender.app/Contents/MacOS/Blender")
    else:
        common.extend(["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"])
    for c in common:
        if Path(c).is_file():
            return c
    raise SystemExit(
        "ERROR: Blender not found on PATH or standard install locations. "
        "Pass --blender PATH or set the BLENDER_EXE environment variable."
    )


def _detect_blender_version(explicit_ver: Optional[str], blender_exe: Optional[str]) -> str:
    if explicit_ver:
        return explicit_ver
    if os.environ.get("COLAB_BLENDER_VERSION"):
        return os.environ["COLAB_BLENDER_VERSION"]
    if blender_exe:
        try:
            r = subprocess.run([blender_exe, "--version"], capture_output=True, text=True, timeout=10)
            m = BLENDER_VER_RE.search(r.stdout)
            if m:
                return m.group(1)
        except Exception:
            pass
    return DEFAULT_BLENDER_VERSION


def _blender_tar_url(version: str) -> str:
    major_minor = ".".join(version.split(".")[:2])
    return f"https://download.blender.org/release/Blender{major_minor}/blender-{version}-linux-x64.tar.xz"


def _colab_env() -> dict:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    return env


def _run_colab(args: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = [_find_colab()] + args
    return subprocess.run(
        cmd,
        env=_colab_env(),
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
        creationflags=CREATION_FLAGS,
    )


def _compress_ranges(nums: list[int]) -> str:
    if not nums:
        return ""
    s = sorted(set(nums))
    parts = []
    start = prev = s[0]
    for x in s[1:]:
        if x == prev + 1:
            prev = x
        else:
            parts.append(f"{start}-{prev}" if prev > start else str(start))
            start = prev = x
    parts.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(parts)


def _parse_fmt(extras: list[str]) -> str:
    for i, x in enumerate(extras):
        if x.startswith("fmt="):
            return x.split("=", 1)[1].lower()
        if x == "--fmt" and i + 1 < len(extras):
            return extras[i + 1].lower()
    return "png"


def _resolve_blend_path(blend_arg: str, project_root: Path) -> Path:
    candidates = [
        Path(blend_arg),
        project_root / blend_arg,
        project_root / "blender" / blend_arg,
    ]
    for p in candidates:
        if p.is_file():
            return p.resolve()
    raise SystemExit(f"ERROR: Blend file not found: {blend_arg}")


def _resolve_out_dir(out_arg: str, out_root: Path) -> Path:
    p = Path(out_arg)
    if p.is_absolute():
        return p
    return out_root / out_arg


def _missing_frames(out_dir: Path, prefix: str, f0: int, f1: int, fmt: str) -> list[int]:
    primary_ext = ".exr" if fmt == "exr" else ".png"
    missing = []
    for f in range(f0, f1 + 1):
        p = out_dir / f"{prefix}{f:04d}{primary_ext}"
        if not (p.is_file() and p.stat().st_size > 0):
            missing.append(f)
    return missing


def _stage_and_zip_shot(
    blend_path: Path,
    stage_root: Path,
    project_root: Path,
    blender_exe: Optional[str],
    *,
    skip_prepare: bool,
    dry: bool,
) -> tuple[Optional[Path], str]:
    """Prepares a portable shot bundle and zips it to match Colab's /content/bundle layout."""
    shot_stem = blend_path.stem
    shot_name = blend_path.name
    if skip_prepare:
        if dry:
            print(f"[prepare] --skip-prepare: would upload {blend_path} + cloud_render.py")
            return None, shot_name
        zip_path = stage_root / f"{shot_stem}_bundle.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(blend_path, f"blender/{shot_name}")
            zf.write(CLOUD_RENDER_SCRIPT, "cloud_render.py")
        return zip_path, shot_name

    assert blender_exe is not None
    stage_dir = stage_root / shot_stem
    stage_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        blender_exe,
        "-b",
        str(blend_path),
        "-P",
        str(PREPARE_SCRIPT),
        "--",
        shot_stem,
        "--stage",
        str(stage_dir).replace("\\", "/"),
        "--root",
        str(project_root).replace("\\", "/"),
    ]
    if dry:
        cmd.append("--dry")

    print(f"[prepare] Running prepare_shot.py on {shot_name}...")
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    unresolved = []
    for line in res.stdout.splitlines():
        if line.startswith(("SHOT ", "NOTE ", "UNRESOLVED ", "PREPARED ", "DRY RUN:", "  [lib]")):
            print("  " + line)
        if line.startswith("UNRESOLVED "):
            unresolved.append(line)
    if res.returncode != 0:
        print(res.stdout[-2000:], file=sys.stderr)
        print(res.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"ERROR: prepare_shot.py failed (rc={res.returncode})")
    if unresolved:
        raise SystemExit(
            f"ERROR: {len(unresolved)} unresolved asset(s) in {shot_name}. Fix before uploading to Colab."
        )
    if dry:
        return None, shot_name

    zip_path = stage_root / f"{shot_stem}_bundle.zip"
    blender_stage = stage_dir / "blender"
    bundle_stage = stage_dir / "bundle"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(CLOUD_RENDER_SCRIPT, "cloud_render.py")
        if blender_stage.is_dir():
            for fp in sorted(blender_stage.rglob("*")):
                if fp.is_file():
                    rel = fp.relative_to(blender_stage).as_posix()
                    zf.write(fp, f"blender/{rel}")
        if bundle_stage.is_dir():
            for fp in sorted(bundle_stage.rglob("*")):
                if fp.is_file():
                    rel = fp.relative_to(bundle_stage).as_posix()
                    zf.write(fp, rel)
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"[prepare] Bundle zip ready: {zip_path.name} ({size_mb:.2f} MB)")
    return zip_path, shot_name


def _session_exists(session: str) -> bool:
    res = _run_colab(["status", "-s", session], check=False, capture=True)
    txt = f"{res.stdout or ''}\n{res.stderr or ''}".lower()
    if res.returncode != 0:
        return False
    if "not found" in txt or "no active" in txt or "expired" in txt:
        return False
    return True


def _ensure_session(
    session: str, gpu: str, high_mem: bool, telemetry: Optional[_RenderTelemetry] = None
) -> bool:
    """Ensure Colab session exists. Returns True if we created a new VM."""
    log = telemetry.log if telemetry else (lambda s: print(s, flush=True))
    if _session_exists(session):
        log(f"[colab] Reusing existing session '{session}'")
        return False
    args = ["new", "-s", session]
    if gpu.upper() != "CPU":
        args += ["--gpu", gpu.upper()]
    if high_mem:
        args.append("--high-mem")
    res = _run_colab(args, check=False, capture=True)
    for line in (res.stdout or "").splitlines() + (res.stderr or "").splitlines():
        s = line.strip()
        if s:
            log(s)
    if res.returncode != 0:
        detail = ((res.stderr or "") + "\n" + (res.stdout or "")).strip()
        last_line = detail.splitlines()[-1] if detail else f"exit code {res.returncode}"
        raise RuntimeError(f"colab new -s {session} failed: {last_line}")
    return True


def _kill_proc_tree(pid: int) -> None:
    if not pid or pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATION_FLAGS,
            check=False,
        )
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _exec_script(
    session: str,
    code: str,
    *,
    timeout: float = 180.0,
    telemetry: Optional[_RenderTelemetry] = None,
    retries: int = 3,
    label: str = "exec",
) -> None:
    """Run a short Python snippet on Colab with a local wall-clock watchdog & retry.

    Works around an upstream google-colab-cli / jupyter_kernel_client bug where a slow
    websocket handshake leaves a non-daemon ping thread alive so `colab exec` never exits.
    """
    log = telemetry.log if telemetry else (lambda s: print(s, flush=True))
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
        tf.write(code)
        tmp_path = tf.name
    try:
        last_err = ""
        for attempt in range(1, retries + 1):
            proc = subprocess.Popen(
                [_find_colab(), "exec", "-s", session, "--timeout", str(int(timeout)), "-f", tmp_path],
                env=_colab_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATION_FLAGS,
            )
            lines: list[str] = []
            timed_out = {"flag": False}

            def _watchdog() -> None:
                timed_out["flag"] = True
                log(f"[warn] {label} hung past {int(timeout) + 20}s (colab websocket deadlock) — killing PID {proc.pid}...")
                _kill_proc_tree(proc.pid)

            timer = threading.Timer(timeout + 20.0, _watchdog)
            timer.daemon = True
            timer.start()
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    s = line.rstrip()
                    lines.append(s)
                    if s:
                        log(s)
                rc = proc.wait()
            finally:
                timer.cancel()

            if rc == 0 and not timed_out["flag"]:
                return
            tail = " | ".join(lines[-3:]) if lines else f"rc={rc}"
            last_err = f"{label} timed out" if timed_out["flag"] else f"{label} failed (rc={rc}): {tail}"
            if attempt < retries:
                log(f"[warn] {last_err} — retrying ({attempt}/{retries})...")
                time.sleep(3 * attempt)
        raise RuntimeError(last_err)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _ensure_remote_blender(session: str, version: str, telemetry: Optional[_RenderTelemetry] = None) -> None:
    log = telemetry.log if telemetry else (lambda s: print(s, flush=True))
    tar_url = _blender_tar_url(version)
    log(f"[colab] Checking GPU & Blender {version} on '{session}'...")
    setup_code = textwrap.dedent(f"""
        import os, subprocess
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                           capture_output=True, text=True)
        print("GPU:", r.stdout.strip() if r.returncode == 0 else "NONE (CPU runtime)", flush=True)
        ver_file = "/content/blender/.version"
        cur_ver = open(ver_file).read().strip() if os.path.isfile(ver_file) else None
        if not os.path.exists("/content/blender/blender") or cur_ver != {version!r}:
            print("Installing Blender {version} + graphics libraries...", flush=True)
            subprocess.run("apt-get -qq update && apt-get -qq install -y "
                           "libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 libxkbcommon0 libsm6 libegl1 > /dev/null",
                           shell=True, check=True)
            subprocess.run(["wget", "-q", {tar_url!r}, "-O", "/content/blender.tar.xz"], check=True)
            import shutil
            shutil.rmtree("/content/blender", ignore_errors=True)
            os.makedirs("/content/blender", exist_ok=True)
            subprocess.run(["tar", "-xf", "/content/blender.tar.xz", "-C", "/content/blender", "--strip-components=1"],
                           check=True)
            os.remove("/content/blender.tar.xz")
            open(ver_file, "w").write({version!r})
        v = subprocess.run(["/content/blender/blender", "--version"], capture_output=True, text=True, check=True)
        print("BLENDER:", v.stdout.splitlines()[0], flush=True)
        os.makedirs("/content/bundle", exist_ok=True)
        os.makedirs("/content/out", exist_ok=True)
    """).strip()
    _exec_script(session, setup_code, timeout=480, telemetry=telemetry, retries=3, label="blender-setup")


def _upload_bundle(session: str, zip_path: Path, telemetry: Optional[_RenderTelemetry] = None) -> None:
    log = telemetry.log if telemetry else (lambda s: print(s, flush=True))
    total = zip_path.stat().st_size
    if total <= CHUNK_BYTES:
        log(f"[colab] Uploading {zip_path.name} ({total / 1e6:.2f} MB)...")
        _run_colab(["upload", "-s", session, str(zip_path), "/content/bundle.zip"], check=True)
        log("[colab] Unpacking bundle on VM...")
        unpack_code = textwrap.dedent("""
            import os, shutil, zipfile
            shutil.rmtree("/content/bundle", ignore_errors=True)
            os.makedirs("/content/bundle", exist_ok=True)
            with zipfile.ZipFile("/content/bundle.zip", "r") as zf:
                zf.extractall("/content/bundle")
            os.remove("/content/bundle.zip")
            print("UNPACKED:", sorted(os.listdir("/content/bundle/blender")), flush=True)
        """).strip()
        _exec_script(session, unpack_code, timeout=120, telemetry=telemetry, retries=3, label="bundle-unpack")
        return

    n_chunks = (total + CHUNK_BYTES - 1) // CHUNK_BYTES
    log(f"[colab] Uploading {zip_path.name} ({total / 1e6:.2f} MB in {n_chunks} chunks)...")
    with tempfile.TemporaryDirectory(prefix="colab_chunks_") as tmpdir:
        with open(zip_path, "rb") as src:
            for idx in range(n_chunks):
                chunk_file = Path(tmpdir) / f"part_{idx:03d}.bin"
                chunk_file.write_bytes(src.read(CHUNK_BYTES))
                remote = f"/content/bundle_part_{idx:03d}.bin"
                _run_colab(["upload", "-s", session, str(chunk_file), remote], check=True)
                log(f"  uploaded chunk {idx + 1}/{n_chunks}")

    log("[colab] Reassembling and unpacking bundle on VM...")
    unpack_code = textwrap.dedent(f"""
        import os, shutil, zipfile
        with open("/content/bundle.zip", "wb") as out_f:
            for idx in range({n_chunks}):
                p = f"/content/bundle_part_{{idx:03d}}.bin"
                with open(p, "rb") as in_f:
                    shutil.copyfileobj(in_f, out_f)
                os.remove(p)
        shutil.rmtree("/content/bundle", ignore_errors=True)
        os.makedirs("/content/bundle", exist_ok=True)
        with zipfile.ZipFile("/content/bundle.zip", "r") as zf:
            zf.extractall("/content/bundle")
        os.remove("/content/bundle.zip")
        print("UNPACKED:", sorted(os.listdir("/content/bundle/blender")), flush=True)
    """).strip()
    _exec_script(session, unpack_code, timeout=120, telemetry=telemetry, retries=3, label="bundle-unpack")


def _download_one_file(session: str, remote_path: str, local_dst: Path, *, delete_remote: bool = True) -> bool:
    local_dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = local_dst.with_name(local_dst.name + ".part")
    for attempt in range(3):
        res = _run_colab(["download", "-s", session, remote_path, str(tmp_dst)], check=False, capture=True)
        if res.returncode == 0 and tmp_dst.is_file() and tmp_dst.stat().st_size > 0:
            os.replace(tmp_dst, local_dst)
            if delete_remote:
                _run_colab(["rm", "-s", session, remote_path], check=False, capture=True)
            return True
        time.sleep(1.0 * (attempt + 1))
    print(f"[warn] Failed to download {remote_path} -> {local_dst}", file=sys.stderr)
    return False


def _run_remote_job(
    session: str,
    shot_name: str,
    job_slug: str,
    local_out_dir: Path,
    prefix: str,
    f0: int,
    f1: int,
    samples: int,
    extras: list[str],
    missing: list[int],
    par: int,
    telemetry: Optional[_RenderTelemetry] = None,
    fmt: str = "png",
) -> tuple[int, int]:
    """Execute Blender render job on Colab and stream completed frames back locally."""
    local_out_dir.mkdir(parents=True, exist_ok=True)
    remote_out = f"/content/out/{job_slug}"
    frame_spec = _compress_ranges(missing)
    par_eff = max(1, min(par, len(missing)))

    job_extras = [x for x in extras if not x.startswith("frames=")]
    inline_spec = frame_spec if len(frame_spec) < 1000 else None

    driver_code = textwrap.dedent(f"""
        import os, shutil, subprocess, sys, threading, time
        shutil.rmtree("/content/claims", ignore_errors=True)
        remote_out = {remote_out!r}
        os.makedirs(remote_out, exist_ok=True)
        spec = {frame_spec!r}
        extras = {job_extras!r}
        if {inline_spec is None}:
            with open("/content/frame_spec.txt", "w", encoding="utf-8") as fh:
                fh.write(spec)
            extras = extras + ["frames=@/content/frame_spec.txt"]
        else:
            extras = extras + [f"frames={{spec}}"]
        cmd = [
            "/content/blender/blender", "-b", "/content/bundle/blender/{shot_name}",
            "-P", "/content/bundle/cloud_render.py", "--",
            remote_out, {prefix!r}, str({f0}), str({f1}), str({samples}),
        ] + extras
        par = {par_eff}
        print(f"LAUNCH par={{par}} missing={len(missing)} cmd={{' '.join(cmd)}}", flush=True)
        def pump(p, k):
            for line in p.stdout:
                if line.startswith(("FRAME ", "DONE ", "DEVICE ", "DENOISER ", "ENGINE ", "PNG ", "EEVEE ", "GPU_BACKEND ", "OPTIX", "ASSET ", "Traceback")) or "Error" in line:
                    print(f"[{{k}}] {{line.rstrip()}}", flush=True)
        ps = []
        for k in range(par):
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            t = threading.Thread(target=pump, args=(p, k), daemon=True)
            t.start()
            ps.append((p, t))
            if k + 1 < par:
                time.sleep(15)
        rc = 0
        for p, t in ps:
            p.wait()
            t.join()
            if p.returncode:
                rc = p.returncode
        print(f"REMOTE_EXIT rc={{rc}}", flush=True)
        if rc != 0:
            sys.exit(rc)
    """).strip()

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
        tf.write(driver_code)
        driver_path = tf.name

    downloaded_files: set[str] = set()
    futures: list[concurrent.futures.Future] = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    rc = 0

    total_frames = f1 - f0 + 1
    done_so_far = total_frames - len(missing)
    idle_limit = 900.0  # 15 min without any output -> kill hung colab websocket instead of burning CU forever
    last_activity = {"t": time.monotonic()}
    stop_watch = threading.Event()
    hung = {"flag": False}

    try:
        proc = subprocess.Popen(
            [_find_colab(), "exec", "-s", session, "--timeout", "0", "-f", driver_path],
            env=_colab_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATION_FLAGS,
        )

        def _idle_watchdog() -> None:
            while not stop_watch.wait(15.0):
                if time.monotonic() - last_activity["t"] > idle_limit:
                    hung["flag"] = True
                    msg = f"[warn] No render output for {int(idle_limit)}s — killing hung colab exec PID {proc.pid}"
                    if telemetry:
                        telemetry.log(msg)
                    else:
                        print(msg, flush=True)
                    _kill_proc_tree(proc.pid)
                    return

        wt = threading.Thread(target=_idle_watchdog, daemon=True)
        wt.start()
        assert proc.stdout is not None
        for line in proc.stdout:
            last_activity["t"] = time.monotonic()
            s = line.rstrip()
            if telemetry:
                telemetry.log(s)
            else:
                print(s, flush=True)
            m = FRAME_LINE_RE.search(s)
            if m:
                done_so_far = min(total_frames, done_so_far + 1)
                primary_local = ""
                if m.group(3):
                    fnames = [x.strip() for x in m.group(3).split(",") if x.strip()]
                    for fn in fnames:
                        if fn.lower().endswith(".png") and not fn.lower().endswith("_mist.png"):
                            primary_local = str(local_out_dir / fn)
                    for fname in fnames:
                        if fname not in downloaded_files:
                            downloaded_files.add(fname)
                            futures.append(
                                pool.submit(
                                    _download_one_file,
                                    session,
                                    f"{remote_out}/{fname}",
                                    local_out_dir / fname,
                                )
                            )
                if telemetry:
                    try:
                        telemetry.record_frame(m.group(1), float(m.group(2)), done_so_far, primary_local)
                    except ValueError:
                        pass
        stop_watch.set()
        rc = proc.wait()
        if hung["flag"] and rc == 0:
            rc = 124
    finally:
        stop_watch.set()
        for fut in futures:
            try:
                fut.result()
            except Exception as e:
                print(f"[warn] Download thread error: {e}", file=sys.stderr)
        pool.shutdown(wait=True)
        try:
            os.remove(driver_path)
        except OSError:
            pass

    # Final sweep of remote_out for any remaining completed files
    ls_res = _run_colab(["ls", "-s", session, remote_out], check=False, capture=True)
    if ls_res.returncode == 0:
        for line in ls_res.stdout.splitlines():
            fname = line.strip()
            if not fname or fname.endswith("/") or fname.endswith(".part") or fname.startswith("[colab]"):
                continue
            dst = local_out_dir / fname
            if not (dst.is_file() and dst.stat().st_size > 0):
                if _download_one_file(session, f"{remote_out}/{fname}", dst):
                    downloaded_files.add(fname)

    return len(downloaded_files), rc


def _maybe_open_monitor_gui(no_gui: bool) -> None:
    """Auto-open Colab CLI Studio on the Live Monitor tab (detached, single-instance, zero-CMD)."""
    if no_gui or os.environ.get("COLAB_NO_GUI", "").strip().lower() in ("1", "true", "yes"):
        return
    if not ACCOUNTS_SCRIPT.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(ACCOUNTS_SCRIPT), "monitor"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATION_FLAGS,
            timeout=8,
            check=False,
        )
    except Exception:
        pass


def _split_chunks(items: list[int], n: int) -> list[list[int]]:
    """Split a sorted list into n non-empty contiguous chunks of nearly equal size."""
    n = max(1, min(n, len(items)))
    q, r = divmod(len(items), n)
    chunks: list[list[int]] = []
    start = 0
    for i in range(n):
        sz = q + (1 if i < r else 0)
        chunks.append(items[start : start + sz])
        start += sz
    return chunks


def _build_shard_commands(planned: list, args: argparse.Namespace) -> list[dict]:
    """Build per-VM shard specs + child CLI commands when --vms > 1."""
    total_missing = sum(len(item[9]) for item in planned)
    n_vms = max(1, min(int(getattr(args, "vms", 1) or 1), total_missing))
    base_ts = int(time.time())
    parent_pid = os.getpid()
    base_session = args.session
    shards: list[dict] = []

    if args.cmd == "shot" and len(planned) == 1:
        j, blend_path, out_dir, prefix, _f0, _f1, samples, extras, _fmt, missing = planned[0]
        chunks = _split_chunks(missing, n_vms)
        clean_extras = [x for x in extras if not x.startswith("frames=")]
        for idx, chunk in enumerate(chunks, 1):
            run_id = f"{base_ts}_{parent_pid}_v{idx}"
            sess = f"{base_session}-{idx}"
            shard_name = f"{j['name']} [{idx}/{len(chunks)}]"
            sf0, sf1 = chunk[0], chunk[-1]
            cmd = [
                sys.executable,
                str(Path(__file__).resolve()),
                "shot",
                str(blend_path),
                "--out",
                str(out_dir),
                "--prefix",
                prefix,
                "--f0",
                str(sf0),
                "--f1",
                str(sf1),
                "--samples",
                str(samples),
                "--name",
                shard_name,
                "--gpu",
                args.gpu,
                "-s",
                sess,
                "--par",
                str(args.par),
                "--vms",
                "1",
                "--wait",
                "--no-gui",
                "--_run-id",
                run_id,
            ]
            for ex in clean_extras:
                cmd += ["--extra", ex]
            if args.high_mem:
                cmd.append("--high-mem")
            if args.blender:
                cmd += ["--blender", args.blender]
            if args.blender_version:
                cmd += ["--blender-version", args.blender_version]
            if args.project_root:
                cmd += ["--project-root", args.project_root]
            if args.out_root:
                cmd += ["--out-root", args.out_root]
            if args.keep:
                cmd.append("--keep")
            if args.stop_existing:
                cmd.append("--stop-existing")
            if args.skip_prepare:
                cmd.append("--skip-prepare")
            shards.append(
                {
                    "idx": idx,
                    "run_id": run_id,
                    "session": sess,
                    "name": shard_name,
                    "frames_desc": f"{sf0}-{sf1} ({len(chunk)} frames)",
                    "out_dir": str(out_dir),
                    "cmd": cmd,
                }
            )
        return shards

    # Queue mode: shard every planned job's missing frames across n_vms buckets
    per_vm_jobs: list[list[dict]] = [[] for _ in range(n_vms)]
    for j, blend_path, out_dir, prefix, _f0, _f1, samples, extras, _fmt, missing in planned:
        chunks = _split_chunks(missing, min(n_vms, len(missing)))
        clean_extras = [x for x in extras if not x.startswith("frames=")]
        for idx, chunk in enumerate(chunks):
            per_vm_jobs[idx].append(
                {
                    "name": f"{j['name']} [{idx + 1}/{len(chunks)}]",
                    "blend": str(blend_path),
                    "out": str(out_dir),
                    "prefix": prefix,
                    "f0": chunk[0],
                    "f1": chunk[-1],
                    "samples": samples,
                    "extra": clean_extras,
                }
            )

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    active_buckets = [b for b in per_vm_jobs if b]
    for idx, bucket in enumerate(active_buckets, 1):
        run_id = f"{base_ts}_{parent_pid}_v{idx}"
        sess = f"{base_session}-{idx}"
        shard_q = JOBS_DIR / f"{run_id}_queue.json"
        shard_q.write_text(json.dumps(bucket, indent=2), encoding="utf-8")
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "queue",
            str(shard_q),
            "--gpu",
            args.gpu,
            "-s",
            sess,
            "--par",
            str(args.par),
            "--vms",
            "1",
            "--wait",
            "--no-gui",
            "--_run-id",
            run_id,
        ]
        if args.high_mem:
            cmd.append("--high-mem")
        if args.blender:
            cmd += ["--blender", args.blender]
        if args.blender_version:
            cmd += ["--blender-version", args.blender_version]
        if args.project_root:
            cmd += ["--project-root", args.project_root]
        if args.out_root:
            cmd += ["--out-root", args.out_root]
        if args.keep:
            cmd.append("--keep")
        if args.stop_existing:
            cmd.append("--stop-existing")
        if args.skip_prepare:
            cmd.append("--skip-prepare")
        n_fr = sum(int(x["f1"]) - int(x["f0"]) + 1 for x in bucket)
        shards.append(
            {
                "idx": idx,
                "run_id": run_id,
                "session": sess,
                "name": ", ".join(x["name"] for x in bucket),
                "frames_desc": f"{len(bucket)} job(s), ~{n_fr} frames",
                "out_dir": bucket[0]["out"],
                "cmd": cmd,
            }
        )
    return shards


def _submit_via_gui_inbox(job_specs: list[tuple[str, list[str]]]) -> Optional[dict[str, int]]:
    """Hand off detached render launch(es) to the running Colab Render desktop GUI.

    When an AI agent runs a command on Windows, the agent's shell Job Object kills direct
    child processes as soon as the command exits. Submitting through the user's running
    Colab Render desktop app spawns workers in the desktop session so they survive shell exit.
    """
    try:
        if not GUI_HEARTBEAT_PATH.is_file():
            return None
        hb = json.loads(GUI_HEARTBEAT_PATH.read_text(encoding="utf-8"))
        if not hb.get("inbox"):
            return None
        if time.time() - float(hb.get("ts") or 0.0) > 6.0:
            return None
    except Exception:
        return None

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    req_id = f"{int(time.time() * 1000)}_{os.getpid()}"
    req_path = INBOX_DIR / f"{req_id}.req.json"
    ack_path = INBOX_DIR / f"{req_id}.ack.json"
    payload = {
        "req_id": req_id,
        "cwd": os.getcwd(),
        "jobs": [{"run_id": rid, "cmd": cmd} for rid, cmd in job_specs],
    }
    try:
        tmp = req_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, req_path)
    except OSError:
        return None

    wait_sec = 4.0 + 0.6 * max(0, len(job_specs) - 1)
    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        if ack_path.is_file():
            try:
                ack = json.loads(ack_path.read_text(encoding="utf-8"))
                ack_path.unlink(missing_ok=True)
                if ack.get("ok") and isinstance(ack.get("pids"), dict):
                    return {str(k): int(v) for k, v in ack["pids"].items()}
            except Exception:
                pass
            return None
        time.sleep(0.08)

    req_path.unlink(missing_ok=True)
    return None


def _run_multi_vm(planned: list, args: argparse.Namespace) -> int:
    """Launch N parallel Colab VMs, each rendering a disjoint slice of missing frames."""
    shards = _build_shard_commands(planned, args)
    detach = _should_detach(args)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)

    if detach:
        inbox_pids = _submit_via_gui_inbox([(s["run_id"], s["cmd"]) for s in shards])
        if inbox_pids is not None:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if all((JOBS_DIR / f"{s['run_id']}.json").is_file() for s in shards):
                    break
                time.sleep(0.05)
            print(f"[detached via Colab Render GUI] Started {len(shards)} parallel Colab {args.gpu.upper()} VM shard(s):")
            for s in shards:
                pid = inbox_pids.get(s["run_id"], 0)
                print(f"  [{s['idx']}/{len(shards)}] {s['name']} | session={s['session']} | {s['frames_desc']} | pid={pid} | run_id={s['run_id']}")
            print(f"[detached] Output : {shards[0]['out_dir']}")
            print(f"[detached] Monitor: Colab Render GUI (each VM auto-stops when its shard finishes)")
            print(
                "[detached] Handoff complete — do NOT poll or wait for completion unless a follow-up step needs "
                "the finished frames (pass --wait to block)."
            )
            return 0

        _maybe_open_monitor_gui(getattr(args, "no_gui", False))
        kwargs: dict = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=_colab_env(),
        )
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            kwargs["start_new_session"] = True

        launched: list[tuple[dict, subprocess.Popen]] = []
        for idx, s in enumerate(shards):
            if idx > 0:
                time.sleep(0.5)
            proc = subprocess.Popen(s["cmd"], **kwargs)
            launched.append((s, proc))

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if all((JOBS_DIR / f"{s['run_id']}.json").is_file() or p.poll() is not None for s, p in launched):
                break
            time.sleep(0.05)

        print(f"[detached] Started {len(launched)} parallel Colab {args.gpu.upper()} VM shard(s):")
        bad = 0
        for s, proc in launched:
            rc = proc.poll()
            st = f"EXITED({rc})" if rc is not None else f"pid={proc.pid}"
            if rc not in (None, 0):
                bad += 1
            print(f"  [{s['idx']}/{len(launched)}] {s['name']} | session={s['session']} | {s['frames_desc']} | {st} | run_id={s['run_id']}")
        print(f"[detached] Output : {shards[0]['out_dir']}")
        print(f"[detached] Monitor: Colab Render GUI (each VM auto-stops when its shard finishes)")
        if os.name == "nt":
            print(
                "[note] Colab Render GUI inbox was not active — launched directly. If running inside an AI agent "
                "shell on Windows, keep Colab_Accounts.bat open so detached renders survive shell exit."
            )
        print(
            "[detached] Handoff complete — do NOT poll or wait for completion unless a follow-up step needs "
            "the finished frames (pass --wait to block)."
        )
        return 1 if bad else 0

    _maybe_open_monitor_gui(getattr(args, "no_gui", False))

    # Blocking --wait mode: run all shards concurrently and stream prefixed logs
    procs: list[tuple[dict, subprocess.Popen]] = []
    threads: list[threading.Thread] = []
    print_lock = threading.Lock()

    def _pump(label: str, pipe) -> None:
        for line in pipe:
            with print_lock:
                print(f"[{label}] {line.rstrip()}", flush=True)

    try:
        for s in shards:
            p = subprocess.Popen(
                s["cmd"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=_colab_env(),
                creationflags=CREATION_FLAGS,
            )
            procs.append((s, p))
            assert p.stdout is not None
            t = threading.Thread(target=_pump, args=(s["session"], p.stdout), daemon=True)
            t.start()
            threads.append(t)

        overall = 0
        for _s, p in procs:
            rc = p.wait()
            if rc != 0:
                overall = rc
        for t in threads:
            t.join(timeout=2.0)
        return overall
    except KeyboardInterrupt:
        print("\n[colab] Stopping all parallel VM shards...", flush=True)
        for _s, p in procs:
            _kill_proc_tree(p.pid)
        return 130


def _should_detach(args: argparse.Namespace) -> bool:
    if getattr(args, "dry_run", False):
        return False
    if getattr(args, "wait", False) or getattr(args, "_run_id", None):
        return False
    if os.environ.get("COLAB_WAIT", "").strip().lower() in ("1", "true", "yes"):
        return False
    return True


def _spawn_detached_render(planned: list, args: argparse.Namespace) -> int:
    """Spawn the render in a fully detached background process and exit immediately."""
    run_id = f"{int(time.time())}_{os.getpid()}"
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = JOBS_DIR / f"{run_id}.json"
    log_path = JOBS_DIR / f"{run_id}.log"

    first_job, first_blend, first_out, _p, f0, f1, _s, _ex, _fmt, first_missing = planned[0]
    total_missing = sum(len(item[9]) for item in planned)
    job_names = ", ".join(item[0]["name"] for item in planned)

    # Reconstruct child command with --wait, --no-gui (parent opens GUI once), and shared --_run-id
    child_args = [a for a in sys.argv[1:] if a != "--detach"]
    child_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        *child_args,
        "--wait",
        "--no-gui",
        "--_run-id",
        run_id,
    ]

    inbox_pids = _submit_via_gui_inbox([(run_id, child_cmd)])
    if inbox_pids is not None:
        pid = inbox_pids.get(run_id, 0)
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            if json_path.is_file():
                break
            time.sleep(0.05)
        print(f"[detached via Colab Render GUI] Render started (run_id={run_id}, pid={pid}, gpu={args.gpu.upper()}, session={args.session})")
        print(f"[detached] Jobs   : {job_names} ({total_missing} frame(s) queued)")
        print(f"[detached] Output : {first_out}")
        print(f"[detached] Log    : {log_path}")
        print(f"[detached] Monitor: Colab Render GUI (VM auto-stops when done)")
        print(
            "[detached] Handoff complete — do NOT poll or wait for completion unless a follow-up step needs "
            "the finished frames (pass --wait to block)."
        )
        return 0

    kwargs: dict = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=_colab_env(),
    )
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(child_cmd, **kwargs)

    # Wait briefly for the worker to initialize its telemetry file (or catch immediate startup crashes)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(
                f"ERROR: Detached render worker exited immediately (code {proc.returncode}). "
                f"Re-run with --wait to see error output."
            )
        if json_path.is_file():
            break
        time.sleep(0.05)

    _maybe_open_monitor_gui(getattr(args, "no_gui", False))

    print(f"[detached] Render started in background (run_id={run_id}, pid={proc.pid}, gpu={args.gpu.upper()}, session={args.session})")
    print(f"[detached] Jobs   : {job_names} ({total_missing} frame(s) queued)")
    print(f"[detached] Output : {first_out}")
    print(f"[detached] Log    : {log_path}")
    print(f"[detached] Monitor: Colab Render GUI (VM auto-stops when done)")
    if os.name == "nt":
        print(
            "[note] Colab Render GUI inbox was not active — launched directly. If running inside an AI agent "
            "shell on Windows, keep Colab_Accounts.bat open so detached renders survive shell exit."
        )
    print(
        "[detached] Handoff complete — do NOT poll or wait for completion unless a follow-up step needs "
        "the finished frames (pass --wait to block)."
    )
    return 0


def _execute_jobs(jobs: list[dict], args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd().resolve()
    out_root = Path(args.out_root).resolve() if args.out_root else project_root

    planned = []
    for j in jobs:
        out_dir = _resolve_out_dir(j["out"], out_root)
        prefix = j.get("prefix", "f_")
        f0 = int(j["f0"])
        f1 = int(j["f1"])
        samples = int(j.get("samples", 0))
        extras = list(j.get("extra", []))
        fmt = _parse_fmt(extras)
        missing = _missing_frames(out_dir, prefix, f0, f1, fmt)
        total = f1 - f0 + 1
        done = total - len(missing)
        print(f"[plan] {j['name']}: {done}/{total} frames already in {out_dir}")
        if missing:
            blend_path = _resolve_blend_path(j["blend"], project_root)
            planned.append((j, blend_path, out_dir, prefix, f0, f1, samples, extras, fmt, missing))

    if not planned:
        print("[done] All requested frames already exist locally. No Colab VM allocated.")
        return 0

    # Fail fast before detaching if local prerequisites are missing
    _find_colab()
    blender_exe = None if args.skip_prepare else _find_blender(args.blender)

    if not args.dry_run and int(getattr(args, "vms", 1) or 1) > 1:
        total_missing = sum(len(item[9]) for item in planned)
        if total_missing > 1:
            return _run_multi_vm(planned, args)

    if _should_detach(args):
        return _spawn_detached_render(planned, args)

    blender_ver = _detect_blender_version(args.blender_version, blender_exe)

    telemetry = _RenderTelemetry(enabled=not args.dry_run, run_id=getattr(args, "_run_id", None))
    telemetry.update(
        status="preparing",
        job_count=len(planned),
        gpu=args.gpu.upper(),
        session=args.session,
        par=args.par,
    )
    if not args.dry_run:
        _maybe_open_monitor_gui(getattr(args, "no_gui", False))

    with tempfile.TemporaryDirectory(prefix="blender_colab_stage_") as tmp_stage:
        stage_root = Path(tmp_stage)
        staged = []
        for idx, item in enumerate(planned, 1):
            j, blend_path, out_dir, prefix, f0, f1, samples, extras, fmt, missing = item
            telemetry.update(
                status="preparing",
                job_index=idx,
                job_name=j["name"],
                blend=str(blend_path),
                out_dir=str(out_dir),
                total_frames=f1 - f0 + 1,
                done_frames=(f1 - f0 + 1) - len(missing),
            )
            telemetry.log(f"[prepare] Staging {j['name']} ({blend_path.name})...")
            zip_path, shot_name = _stage_and_zip_shot(
                blend_path, stage_root, project_root, blender_exe, skip_prepare=args.skip_prepare, dry=args.dry_run
            )
            staged.append((j, zip_path, shot_name, out_dir, prefix, f0, f1, samples, extras, fmt, missing))

        if args.dry_run:
            print(f"[dry-run] {len(staged)} job(s) ready (Colab Blender {blender_ver}). No VM allocated.")
            return 0

        created_session = False
        overall_rc = 0
        try:
            telemetry.update(status="provisioning")
            telemetry.log(f"[colab] Provisioning session '{args.session}' (gpu={args.gpu.upper()})...")
            created_session = _ensure_session(args.session, args.gpu, args.high_mem, telemetry=telemetry)
            _ensure_remote_blender(args.session, blender_ver, telemetry=telemetry)

            for idx, (j, zip_path, shot_name, out_dir, prefix, f0, f1, samples, extras, fmt, missing) in enumerate(staged, 1):
                assert zip_path is not None
                total = f1 - f0 + 1
                telemetry.update(
                    status="uploading",
                    job_index=idx,
                    job_name=j["name"],
                    blend=shot_name,
                    out_dir=str(out_dir),
                    total_frames=total,
                    done_frames=total - len(missing),
                )
                telemetry.log(f"\n=== [{idx}/{len(staged)}] {j['name']} ({len(missing)} missing frame(s)) ===")
                _upload_bundle(args.session, zip_path, telemetry=telemetry)
                job_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", j["out"]).strip("_") or f"job_{idx}"
                t0 = time.time()
                telemetry.update(status="rendering")
                dl_count, rc = _run_remote_job(
                    args.session,
                    shot_name,
                    job_slug,
                    out_dir,
                    prefix,
                    f0,
                    f1,
                    samples,
                    extras,
                    missing,
                    args.par,
                    telemetry=telemetry,
                    fmt=fmt,
                )
                rem = _missing_frames(out_dir, prefix, f0, f1, fmt)
                telemetry.update(done_frames=total - len(rem))
                telemetry.log(
                    f"[summary] {j['name']}: {total - len(rem)}/{total} present locally "
                    f"(downloaded {dl_count} file(s) in {time.time() - t0:.1f}s, rc={rc})"
                )
                if rc != 0 or rem:
                    overall_rc = rc or 1
        except KeyboardInterrupt:
            telemetry.log("\n[colab] Interrupted by user.")
            telemetry.update(status="stopped", error="Interrupted by user")
            overall_rc = 130
        except Exception as e:
            telemetry.log(f"[error] {e}")
            telemetry.update(status="error", error=str(e))
            raise
        finally:
            if not args.keep and (created_session or args.stop_existing):
                telemetry.log(f"[colab] Stopping session '{args.session}' to stop compute billing...")
                _run_colab(["stop", "-s", args.session], check=False)
            elif args.keep:
                telemetry.log(f"[colab] Keeping session '{args.session}' alive (--keep). Stop later with: python colab_render.py stop -s {args.session}")
            if telemetry.state.get("status") not in ("stopped", "error"):
                telemetry.update(status="done" if overall_rc == 0 else "error")

        return overall_rc


def cmd_login(_args: argparse.Namespace) -> int:
    print("Starting Google OAuth2 sign-in for `colab` CLI...")
    print("Open the URL printed below in your browser, approve access, and paste the code back here.\n")
    res = _run_colab(["sessions"], check=False)
    if res.returncode == 0:
        print("\n[ok] Authenticated! Checking compute-unit usage:")
        _run_colab(["usage"], check=False)
    return res.returncode


def cmd_status(args: argparse.Namespace) -> int:
    print(f"colab   : {_find_colab()}")
    try:
        print(f"blender : {_find_blender(args.blender)}")
    except SystemExit as e:
        print(f"blender : {e}")
    print("\n--- Active account ---")
    subprocess.run([sys.executable, str(ACCOUNTS_SCRIPT), "active"], check=False)
    print("\n--- Active Colab sessions ---")
    r1 = _run_colab(["sessions"], check=False)
    print("\n--- Compute-unit usage ---")
    r2 = _run_colab(["usage"], check=False)
    return r1.returncode or r2.returncode


def cmd_gui(_args: argparse.Namespace) -> int:
    return subprocess.call([sys.executable, str(ACCOUNTS_SCRIPT), "gui"])


def cmd_accounts(args: argparse.Namespace) -> int:
    sub_args = ["switch", args.switch_to] if args.switch_to else ["list"]
    return subprocess.call([sys.executable, str(ACCOUNTS_SCRIPT), *sub_args])


def cmd_stop(args: argparse.Namespace) -> int:
    return _run_colab(["stop", "-s", args.session], check=False).returncode


def cmd_shot(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd().resolve()
    blend_path = _resolve_blend_path(args.blend, project_root)
    f1 = args.f1 if args.f1 is not None else args.f0
    out = args.out or str(Path("renders") / blend_path.stem)
    job = {
        "name": args.name or blend_path.stem,
        "blend": str(blend_path),
        "out": out,
        "prefix": args.prefix,
        "f0": args.f0,
        "f1": f1,
        "samples": args.samples,
        "extra": list(args.extra or []),
    }
    return _execute_jobs([job], args)


def cmd_queue(args: argparse.Namespace) -> int:
    qpath = Path(args.jobs_json)
    if not qpath.is_file():
        raise SystemExit(f"ERROR: Jobs file not found: {args.jobs_json}")
    jobs = json.loads(qpath.read_text(encoding="utf-8"))
    if args.only:
        needle = args.only.lower()
        jobs = [j for j in jobs if needle in j.get("name", "").lower() or needle in j.get("blend", "").lower()]
        if not jobs:
            raise SystemExit(f"ERROR: No jobs matched --only {args.only!r}")
    return _execute_jobs(jobs, args)


def _add_render_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gpu", default="L4", help="Colab GPU: T4, L4 (default), G4, A100, H100, or CPU")
    p.add_argument("--high-mem", action="store_true", help="Request high-RAM shape (non-L4 GPUs)")
    p.add_argument("-s", "--session", default=DEFAULT_SESSION, help=f"Colab session name (default: {DEFAULT_SESSION})")
    p.add_argument("--par", type=int, default=3, help="Parallel Blender processes per GPU VM (default: 3)")
    p.add_argument(
        "--vms",
        type=int,
        default=1,
        help="Number of parallel Colab GPU VMs to shard missing frames across (default: 1)",
    )
    p.add_argument("--blender", help="Path to local Blender binary (or set BLENDER_EXE)")
    p.add_argument("--blender-version", help="Linux Blender version to install on Colab (default: match local Blender)")
    p.add_argument("--project-root", help="Project root for resolving relative paths (default: current directory)")
    p.add_argument("--out-root", help="Base directory for relative --out paths (default: project root)")
    p.add_argument("--keep", action="store_true", help="Keep the Colab VM running after finishing")
    p.add_argument("--stop-existing", action="store_true", help="Also stop the session even if it already existed before this run")
    p.add_argument("--skip-prepare", action="store_true", help="Upload the .blend directly without running prepare_shot.py")
    p.add_argument("--dry-run", action="store_true", help="Check assets and missing frames without allocating a VM")
    p.add_argument(
        "--wait",
        action="store_true",
        help="Wait in the foreground until rendering finishes (default: detach immediately to background & Live Monitor GUI)",
    )
    p.add_argument("--detach", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--_run-id", dest="_run_id", default=None, help=argparse.SUPPRESS)
    p.add_argument("--no-gui", action="store_true", help="Do not auto-open the Live Monitor GUI when rendering starts")


def main() -> int:
    ap = argparse.ArgumentParser(description="Render Blender scenes on Google Colab via `colab` CLI.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="Authenticate `colab` CLI with Google OAuth2 (terminal flow)")
    p_login.set_defaults(func=cmd_login)

    p_gui = sub.add_parser("gui", help="Open the Colab Account Switcher GUI (add/switch accounts, check CUs)")
    p_gui.set_defaults(func=cmd_gui)

    p_accounts = sub.add_parser("accounts", help="List saved Colab accounts or switch active account")
    p_accounts.add_argument("--switch", dest="switch_to", help="Switch active account to this email (or substring)")
    p_accounts.set_defaults(func=cmd_accounts)

    p_status = sub.add_parser("status", help="Show local tools, active account, Colab sessions, and CU balance")
    p_status.add_argument("--blender", help="Path to local Blender binary")
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="Stop a running Colab session")
    p_stop.add_argument("-s", "--session", default=DEFAULT_SESSION, help=f"Session name (default: {DEFAULT_SESSION})")
    p_stop.set_defaults(func=cmd_stop)

    p_shot = sub.add_parser("shot", help="Prepare, upload, render, and download a single Blender shot")
    p_shot.add_argument("blend", help="Path to .blend file")
    p_shot.add_argument("--out", help="Output folder (default: renders/<blend_stem>)")
    p_shot.add_argument("--prefix", default="f_", help="Frame filename prefix (default: f_)")
    p_shot.add_argument("--f0", type=int, default=1, help="Start frame (default: 1)")
    p_shot.add_argument("--f1", type=int, default=None, help="End frame (defaults to --f0)")
    p_shot.add_argument("--samples", type=int, default=0, help="Cycles/EEVEE sample override (0 = blend default)")
    p_shot.add_argument("--name", help="Display name for logs")
    p_shot.add_argument(
        "--extra",
        action="append",
        default=[],
        help="Extra cloud_render.py arg (repeatable), e.g. --extra pct=50 --extra fmt=exr --extra hide=Fog*",
    )
    _add_render_flags(p_shot)
    p_shot.set_defaults(func=cmd_shot)

    p_queue = sub.add_parser("queue", help="Run a jobs.json queue via Colab CLI")
    p_queue.add_argument("jobs_json", nargs="?", default="jobs.json", help="Path to jobs JSON (default: jobs.json)")
    p_queue.add_argument("--only", help="Filter queue jobs whose name or blend contains this substring")
    _add_render_flags(p_queue)
    p_queue.set_defaults(func=cmd_queue)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
