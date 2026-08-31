r"""Compare what the USD materials say against what Unreal actually built.

    env\rg-unreal.bat -run=pythonscript -script=unreal\probe_materials.py

With camera, exposure and light intensity matched, the residual difference between
the two engines is material. Three effects were measured on the rendered pair
(see docs/usd_transfer_losses.md) and this probe exists to attribute them:

  * Isaac carries 1.4x more fine detail. The ratio RISES as the spatial scale
    gets finer (1.16x at 17 px, 1.47x at 3 px), which is lost high-frequency
    texture rather than a uniform blur -- the signature of a missing normal map.

  * Unreal's floor shows more reflection (0.76-0.86x, i.e. Unreal has MORE
    high-frequency content there) -- the signature of a lower roughness.

  * The purple crates sit ~0.87 px off while every other surface in frame agrees
    to within 0.17 px. The crates are the only translucent material present, so
    blend mode and opacity are the things to look at.

For each material it prints the UsdPreviewSurface inputs beside the parameters of
the UMaterialInstance the USD importer generated, so a dropped input is visible
rather than inferred. Materials are de-duplicated by name -- the warehouse has
255 instances of a handful of distinct materials.

Exits non-zero if a material has a normal map in USD but none in Unreal.
"""

import json
import os
import sys

import unreal

LEVEL = "/Game/L_UsdWarehouse"
DEFAULT_FILTERS = ("floor", "crateplastic", "cardbox", "walla", "palette")

# UsdPreviewSurface inputs worth comparing.
USD_INPUTS = ("diffuseColor", "roughness", "metallic", "normal", "opacity",
              "specularColor", "ior", "occlusion")


def log(msg):
    print(f"[probe_materials] {msg}")


def usd_material_summary(prim):
    """UsdPreviewSurface inputs: a texture path, a constant, or absent."""
    from pxr import UsdShade

    mat = UsdShade.Material(prim)
    surf = mat.GetSurfaceOutput()
    if not surf or not surf.HasConnectedSource():
        return None
    shader = UsdShade.Shader(surf.GetConnectedSource()[0].GetPrim())

    out = {}
    for name in USD_INPUTS:
        inp = shader.GetInput(name)
        if not inp:
            continue
        if inp.HasConnectedSource():
            tex = UsdShade.Shader(inp.GetConnectedSource()[0].GetPrim())
            f = tex.GetInput("file")
            path = f.Get() if f else None
            out[name] = f"tex:{os.path.basename(str(path))}" if path else "tex:?"
        else:
            v = inp.Get()
            out[name] = f"const:{v}"
    return out


def unreal_material_summary(asset):
    """Texture / scalar / vector parameters and the base property overrides."""
    out = {"asset": asset.get_name(), "class": type(asset).__name__,
           "textures": {}, "scalars": {}, "vectors": {}, "overrides": {}}

    def params(prop):
        try:
            return asset.get_editor_property(prop) or []
        except Exception:
            return []

    for tp in params("texture_parameter_values"):
        try:
            name = str(tp.get_editor_property("parameter_info").get_editor_property("name"))
            tex = tp.get_editor_property("parameter_value")
            out["textures"][name] = tex.get_name() if tex else None
        except Exception:
            continue
    for sp in params("scalar_parameter_values"):
        try:
            name = str(sp.get_editor_property("parameter_info").get_editor_property("name"))
            out["scalars"][name] = float(sp.get_editor_property("parameter_value"))
        except Exception:
            continue
    for vp in params("vector_parameter_values"):
        try:
            name = str(vp.get_editor_property("parameter_info").get_editor_property("name"))
            c = vp.get_editor_property("parameter_value")
            out["vectors"][name] = [round(c.r, 4), round(c.g, 4), round(c.b, 4)]
        except Exception:
            continue
    try:
        ov = asset.get_editor_property("base_property_overrides")
        for f in ("blend_mode", "two_sided", "opacity_mask_clip_value",
                  "override_blend_mode", "override_two_sided"):
            try:
                out["overrides"][f] = str(ov.get_editor_property(f))
            except Exception:
                pass
    except Exception:
        pass
    return out


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

    try:
        from pxr import Usd, UsdShade
    except ImportError as exc:
        log(f"FAILED -- pxr not importable ({exc})")
        return 1

    stage = Usd.Stage.Open(root_layer)

    seen = {}
    for prim in stage.Traverse():
        if not UsdShade.Material(prim):
            continue
        name = prim.GetName()
        if name in seen:
            continue
        if not any(f in name.lower() for f in filters):
            continue
        seen[name] = prim

    if not seen:
        log(f"no materials matched {filters}")
        return 1
    log(f"{len(seen)} distinct materials matching {filters}")

    report, failures = [], []
    for name, prim in sorted(seen.items()):
        path = str(prim.GetPath())
        usd = usd_material_summary(prim)
        assets = stage_actor.get_generated_assets(path) or []
        mats = [a for a in assets if isinstance(a, unreal.MaterialInterface)]

        print()
        log(f"{name}   ({path})")
        if usd is None:
            log("   USD    : no UsdPreviewSurface surface output (MDL only?)")
        else:
            for k in USD_INPUTS:
                if k in usd:
                    log(f"   USD    {k:14} {usd[k]}")
        if not mats:
            log("   Unreal : NO material asset generated for this prim")
            failures.append((name, "no UE material"))
            report.append({"material": name, "usd": usd, "unreal": None})
            continue

        ue = unreal_material_summary(mats[0])
        log(f"   Unreal {'asset':14} {ue['asset']}  ({ue['class']})")
        for k, v in sorted(ue["textures"].items()):
            log(f"   Unreal tex {k:12} {v}")
        for k, v in sorted(ue["scalars"].items()):
            log(f"   Unreal val {k:12} {v}")
        for k, v in sorted(ue["vectors"].items()):
            log(f"   Unreal vec {k:12} {v}")
        for k, v in ue["overrides"].items():
            log(f"   Unreal ovr {k:12} {v}")

        # the detail question: a normal map in USD must survive into Unreal
        if usd and "normal" in usd and usd["normal"].startswith("tex:"):
            has_normal = any("normal" in k.lower() for k in ue["textures"]) or \
                         any(v and "_N" in str(v) for v in ue["textures"].values())
            log(f"   => normal map in USD, in Unreal: {'YES' if has_normal else 'NO'}")
            if not has_normal:
                failures.append((name, "normal map dropped"))
        report.append({"material": name, "usd": usd, "unreal": ue})

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_materials.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print()
    log(f"wrote {out}")

    print()
    if failures:
        for n, why in failures:
            print(f"  [FAIL] {n}: {why}")
    else:
        print("  [PASS] every material with a USD normal map has one in Unreal")
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
