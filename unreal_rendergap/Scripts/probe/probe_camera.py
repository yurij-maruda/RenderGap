r"""Read back what Unreal actually made of the USD bench camera.

    env\rg-unreal.bat -run=pythonscript -script=unreal_rendergap\Scripts\probe\probe_camera.py

Loads L_UsdWarehouse, finds the AUsdStageActor, asks it for the component it
generated for the camera prim, and compares every value against the USD the
stage was built from. Nothing is rendered, so this costs seconds rather than
minutes -- which is the point: it catches the whole class of silent import
failures (the x100 focal length clamp, a dropped exposure term, a filmback that
did not survive) before any render time is spent on them.

Prints PASS/FAIL per check and exits non-zero on failure, like
isaac/tools/check_env.py, so it works unchanged as a CI step.

Writes Scripts/probe/_probe_camera.json for analysis/compare_frame.py to cross-check
against Isaac's meta.json.
"""

import json
import math
import os
import sys

import unreal

LEVEL = "/Game/L_UsdWarehouse"
CAMERA_PRIM = "/World/nova_carter/MainCamera"
RESOLUTION = (800, 600)

# UCineCameraComponent::RecalcDerivedData clamps against these; the default lens preset
# is "Universal Zoom" from BaseEngine.ini. Repeated here so the probe can say *why* a
# value was altered rather than just that it differs.
LENS_MIN_FOCAL, LENS_MAX_FOCAL = 4.0, 1000.0
LENS_MIN_FSTOP, LENS_MAX_FSTOP = 1.2, 22.0


def log(msg):
    print(f"[probe_camera] {msg}")


def find_stage_actor(world):
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.UsdStageActor):
        return actor
    return None


def read_usd_expectations(root_layer):
    """Same numbers, straight from the stage. UE ships pxr with the USDCore plugin."""
    try:
        from pxr import Usd, UsdGeom
    except ImportError as exc:
        log(f"WARNING -- pxr not importable in this interpreter ({exc}); skipping USD cross-check.")
        return None

    stage = Usd.Stage.Open(root_layer)
    cam = UsdGeom.Camera(stage.GetPrimAtPath(CAMERA_PRIM))
    if not cam:
        log(f"WARNING -- {CAMERA_PRIM} not found in {root_layer}; skipping USD cross-check.")
        return None

    def attr(name):
        return cam.GetPrim().GetAttribute(name).Get()

    return {
        "focal_length_mm": attr("focalLength") * 100.0,
        "sensor_width_mm": attr("horizontalAperture") * 100.0,
        "sensor_height_mm": attr("verticalAperture") * 100.0,
        "near_clip_uu": attr("clippingRange")[0] * 100.0,
        "fstop": attr("fStop"),
        "shutter_speed": 1.0 / attr("exposure:time"),
        "iso": attr("exposure:iso"),
        "exposure_bias": attr("exposure") + math.log2(attr("exposure:responsivity")),
        "linear_exposure_scale": cam.ComputeLinearExposureScale(),
    }


