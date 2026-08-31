r"""What did the rect lights actually become in Unreal, and what does that predict?

    env\rg-unreal.bat -run=pythonscript -script="unreal_rendergap\Scripts\probe\probe_lights.py"

analysis/compare_frame.py measures a brightness ratio between the two engines.
This says how much of it the lights are responsible for, analytically, so the
residual can be attributed rather than guessed. docs/usd_transfer_losses.md's
rule is that an entry is added only after it is traced to a line of engine
source or an authored attribute -- this is the tracing step for the lights.

The chain, for a WxH rect light with inputs:intensity I and normalize = false:

  USD           I nits over Area = W*H m^2
  importer      Intensity = I * 2^exposure * PI * Area          [lumens]
                (UsdToUnreal::ConvertRectLightIntensityAttr, USDLightConversion.cpp:321)
  RenderGapUSD  Intensity /= PI * Area^2
                (FRenderGapLuxLightTranslator::UpdateComponents)
  renderer      lumens  -> LightBrightness *= 100*100/PI
                nits    -> LightBrightness *= AreaInCm2
                (URectLightComponent::ComputeLightBrightness, RectLightComponent.cpp:183)

Isaac renders the same prim as if it were area-normalised, i.e. I/Area nits,
which is what usd_transfer_losses.md section 1 established empirically. So the
predicted Isaac/Unreal ratio is the ratio of those two LightBrightness values.
"""

import json
import math
import os
import sys

import unreal

LEVEL = "/Game/L_UsdWarehouse"
UE_PI = math.pi


def log(msg):
    print(f"[probe_lights] {msg}")


def main():
    unreal.EditorLoadingAndSavingUtils.load_map(LEVEL)
    world = unreal.EditorLevelLibrary.get_editor_world()

    stage_actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.UsdStageActor)
    if not stage_actors:
        log("FAILED -- no AUsdStageActor in the level")
        return 1
    stage_actor = stage_actors[0]
    root_layer = stage_actor.get_editor_property("root_layer").file_path

    try:
        from pxr import Usd, UsdLux
        stage = Usd.Stage.Open(root_layer)
    except ImportError:
        log("FAILED -- pxr not importable; cannot cross-check against the USD")
        return 1

    rows = []
    for prim in stage.Traverse():
        rect = UsdLux.RectLight(prim)
        if not rect or not prim.IsActive():
            continue
        vis = prim.GetAttribute("visibility")
        if vis and vis.Get() == "invisible":
            continue

        prim_path = str(prim.GetPath())
        component = stage_actor.get_generated_component(prim_path)
        if component is None:
            continue

        intensity = rect.GetIntensityAttr().Get() or 0.0
        exposure = rect.GetExposureAttr().Get() or 0.0
        width = rect.GetWidthAttr().Get() or 0.0
        height = rect.GetHeightAttr().Get() or 0.0
        normalize = bool(rect.GetNormalizeAttr().Get())
        area_m2 = width * height
        if area_m2 <= 0:
            continue

        ue_intensity = component.get_editor_property("intensity")
        units = component.get_editor_property("intensity_units")
        source_w = component.get_editor_property("source_width")
        source_h = component.get_editor_property("source_height")
        attenuation = component.get_editor_property("attenuation_radius")
        area_cm2 = source_w * source_h

        # What the renderer will actually shade with, per RectLightComponent.cpp:183.
        # Compare the enum itself: str() of an unreal enum is its repr, so name matching
        # on it silently falls through to the wrong branch.
        if units == unreal.LightUnits.LUMENS:
            ue_brightness = ue_intensity * (100.0 * 100.0 / UE_PI)
        elif units == unreal.LightUnits.CANDELAS:
            ue_brightness = ue_intensity * (100.0 * 100.0)
        else:  # EV / Unitless -> nits path
            ue_brightness = ue_intensity * area_cm2

        # NOTE: this used to model Isaac as area-normalising (I/Area nits). That model
        # predicted a 1.00x match and measured 22x too dark, so it is wrong -- see
        # docs/usd_transfer_losses.md section 1. What is reported below is the ratio the
        # CURRENT translator divisor produces, against the empirically established target
        # of ~1.0. It is a check on the fitted constant, not a derivation of it.
        isaac_nits = intensity * (2.0 ** exposure) / area_m2
        isaac_brightness = isaac_nits * area_cm2

        rows.append({
            "prim": prim_path,
            "usd_intensity": intensity,
            "usd_normalize": normalize,
            "usd_size_m": [width, height],
            "area_m2": area_m2,
            "unreal_intensity": ue_intensity,
            "unreal_units": str(units),
            "unreal_source_cm": [source_w, source_h],
            "attenuation_radius_cm": attenuation,
            "unreal_brightness": ue_brightness,
            "isaac_equivalent_brightness": isaac_brightness,
            "predicted_isaac_over_unreal": isaac_brightness / ue_brightness if ue_brightness else None,
        })

    if not rows:
        log("FAILED -- no active rect lights resolved through the stage actor")
        return 1

    for r in rows:
        log(f"{r['prim']}")
        log(f"    USD     intensity {r['usd_intensity']:g} nits, {r['usd_size_m'][0]:g}x"
            f"{r['usd_size_m'][1]:g} m = {r['area_m2']:g} m2, normalize={r['usd_normalize']}")
        log(f"    Unreal  Intensity {r['unreal_intensity']:.6g} {r['unreal_units'].split('.')[-1]}, "
            f"source {r['unreal_source_cm'][0]:g}x{r['unreal_source_cm'][1]:g} cm, "
            f"attenuation {r['attenuation_radius_cm']:g} cm")
        log(f"    shading brightness   unreal {r['unreal_brightness']:.6g}   "
            f"isaac-equivalent {r['isaac_equivalent_brightness']:.6g}")
        log(f"    => predicted isaac/unreal  {r['predicted_isaac_over_unreal']:.2f}x")

        area = r["area_m2"]
        log(f"    reference factors: PI*Area = {UE_PI * area:.2f}   "
            f"PI*Area^2 = {UE_PI * area * area:.2f}   Area = {area:.2f}")

    ratios = [r["predicted_isaac_over_unreal"] for r in rows if r["predicted_isaac_over_unreal"]]
    log("")
    log(f"predicted isaac/unreal from lights alone: "
        f"{min(ratios):.2f}x .. {max(ratios):.2f}x  across {len(ratios)} light(s)")
    log("The number above uses the DISPROVEN area-normalised model and is kept only as a")
    log("reference point. The real check is analysis/compare_frame.py: with the fitted")
    log("divisor the rendered median ratio is ~1.06x, not the ~1.00x this model claims.")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_lights.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)
    log(f"wrote {out}")
    return 0


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
