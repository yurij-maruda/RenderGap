r"""Compare the textures Unreal imported against the PNGs Isaac samples directly.

    env\rg-unreal.bat -run=pythonscript -script="unreal_rendergap\Scripts\probe\probe_textures.py"

probe_materials.py established that the material GRAPH transfers intact --
normal, roughness, metallic and AO are all wired in Unreal exactly as the
UsdPreviewSurface specifies, and nothing is dropped. So the residual render
difference has to come from the texture data itself, not from how it is plumbed.

Two mechanisms would produce what was measured, and this separates them:

  * Block compression. Unreal imports PNG and compresses to BC1/BC5/BC7; Isaac
    samples the PNG. BC works on 4x4 blocks, which is why the detail deficit
    GROWS towards fine scales (1.16x at 17 px, 1.47x at 3 px) instead of being a
    flat blur. Check: CompressionSettings, and whether the imported size matches
    the source.

  * An sRGB flag on a data texture. ORM (occlusion/roughness/metallic) and normal
    maps MUST be linear. If Unreal marks one sRGB, every roughness value it reads
    is wrong, and the surface renders at the wrong gloss -- which is the shape of
    the floor result, where Unreal shows 15-30% MORE high-frequency content
    (sharper reflections) than Isaac.

Exits non-zero if a data texture (ORM / normal) is flagged sRGB, or if an
imported texture is smaller than its source PNG.
"""

import json
import os
import sys

import unreal

LEVEL = "/Game/L_UsdWarehouse"
DEFAULT_FILTERS = ("floor", "crateplastic", "cardbox", "walla", "palette")

# Suffix -> what the texture is. Isaac's source names end _D / _N / _ORM.
DATA_SUFFIXES = ("_N", "_ORM", "_M")     # must be linear
COLOR_SUFFIXES = ("_D",)                 # must be sRGB


def log(msg):
    print(f"[probe_textures] {msg}")


def source_png_size(path):
    """Read a PNG's IHDR without pulling in an image library."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(33)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))
    except Exception:
        return None


def classify(name):
    stem = name.rstrip("0123456789")
    for suf in DATA_SUFFIXES:
        if stem.endswith(suf):
            return "data"
    for suf in COLOR_SUFFIXES:
        if stem.endswith(suf):
            return "color"
    return "unknown"


def main():
    filters = tuple(a.lower() for a in sys.argv[1:]) or DEFAULT_FILTERS

    unreal.EditorLoadingAndSavingUtils.load_map(LEVEL)
    world = unreal.EditorLevelLibrary.get_editor_world()
    actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.UsdStageActor)
    if not actors:
        log("FAILED -- no AUsdStageActor in the level")
        return 1
    stage_actor = actors[0]
    root_layer = stage_actor.get_editor_property("root_layer").file_path

    from pxr import Usd, UsdShade

    stage = Usd.Stage.Open(root_layer)
    stage_dir = os.path.dirname(root_layer)

    # USD side: texture prim -> resolved source file, for the materials of interest
    sources = {}
    for prim in stage.Traverse():
        mat = UsdShade.Material(prim)
        if not mat or not any(f in prim.GetName().lower() for f in filters):
            continue
        surf = mat.GetSurfaceOutput()
        if not surf or not surf.HasConnectedSource():
            continue
        shader = UsdShade.Shader(surf.GetConnectedSource()[0].GetPrim())
        for inp in shader.GetInputs():
            if not inp.HasConnectedSource():
                continue
            tex = UsdShade.Shader(inp.GetConnectedSource()[0].GetPrim())
            f = tex.GetInput("file")
            if not f:
                continue
            asset = f.Get()
            if asset is None:
                continue
            resolved = getattr(asset, "resolvedPath", "") or str(asset)
            if not os.path.isabs(resolved):
                resolved = os.path.normpath(os.path.join(stage_dir, str(asset).strip("@")))
            sources[os.path.splitext(os.path.basename(resolved))[0]] = resolved

    log(f"{len(sources)} distinct source textures referenced by {filters}")

    # Unreal side
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    assets = reg.get_assets_by_path("/Game/UsdAssets", recursive=True)
    ue_by_stem = {}
    for a in assets:
        obj = a.get_asset()
        if isinstance(obj, unreal.Texture2D):
            ue_by_stem[obj.get_name().rstrip("0123456789")] = obj

    report, failures = [], []
    print()
    log(f"{'texture':28} {'kind':6} {'source':>11} {'unreal':>11} {'sRGB':>5}  compression")
    for stem, src in sorted(sources.items()):
        kind = classify(stem)
        ue = ue_by_stem.get(stem) or ue_by_stem.get(stem.rstrip("0123456789"))
        ssize = source_png_size(src)
        if ue is None:
            log(f"{stem:28} {kind:6} {str(ssize):>11} {'MISSING':>11}")
            failures.append((stem, "no Unreal texture"))
            continue
        try:
            usize = (ue.blueprint_get_size_x(), ue.blueprint_get_size_y())
        except Exception:
            usize = (getattr(ue, "get_editor_property", lambda *_: None)("imported_size") or "?",)
        srgb = bool(ue.get_editor_property("srgb"))
        comp = str(ue.get_editor_property("compression_settings")).split(".")[-1]

        log(f"{stem:28} {kind:6} {str(ssize):>11} {str(usize):>11} {str(srgb):>5}  {comp}")

        if kind == "data" and srgb:
            failures.append((stem, "data texture flagged sRGB -- roughness/normal will be wrong"))
        if kind == "color" and not srgb:
            failures.append((stem, "colour texture NOT flagged sRGB"))
        if ssize and isinstance(usize, tuple) and len(usize) == 2 and usize[0] and usize[0] < ssize[0]:
            failures.append((stem, f"downsized on import {ssize} -> {usize}"))

        report.append({"texture": stem, "kind": kind, "source_size": ssize,
                       "unreal_size": list(usize) if isinstance(usize, tuple) else None,
                       "srgb": srgb, "compression": comp, "source": src})

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_textures.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print()
    log(f"wrote {out}")
    print()
    if failures:
        for n, why in failures:
            print(f"  [FAIL] {n}: {why}")
    else:
        print("  [PASS] colour textures sRGB, data textures linear, none downsized")
        print("         -> remaining detail difference is block compression, not import settings")
    return 1 if failures else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except BaseException:
        import traceback
        traceback.print_exc()
    sys.stdout.flush()
    log(f"exit {code}")
    if code != 0:
        raise SystemExit(code)
