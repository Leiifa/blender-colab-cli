"""Local, no rendering: build a portable cloud copy of a Blender scene for Colab.

Usage:
  blender -b <shot.blend> -P prepare_shot.py -- <shot> [--stage ./_colab_stage] [--root <project root>] [--dry]

Produces `<stage>/blender/<shot>.blend`, with:
  - Single images, fonts, and sounds packed into the `.blend`
  - Image sequences, movies, UDIM tiles, VDB volumes, and caches copied to
    `<stage>/bundle/<path relative to project root>` with paths rewritten to `//../<path>`
    (matching Colab layout: `/content/bundle/blender/<shot>.blend` + `/content/bundle/<path>`)
  - Linked `.blend` libraries rewritten to `//<file>.blend` and recursively prepared into `<stage>/blender/`

Prints `PREPARED ...` on success, or `UNRESOLVED ...` lines for any external asset that could not be made portable.
"""
import bpy
import os
import re
import shutil
import subprocess
import sys

A = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def opt(k, d):
    return A[A.index(k) + 1] if k in A else d


if not A or A[0].startswith("--"):
    raise SystemExit("usage: blender -b <shot.blend> -P prepare_shot.py -- <shot> [--stage DIR] [--root DIR] [--dry]")

SHOT = A[0][:-6] if A[0].endswith(".blend") else A[0]
STAGE = opt("--stage", opt("--drive", os.path.join(os.getcwd(), "_colab_stage"))).replace("\\", "/").rstrip("/")
SRC = bpy.data.filepath.replace("\\", "/")
DEFAULT_ROOT = os.path.dirname(os.path.dirname(SRC)) if "/blender/" in SRC else os.path.dirname(SRC) or os.getcwd()
ROOT = opt("--root", DEFAULT_ROOT).replace("\\", "/").rstrip("/")
DRY = "--dry" in A
BUNDLE = STAGE + "/bundle"
copied, packed, unresolved, notes = [], [], [], []


def absn(p, lib=None):
    return os.path.normpath(bpy.path.abspath(p, library=lib)).replace("\\", "/")


def rel_to_root(ap):
    """Path relative to project root; files outside it go under ext/<parent dir name>/."""
    try:
        r = os.path.relpath(ap, ROOT).replace("\\", "/")
        if not r.startswith(".."):
            return r
    except ValueError:
        pass
    return "ext/" + os.path.basename(os.path.dirname(ap)) + "/" + os.path.basename(ap)


def copy_file(ap, rp):
    dst = BUNDLE + "/" + rp
    if not os.path.isfile(ap):
        unresolved.append(f"missing source {ap}")
        return
    if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(ap) and os.path.getmtime(dst) >= os.path.getmtime(ap):
        return
    if not DRY:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(ap, dst)
    copied.append(rp)


def seq_files(ap):
    """All files of the numbered sequence `ap` belongs to (name<digits>.ext), plus <UDIM>/<UVTILE> tiles."""
    d, b = os.path.split(ap)
    if "<UDIM>" in b or "<UVTILE>" in b:
        pat = re.compile(
            "^"
            + re.escape(b)
            .replace(re.escape("<UDIM>"), r"\d{4}")
            .replace(re.escape("<UVTILE>"), r"u\d+_v\d+")
            + "$"
        )
    else:
        m = re.match(r"^(.*?)(\d+)(\.[^.]+)$", b)
        if not m:
            return [ap]
        pat = re.compile("^" + re.escape(m.group(1)) + r"\d+" + re.escape(m.group(3)) + "$")
    if not os.path.isdir(d):
        return []
    return [d + "/" + f for f in sorted(os.listdir(d)) if pat.match(f)]


def externalise(idb, files=None):
    """Copy the files behind `idb.filepath` into the bundle and point `idb` at `//../<rel>`."""
    ap = absn(idb.filepath, idb.library)
    fl = files if files is not None else [ap]
    if not fl:
        unresolved.append(f"{type(idb).__name__} {idb.name}: no files for {ap}")
        return
    for f in fl:
        copy_file(f, rel_to_root(f))
    idb.filepath = "//../" + rel_to_root(ap)


# ------------------------------------------------------------------ images & media
for img in bpy.data.images:
    if img.library or img.users == 0 or img.source in ("GENERATED", "VIEWER") or img.packed_file:
        continue
    if img.source == "FILE":
        if not os.path.isfile(absn(img.filepath)):
            unresolved.append(f"image {img.name}: missing {absn(img.filepath)}")
            continue
        try:
            img.pack()
            packed.append(img.name)
        except Exception as e:
            notes.append(f"image {img.name} not packable ({e}), copied instead")
            externalise(img)
    elif img.source in ("SEQUENCE", "TILED"):
        externalise(img, seq_files(absn(img.filepath)))
    elif img.source == "MOVIE":
        externalise(img)
    else:
        unresolved.append(f"image {img.name}: source {img.source}")

