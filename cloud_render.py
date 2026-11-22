"""Colab-side Blender render worker (NVIDIA GPU). Honours the blend's own engine (EEVEE or Cycles).

Skips frames that already exist (resumable); lock files under /content/claims let parallel
Blender processes on one GPU split frames without colliding.

Usage:
  blender -b <bundle>/blender/<shot>.blend -P cloud_render.py -- <outdir> <prefix> <f0> <f1> <samples> [extras...]
    samples   0 = keep the blend's own (EEVEE taa_render_samples / Cycles samples). Cycles override defaults to 64.

Extras (key=value, or "--key value"):
  engine=cycles|eevee   Override the blend's engine. Cycles uses OptiX (CUDA fallback) + OIDN denoise.
  fmt=png               <prefix>NNNN.png using the blend's PNG settings (default).
  fmt=exr               <prefix>NNNN.exr multilayer 32-bit ZIP with all enabled view-layer passes.
  fmt=v7                <prefix>NNNN.png (16-bit RGB, colour-managed) + <prefix>NNNN_mist.png (16-bit grey);
                        add `keepexr` to also retain <prefix>NNNN.exr.
  camera=NAME           Render through a specific camera object.
  cams                  Write <prefix>NNNN.json with camera loc/lens/sensor/shift/res metadata.
  pct=N                 Resolution percentage override (default 100).
  hide=ObjA,ObjB        Comma-separated object names (or prefixes ending with `*`) to hide from render.
  reverse               Render from f1 down to f0.
  frames=SPEC           Optional subset of frames, e.g. "1,3,5-10" or "@/path/to/spec.txt".

The primary file (<prefix>NNNN.png or .exr) is written last, so its presence means the frame is complete.
"""
import bpy
import json
import os
import shutil
import sys
import tempfile
import time

a = sys.argv[sys.argv.index("--") + 1:]
out, pre, f0, f1, samples = a[0], a[1], int(a[2]), int(a[3]), int(a[4])
ex = a[5:]


def kv(key, default=None):
    for i, x in enumerate(ex):
        if x.startswith(key + "="):
            return x.split("=", 1)[1]
        if x == "--" + key and i + 1 < len(ex):
            return ex[i + 1]
    return default


rev, cams, keepexr = "reverse" in ex, "cams" in ex, "keepexr" in ex
pct = int(kv("pct", 100))
fmt = kv("fmt", "png").lower()
assert fmt in ("png", "exr", "v7"), f"unknown fmt={fmt}"
os.makedirs(out, exist_ok=True)
WORK = "/content" if os.path.isdir("/content") else tempfile.gettempdir()
CLAIMS = os.path.join(WORK, "claims")

sc = bpy.context.scene
own = sc.render.engine
want = (kv("engine") or "").lower()
engine = "CYCLES" if want == "cycles" else ("EEVEE" if want == "eevee" else ("CYCLES" if own == "CYCLES" else "EEVEE"))
print("ENGINE blend=%s render=%s fmt=%s" % (own, engine, fmt), flush=True)
sc.render.resolution_percentage = pct
cam = kv("camera")
if cam:
    sc.camera = bpy.data.objects[cam]

# ------------------------------------------------------------------ external files check
missing = []
for coll in (bpy.data.images, bpy.data.movieclips, getattr(bpy.data, "volumes", []), bpy.data.cache_files):
    for d in coll:
        if d.library or getattr(d, "packed_file", None) or getattr(d, "source", "FILE") in ("GENERATED", "VIEWER") or not d.filepath:
            continue
        if os.sep == "/" and "\\" in d.filepath:
            d.filepath = d.filepath.replace("\\", "/")
        if getattr(d, "users", 1) and not os.path.exists(bpy.path.abspath(d.filepath)):
            missing.append(d.filepath)
if missing:
    print("ASSET MISSING (run prepare_shot.py, check bundle) Error:", missing, flush=True)
    sys.exit(4)

