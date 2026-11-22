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
    return res.returncode == 0


def _ensure_session(session: str, gpu: str, high_mem: bool) -> bool:
    """Ensure Colab session exists. Returns True if we created a new VM."""
    if _session_exists(session):
        print(f"[colab] Reusing existing session '{session}'")
        return False
    args = ["new", "-s", session]
    if gpu.upper() != "CPU":
        args += ["--gpu", gpu.upper()]
    if high_mem:
        args.append("--high-mem")
    print(f"[colab] Provisioning session '{session}' (gpu={gpu.upper()}, high_mem={high_mem})...")
    _run_colab(args, check=True)
    return True


def _exec_script(session: str, code: str, *, timeout: float = 0.0) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
        tf.write(code)
        tmp_path = tf.name
    try:
        _run_colab(["exec", "-s", session, "--timeout", str(timeout), "-f", tmp_path], check=True)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _ensure_remote_blender(session: str, version: str) -> None:
    tar_url = _blender_tar_url(version)
    print(f"[colab] Checking GPU & Blender {version} on '{session}'...")
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
    _exec_script(session, setup_code, timeout=600)


def _upload_bundle(session: str, zip_path: Path) -> None:
    total = zip_path.stat().st_size
    if total <= CHUNK_BYTES:
        print(f"[colab] Uploading {zip_path.name} ({total / 1e6:.2f} MB)...")
        _run_colab(["upload", "-s", session, str(zip_path), "/content/bundle.zip"], check=True)
        unpack_code = textwrap.dedent("""
            import os, shutil, zipfile
            shutil.rmtree("/content/bundle", ignore_errors=True)
            os.makedirs("/content/bundle", exist_ok=True)
            with zipfile.ZipFile("/content/bundle.zip", "r") as zf:
                zf.extractall("/content/bundle")
            os.remove("/content/bundle.zip")
            print("UNPACKED:", sorted(os.listdir("/content/bundle/blender")), flush=True)
        """).strip()
        _exec_script(session, unpack_code, timeout=300)
        return

    n_chunks = (total + CHUNK_BYTES - 1) // CHUNK_BYTES
    print(f"[colab] Uploading {zip_path.name} ({total / 1e6:.2f} MB in {n_chunks} chunks)...")
    with tempfile.TemporaryDirectory(prefix="colab_chunks_") as tmpdir:
        with open(zip_path, "rb") as src:
            for idx in range(n_chunks):
                chunk_file = Path(tmpdir) / f"part_{idx:03d}.bin"
                chunk_file.write_bytes(src.read(CHUNK_BYTES))
                remote = f"/content/bundle_part_{idx:03d}.bin"
                _run_colab(["upload", "-s", session, str(chunk_file), remote], check=True)
                print(f"  uploaded chunk {idx + 1}/{n_chunks}")

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
    _exec_script(session, unpack_code, timeout=300)


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
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            s = line.rstrip()
            print(s, flush=True)
            m = FRAME_LINE_RE.search(s)
            if m and m.group(3):
                for fname in [x.strip() for x in m.group(3).split(",") if x.strip()]:
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
        rc = proc.wait()
    finally:
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

    blender_exe = None if args.skip_prepare else _find_blender(args.blender)
    blender_ver = _detect_blender_version(args.blender_version, blender_exe)

    with tempfile.TemporaryDirectory(prefix="blender_colab_stage_") as tmp_stage:
        stage_root = Path(tmp_stage)
        staged = []
        for item in planned:
            j, blend_path, out_dir, prefix, f0, f1, samples, extras, fmt, missing = item
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
            created_session = _ensure_session(args.session, args.gpu, args.high_mem)
            _ensure_remote_blender(args.session, blender_ver)

            for idx, (j, zip_path, shot_name, out_dir, prefix, f0, f1, samples, extras, fmt, missing) in enumerate(staged, 1):
                assert zip_path is not None
                print(f"\n=== [{idx}/{len(staged)}] {j['name']} ({len(missing)} missing frame(s)) ===")
                _upload_bundle(args.session, zip_path)
                job_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", j["out"]).strip("_") or f"job_{idx}"
                t0 = time.time()
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
                )
                rem = _missing_frames(out_dir, prefix, f0, f1, fmt)
                total = f1 - f0 + 1
                print(
                    f"[summary] {j['name']}: {total - len(rem)}/{total} present locally "
                    f"(downloaded {dl_count} file(s) in {time.time() - t0:.1f}s, rc={rc})"
                )
                if rc != 0 or rem:
                    overall_rc = rc or 1
        except KeyboardInterrupt:
            print("\n[colab] Interrupted by user.", file=sys.stderr)
            overall_rc = 130
        finally:
            if not args.keep and (created_session or args.stop_existing):
                print(f"[colab] Stopping session '{args.session}' to stop compute billing...")
                _run_colab(["stop", "-s", args.session], check=False)
            elif args.keep:
                print(f"[colab] Keeping session '{args.session}' alive (--keep). Stop later with: python colab_render.py stop -s {args.session}")

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
    p.add_argument("--par", type=int, default=3, help="Parallel Blender processes on the GPU (default: 3)")
    p.add_argument("--blender", help="Path to local Blender binary (or set BLENDER_EXE)")
    p.add_argument("--blender-version", help="Linux Blender version to install on Colab (default: match local Blender)")
    p.add_argument("--project-root", help="Project root for resolving relative paths (default: current directory)")
    p.add_argument("--out-root", help="Base directory for relative --out paths (default: project root)")
    p.add_argument("--keep", action="store_true", help="Keep the Colab VM running after finishing")
    p.add_argument("--stop-existing", action="store_true", help="Also stop the session even if it already existed before this run")
    p.add_argument("--skip-prepare", action="store_true", help="Upload the .blend directly without running prepare_shot.py")
    p.add_argument("--dry-run", action="store_true", help="Check assets and missing frames without allocating a VM")


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