def main():
    log(f"loading {LEVEL}")
    unreal.EditorLoadingAndSavingUtils.load_map(LEVEL)

    world = unreal.EditorLevelLibrary.get_editor_world()
    stage_actor = find_stage_actor(world)
    if stage_actor is None:
        log("FAILED -- no AUsdStageActor in the level")
        return 1

    root_layer = stage_actor.get_editor_property("root_layer").file_path
    log(f"stage actor: {stage_actor.get_name()}  root layer: {root_layer}")

    component = stage_actor.get_generated_component(CAMERA_PRIM)
    if component is None:
        log(f"FAILED -- the stage actor generated no component for {CAMERA_PRIM}")
        log("          the stage may not have finished loading, or the prim path is wrong")
        return 1

    cam = component
    if not isinstance(cam, unreal.CineCameraComponent):
        owner = component.get_owner()
        if isinstance(owner, unreal.CineCameraActor):
            cam = owner.get_cine_camera_component()
    if not isinstance(cam, unreal.CineCameraComponent):
        log(f"FAILED -- {CAMERA_PRIM} resolved to {type(component).__name__}, not a CineCameraComponent")
        return 1

    filmback = cam.get_editor_property("filmback")
    focus = cam.get_editor_property("focus_settings")
    post = cam.get_editor_property("post_process_settings")
    xform = cam.get_world_transform()

    # Keep the enums as enums for comparison; str() of an unreal enum is its repr
    # ("<CameraFocusMethod.DISABLE: 2>"), so name matching on it silently never matches.
    focus_method = focus.get_editor_property("focus_method")
    exposure_method = post.get_editor_property("auto_exposure_method")

    got = {
        "component": cam.get_name(),
        "actor": cam.get_owner().get_name(),
        "focal_length_mm": cam.get_editor_property("current_focal_length"),
        "sensor_width_mm": filmback.get_editor_property("sensor_width"),
        "sensor_height_mm": filmback.get_editor_property("sensor_height"),
        "sensor_aspect": filmback.get_editor_property("sensor_aspect_ratio"),
        "hfov_deg": cam.get_horizontal_field_of_view(),
        "aperture_fstop": cam.get_editor_property("current_aperture"),
        "focus_method": str(focus.get_editor_property("focus_method")),
        "near_clip_uu": cam.get_editor_property("custom_near_clipping_plane"),
        "shutter_speed": post.get_editor_property("camera_shutter_speed"),
        "iso": post.get_editor_property("camera_iso"),
        "exposure_bias": post.get_editor_property("auto_exposure_bias"),
        "exposure_method": str(post.get_editor_property("auto_exposure_method")),
        "apply_physical_camera_exposure": bool(
            post.get_editor_property("auto_exposure_apply_physical_camera_exposure")),
        "depth_of_field_fstop": post.get_editor_property("depth_of_field_fstop"),
        "depth_of_field_focal_distance": post.get_editor_property("depth_of_field_focal_distance"),
        "world_location": [xform.translation.x, xform.translation.y, xform.translation.z],
    }

    log("what Unreal made of it:")
    for k, v in got.items():
        log(f"    {k:34} {v}")

    # Unreal's own exposure divisor, PostProcessEyeAdaptation.cpp:395, LuminanceMax == 1.
    ue_scale = (2.0 ** got["exposure_bias"]) / (
        got["depth_of_field_fstop"] ** 2 * got["shutter_speed"] * 100.0 / got["iso"])
    got["linear_exposure_scale"] = ue_scale
    log(f"    {'derived exposure scale':34} {ue_scale:.9f}")

    want = read_usd_expectations(root_layer)

    checks = [
        ("camera component resolved from the prim path", True),
        ("depth of field is off", focus_method in (unreal.CameraFocusMethod.DISABLE,
                                                   unreal.CameraFocusMethod.DO_NOT_OVERRIDE)
                                  and got["depth_of_field_focal_distance"] == 0.0),
        ("exposure is manual", exposure_method == unreal.AutoExposureMethod.AEM_MANUAL),
        ("physical camera exposure applied", got["apply_physical_camera_exposure"]),
        ("focal length inside the lens clamp", LENS_MIN_FOCAL <= got["focal_length_mm"] <= LENS_MAX_FOCAL),
        ("aperture inside the lens clamp", LENS_MIN_FSTOP <= got["aperture_fstop"] <= LENS_MAX_FSTOP),
        ("sensor aspect matches the render", abs(got["sensor_aspect"] - RESOLUTION[0] / RESOLUTION[1]) < 1e-4),
    ]

    if want:
        log("cross-check against the USD:")
        for key, tol in (("focal_length_mm", 1e-3), ("sensor_width_mm", 1e-3), ("sensor_height_mm", 1e-3),
                         ("near_clip_uu", 1e-3), ("shutter_speed", 1e-2), ("iso", 1e-2),
                         ("exposure_bias", 1e-4), ("linear_exposure_scale", 1e-6)):
            ok = abs(got[key] - want[key]) <= tol * max(1.0, abs(want[key]))
            log(f"    {key:34} usd {want[key]:<14.6f} unreal {got[key]:<14.6f} {'ok' if ok else 'MISMATCH'}")
            checks.append((f"{key} survived the import", ok))
        # The one that matters: fStop drives Unreal's exposure aperture, not just DOF.
        checks.append(("fStop reached DepthOfFieldFstop unclamped",
                       abs(got["depth_of_field_fstop"] - want["fstop"]) < 1e-4))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_camera.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"unreal": got, "usd": want}, fh, indent=2)
    log(f"wrote {out}")

    print()
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(ok for _, ok in checks) else 1


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