# ------------------------------------------------------------------ engine & GPU setup
def use_device(dev):
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = dev
    prefs.get_devices()
    gpus = [d for d in prefs.devices if d.type == dev]
    for d in prefs.devices:
        d.use = (d.type == dev)
    sc.cycles.device = "GPU"
    print("DEVICE", dev, [d.name for d in gpus], flush=True)
    return bool(gpus)


if engine == "CYCLES":
    sc.render.engine = "CYCLES"
    cy = sc.cycles
    if samples > 0 or own != "CYCLES":
        cy.samples = samples if samples > 0 else 64
    cy.use_denoising = True
    try:
        cy.denoiser = "OPENIMAGEDENOISE"
    except Exception:
        pass
    try:
        cy.denoising_use_gpu = False
    except Exception:
        pass
    for dev in ("OPTIX", "CUDA"):
        try:
            if use_device(dev):
                break
        except Exception as e:
            print("device", dev, e)
    print("DENOISER", cy.use_denoising, getattr(cy, "denoiser", "?"), "samples", cy.samples, flush=True)
else:
    if own == "CYCLES":
        for eng in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
            try:
                sc.render.engine = eng
                break
            except TypeError:
                pass
    if samples > 0:
        sc.eevee.taa_render_samples = samples
    print("EEVEE samples", sc.eevee.taa_render_samples, flush=True)


def gpu_check():
    """EEVEE needs a real GPU context (EGL). Abort on software rasterisers instead of burning hours."""
    try:
        import gpu
        info = (
            gpu.platform.backend_type_get(),
            gpu.platform.vendor_get(),
            gpu.platform.renderer_get(),
            gpu.platform.version_get(),
        )
    except Exception as e:
        print("GPU_BACKEND unknown:", e, flush=True)
        return True
    print("GPU_BACKEND", *info, flush=True)
    soft = any(k in " ".join(map(str, info)).lower() for k in ("llvmpipe", "softpipe", "swrast", "software"))
    if soft:
        print("EEVEE SOFTWARE GL - not using the GPU. Error: stop and report.", flush=True)
    return not soft


hide_spec = kv("hide")
if hide_spec:
    patterns = [p.strip() for p in hide_spec.split(",") if p.strip()]
    for o in bpy.data.objects:
        if o.library is not None:
            continue
        if any((o.name.startswith(p[:-1]) if p.endswith("*") else o.name == p) for p in patterns):
            o.hide_render = True

# ------------------------------------------------------------------ output format
S = sc.render.image_settings


def set_exr():
    try:
        S.media_type = "MULTI_LAYER_IMAGE"
    except Exception:
        pass
    try:
        S.file_format = "OPEN_EXR_MULTILAYER"
    except Exception:
        S.file_format = "OPEN_EXR"
    S.color_depth = "32"
    S.exr_codec = "ZIP"


def set_png(mode=None, depth=None):
    try:
        S.media_type = "IMAGE"
    except Exception:
        pass
    S.file_format = "PNG"
    if mode:
        S.color_mode = mode
    if depth:
        S.color_depth = depth


if fmt == "png":
    if S.file_format != "PNG":
        set_png("RGBA" if sc.render.film_transparent else "RGB", "16")
    print("PNG", S.color_mode, S.color_depth, flush=True)
else:
    set_exr()


def mist_png(exr, dst):
    """Extract Mist pass from a multilayer EXR into a 16-bit greyscale PNG."""
    import numpy as np
    import OpenImageIO as oiio

    inp = oiio.ImageInput.open(exr)
    i = 0
    mist = None
    while inp.seek_subimage(i, 0):
        for j, n in enumerate(inp.spec().channelnames):
            if "Mist" in n:
                mist = inp.read_image(i, 0, j, j + 1, "float")
        i += 1
    inp.close()
    if mist is None:
        print("V7CG no mist channel found", flush=True)
        return False
    mist = np.clip(mist.reshape(mist.shape[0], mist.shape[1], 1), 0, 1)
    o = oiio.ImageOutput.create(dst)
    o.open(dst, oiio.ImageSpec(mist.shape[1], mist.shape[0], 1, "uint16"))
    o.write_image(np.ascontiguousarray(mist.astype(np.float32)))
    o.close()
    return True