for clip in bpy.data.movieclips:
    if clip.library:
        continue
    externalise(clip, seq_files(absn(clip.filepath)) if clip.source == "SEQUENCE" else None)

for fnt in bpy.data.fonts:
    if fnt.library or fnt.packed_file or fnt.filepath in ("", "<builtin>"):
        continue
    try:
        fnt.pack()
        packed.append(fnt.name)
    except Exception as e:
        unresolved.append(f"font {fnt.name}: {e}")

for snd in bpy.data.sounds:
    if snd.library or snd.packed_file:
        continue
    try:
        snd.pack()
        packed.append(snd.name)
    except Exception as e:
        unresolved.append(f"sound {snd.name}: {e}")

for vol in getattr(bpy.data, "volumes", []):
    if vol.library or vol.packed_file:
        continue
    externalise(vol, seq_files(absn(vol.filepath)) if vol.is_sequence else None)

for cf in bpy.data.cache_files:
    if cf.library:
        continue
    externalise(cf)

# ------------------------------------------------------------------ linked libraries
blender = bpy.app.binary_path
for lib in bpy.data.libraries:
    ap = absn(lib.filepath)
    name = os.path.basename(ap)
    if "/datafiles/assets/" in ap:  # Blender built-in brush/asset libraries
        notes.append(f"library {name} is a Blender built-in asset, left as is")
        continue
    lib.filepath = "//" + name
    dst = STAGE + "/blender/" + name
    if not os.path.isfile(ap):
        unresolved.append(f"library {name}: missing {ap}")
        continue
    if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(ap):
        notes.append(f"library {name} already staged")
        continue
    if DRY:
        notes.append(f"library {name} would be prepared")
        continue
    r = subprocess.run(
        [blender, "-b", ap, "-P", os.path.abspath(__file__), "--", name, "--stage", STAGE, "--root", ROOT, "--lib"],
        capture_output=True,
        text=True,
    )
    ok = [l for l in r.stdout.splitlines() if l.startswith(("PREPARED", "UNRESOLVED"))]
    print("\n".join("  [lib] " + l for l in ok), flush=True)
    if not any(l.startswith("PREPARED") for l in ok):
        unresolved.append(f"library {name}: prepare failed (rc {r.returncode})")

# ------------------------------------------------------------------ verify no unresolved paths remain
skip = ("<builtin>",)
for p in bpy.utils.blend_paths(absolute=False, packed=False, local=True):
    pn = p.replace("\\", "/")
    if pn in skip or "/datafiles/assets/" in absn(pn):
        continue
    if pn.startswith("//../"):
        if not (os.path.exists(BUNDLE + "/" + pn[5:]) or DRY or seq_files(BUNDLE + "/" + pn[5:])):
            unresolved.append(f"not in bundle: {pn}")
    elif pn.startswith("//") and pn.endswith(".blend") and "/" not in pn[2:]:
        if not (os.path.isfile(STAGE + "/blender/" + pn[2:]) or DRY):
            unresolved.append(f"library not staged: {pn}")
    else:
        unresolved.append(f"external path left: {pn}")

sc = bpy.context.scene
print(
    "SHOT", SHOT,
    "engine", sc.render.engine,
    "res", sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage,
    "frames", sc.frame_start, sc.frame_end,
    "camera", sc.camera and sc.camera.name,
    "fmt", sc.render.image_settings.file_format,
    flush=True,
)
for n in notes:
    print("NOTE", n)
for u in unresolved:
    print("UNRESOLVED", u)

dst = STAGE + "/blender/" + SHOT + ".blend"
if DRY:
    print(f"DRY RUN: would save {dst}; pack {len(packed)}, copy {len(copied)} files")
else:
    os.makedirs(STAGE + "/blender", exist_ok=True)
    bpy.context.preferences.filepaths.save_version = 0
    tmp = dst + ".part.blend"
    bpy.ops.wm.save_as_mainfile(filepath=tmp, copy=True, relative_remap=False, compress=True)
    os.replace(tmp, dst)
    print(
        f"PREPARED {dst} ({os.path.getsize(dst) / 1e6:.1f} MB): packed {len(packed)}, "
        f"copied {len(copied)} new files to {BUNDLE}"
        + (f", {len(unresolved)} UNRESOLVED" if unresolved else ""),
        flush=True,
    )