def put(src, dst):
    shutil.copyfile(src, dst + ".part")
    os.replace(dst + ".part", dst)
    os.remove(src)


def render_frame(f):
    """Render frame `f` into WORK; returns [(tmp, final)] with the primary file last."""
    sc.frame_set(f)
    base = os.path.join(WORK, f"tmp_{os.getpid()}_{pre}{f:04d}")
    dst = os.path.join(out, f"{pre}{f:04d}")
    files = []
    if cams:
        co = sc.camera
        cd = co.data
        j = dict(
            loc=list(co.matrix_world.translation),
            lens=cd.lens,
            sensor=cd.sensor_width,
            shift_x=cd.shift_x,
            shift_y=cd.shift_y,
            w=sc.render.resolution_x * pct // 100,
            h=sc.render.resolution_y * pct // 100,
            camera=co.name,
            frame=f,
        )
        with open(base + ".json", "w", encoding="utf-8") as fh:
            json.dump(j, fh, indent=1)
        files.append((base + ".json", dst + ".json"))
    if fmt == "png":
        sc.render.filepath = base + ".png"
        bpy.ops.render.render(write_still=True)
        files.append((base + ".png", dst + ".png"))
    elif fmt == "exr":
        sc.render.filepath = base + ".exr"
        bpy.ops.render.render(write_still=True)
        files.append((base + ".exr", dst + ".exr"))
    else:  # v7
        set_exr()
        sc.render.filepath = base + ".exr"
        bpy.ops.render.render(write_still=True)
        set_png("RGB", "16")
        bpy.data.images["Render Result"].save_render(base + ".png", scene=sc)
        if mist_png(base + ".exr", base + "_mist.png"):
            files.append((base + "_mist.png", dst + "_mist.png"))
        if keepexr:
            files.append((base + ".exr", dst + ".exr"))
        else:
            os.remove(base + ".exr")
        files.append((base + ".png", dst + ".png"))
        set_exr()
    return files


def parse_frames(spec):
    if not spec:
        return None
    if spec.startswith("@"):
        with open(spec[1:], "r", encoding="utf-8") as fh:
            spec = fh.read().strip()
    keep = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_, b_ = part.split("-", 1)
            keep.update(range(int(a_), int(b_) + 1))
        else:
            keep.add(int(part))
    return keep


FRAME_FILTER = parse_frames(kv("frames"))
PRIMARY = ".exr" if fmt == "exr" else ".png"
t0 = time.time()
n = 0
checked = engine == "CYCLES"
os.makedirs(CLAIMS, exist_ok=True)
for f in (range(f1, f0 - 1, -1) if rev else range(f0, f1 + 1)):
    if FRAME_FILTER is not None and f not in FRAME_FILTER:
        continue
    p = f"{out}/{pre}{f:04d}{PRIMARY}"
    if os.path.exists(p) and os.path.getsize(p) > 0:
        continue
    try:  # parallel workers on the same VM: claim a frame so only one process renders it
        os.close(os.open(os.path.join(CLAIMS, f"{out.replace('/', '_')}_{pre}{f:04d}"), os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        continue
    t1 = time.time()
    try:
        files = render_frame(f)
    except RuntimeError as e:
        if engine != "CYCLES":
            raise
        print("OPTIX FAILED, falling back to CUDA:", e, flush=True)
        use_device("CUDA")
        t1 = time.time()
        files = render_frame(f)
    if not checked:  # first EEVEE frame: refuse software GL before writing output
        checked = True
        if not gpu_check():
            for tmp, _ in files:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            sys.exit(3)
    written = []
    for tmp, dst in files:
        put(tmp, dst)
        written.append(os.path.basename(dst))
    n += 1
    print(f"FRAME {pre}{f:04d} {time.time() - t1:.1f}s | {','.join(written)}", flush=True)
print(f"DONE {n} frames in {time.time() - t0:.0f}s", flush=True)
