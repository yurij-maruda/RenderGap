r"""Pass 2A, miniature -- one converged frame from the bench camera, headless.

    env\rg-python.bat isaac\render_frame.py --condition pt --spp 512

Opens the real stage (data/warehouse_payload/root_warehouse.usda), points a
Replicator render product at the USD bench camera and writes one 800x600 frame.
Structurally this is isaac/test/hello_warehouse.py's capture block; the
differences are what make it a measurement rather than a smoke test:

  * The camera comes from the stage. Nothing about the view is defined here, so
    Unreal and Isaac cannot drift apart in this file.

  * Every render setting is pinned in carb, never inherited. The stage's own
    customLayerData renderSettings and its /Render scope bind to Kit's *viewport*
    product, not to ours -- so this capture deliberately does not match what the
    GUI shows, and chasing that difference is a waste of a day.

  * The denoiser is off. A denoised frame is a post-process difference wearing
    light transport's clothes, and spec section 6.2 asks for converged, not clean.

  * It writes linear HDR alongside the PNG. Once the two engines are exposure
    matched, every number in analysis/compare_frame.py comes from the linear
    pair; an 8-bit PNG that clips cannot tell you how much it clipped by.

Output (--out, default data/bench/isaac):
    rgb_0000.png    tonemapped, for looking at
    hdr.npy         float32 linear HxWx3, for measuring
    meta.json       resolution, camera, render mode, spp, resolved intrinsics
                    and the USD linear exposure scale
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

DEFAULT_STAGE = os.path.join("data", "warehouse_payload", "root_warehouse.usda")
DEFAULT_CAMERA = "/World/nova_carter/MainCamera"
DEFAULT_OUT = os.path.join("results", "isaac", "init_frame")


def parse_args(argv=None):
    # Parsed before SimulationApp so --help costs nothing and a typo fails in a
    # second rather than after a minute of Kit startup.
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--condition", choices=["pt", "rt"], default="pt",
                   help="pt = RTX Path Tracing (spec condition 1, the control). rt = real-time, for fast iteration.")
    p.add_argument("--spp", type=int, default=512, help="total samples per pixel for --condition pt")
    p.add_argument("--subframes", type=int, default=0,
                   help="render iterations before the frame is read back. 0 = derived from "
                        "--spp / --spp-per-subframe for pt, 32 for rt.")
    p.add_argument("--spp-per-subframe", type=int, default=16,
                   help="samples the path tracer accumulates per render iteration. --spp is the "
                        "TOTAL; this only controls how many iterations it takes to get there. "
                        "At 1 (the old behaviour) 2048 spp needs 2048 iterations and takes "
                        "minutes; at 16 it needs 128.")
    p.add_argument("--resolution", type=int, nargs=2, metavar=("W", "H"), default=(800, 600))
    p.add_argument("--camera", default=DEFAULT_CAMERA)
    p.add_argument("--stage", default=os.path.join(REPO, DEFAULT_STAGE))
    p.add_argument("--out", default=os.path.join(REPO, DEFAULT_OUT))
    p.add_argument("--firefly-filter", dest="firefly_filter", action="store_true", default=True,
                   help="Kit's path-tracer firefly clamp (default on). Unreal's path tracer "
                        "has its own equivalent in r.PathTracing.MaxPathIntensity, so leaving "
                        "Isaac unclamped is an ASYMMETRY, not neutrality.")
    p.add_argument("--no-firefly-filter", dest="firefly_filter", action="store_false")
    p.add_argument("--firefly-clamp", type=float, default=None,
                   help="max unexposed intensity per sample. Default: leave Kit's own value.")
    p.add_argument("--exposure-time", type=float, default=None,
                   help="override the camera's exposure:time on the session layer only "
                        "(never written to disk). This exists for the Kit-honours-exposure gate: "
                        "render at 1/60 and 1/120 and the linear median must halve.")
    return p.parse_args(argv)


args = parse_args()

# Replicator's DiskBackend resolves a RELATIVE output_dir against
# /omni/replicator/backends/disk/root_dir (~/omni.replicator_out), not the working
# directory -- so a relative --out silently splits the PNG away from hdr.npy/meta.json,
# which land next to the repo. Absolute from here on.
args.out = os.path.abspath(args.out)
args.stage = os.path.abspath(args.stage)

from isaacsim import SimulationApp  # noqa: E402

simulation_app = SimulationApp({"headless": True})

import carb  # noqa: E402
import numpy as np  # noqa: E402
import omni.replicator.core as rep  # noqa: E402
import omni.usd  # noqa: E402
from pxr import UsdGeom  # noqa: E402

_settings = carb.settings.get_settings()


def pin(key, value):
    """Set a carb setting and report what actually took, so a wrong key is visible.

    Kit silently accepts writes to paths nothing reads. Printing the read-back is
    the cheapest defence against a render condition that was never applied.
    """
    _settings.set(key, value)
    got = _settings.get(key)
    flag = "ok " if got == value else "!! "
    print(f"  [{flag}] {key:58} = {got!r}" + ("" if got == value else f"   (asked {value!r})"))
    return got == value


def main() -> int:
    ctx = omni.usd.get_context()
    print(f"[render_frame] stage : {args.stage}")
    # Isaac Sim 6.0 returns a bare bool here; older Kit returned (bool, error).
    result = ctx.open_stage(args.stage)
    ok = result[0] if isinstance(result, tuple) else result
    if not ok:
        print(f"[render_frame] FAILED -- could not open stage: {result}")
        return 1
    stage = ctx.get_stage()

    cam_prim = stage.GetPrimAtPath(args.camera)
    if not cam_prim or not cam_prim.IsValid():
        print(f"[render_frame] FAILED -- no prim at {args.camera}")
        return 1
    cam = UsdGeom.Camera(cam_prim)
    if not cam:
        print(f"[render_frame] FAILED -- {args.camera} is not a UsdGeomCamera")
        return 1

    # Gate support: a session-layer opinion, so the layer on disk is never touched.
    if args.exposure_time is not None:
        with Usd_session_layer(stage):
            cam.GetExposureTimeAttr().Set(args.exposure_time)
        print(f"[render_frame] exposure:time overridden on the session layer -> {args.exposure_time}")

    focal = cam.GetFocalLengthAttr().Get()
    h_ap = cam.GetHorizontalApertureAttr().Get()
    v_ap = cam.GetVerticalApertureAttr().Get()
    hfov = 2.0 * math.degrees(math.atan(h_ap / (2.0 * focal)))
    vfov = 2.0 * math.degrees(math.atan(v_ap / (2.0 * focal)))
    exposure_scale = cam.ComputeLinearExposureScale()

    print(f"[render_frame] camera: {args.camera}")
    print(f"               focal {focal} aperture {h_ap} x {v_ap}  ->  hFOV {hfov:.4f} vFOV {vfov:.4f}")
    print(f"               linear exposure scale {exposure_scale:.9f}")

    # --- render settings, all pinned ----------------------------------------
    print(f"[render_frame] pinning render settings ({args.condition}):")
    path_traced = args.condition == "pt"
    # Kit 110 renamed the real-time mode: "RaytracedLighting" is silently ignored and the
    # setting stays on whatever it was. pin() prints the read-back, which is how that was
    # caught -- do not replace it with a bare settings.set().
    pin("/rtx/rendermode", "PathTracing" if path_traced else "RealTimePathTracing")

    if path_traced:
        pin("/rtx/pathtracing/spp", max(1, args.spp_per_subframe))
        pin("/rtx/pathtracing/totalSpp", args.spp)
        pin("/rtx/pathtracing/adaptiveSampling/enabled", False)
        pin("/rtx/pathtracing/optixDenoiser/enabled", 0)

        # Firefly clamping is ON by default, which is a change of position worth stating.
        # It was originally off on the grounds that filtering is a post-process difference
        # -- but Unreal's path tracer clamps by default too
        # (FPostProcessSettings::PathTracingMaxPathIntensity = 24, Scene.cpp:664), so an
        # unclamped Isaac is the asymmetric case, not the neutral one. Measured at 512 spp,
        # unclamped Isaac had 472 px above 2 cd/m2 against Unreal's 38.
        pin("/rtx/pathtracing/fireflyFilter/enabled", bool(args.firefly_filter))
        if args.firefly_filter and args.firefly_clamp is not None:
            for key in ("/rtx/pathtracing/fireflyFilter/maxUnexposedIntensityPerSample",
                        "/rtx/pathtracing/fireflyFilter/maxUnexposedIntensityPerSampleDiffuse",
                        "/rtx/pathtracing/fireflyFilter/maxPerEmissiveUnexposedIntensity"):
                pin(key, args.firefly_clamp)

    # Denoisers, upscalers and temporal AA are all between-render differences that have
    # nothing to do with light transport.
    #
    # aa/op is the one that matters: 0 = off, 3 = DLSS. The Isaac GUI ships with op 3 and
    # `rtx.post.dlss.execMode = 0` (isaacsim.exp.base.kit:153), and execMode 0 is
    # *Performance* -- half linear resolution, upscaled. So the viewport is softer than
    # this capture even before anything else. Do not read execMode 0 as "DLSS off".
    #
    # It is not, however, the reason the viewport was badly out of focus: that was the
    # camera focusing at zero distance under the real-time renderer. Section 6 of
    # docs/usd_transfer_losses.md.
    pin("/rtx/post/aa/op", 0)
    pin("/rtx/indirectDiffuse/denoiser/enabled", False)
    pin("/rtx/reflections/denoiser/enabled", False)

    # Auto-exposure off: the physical exposure model on the camera prim is the only
    # thing allowed to set brightness, because it is the only part Unreal can read.
    pin("/rtx/post/histogram/enabled", False)

    # Post effects the spec's controlled-variable list requires off (section 3.1).
    #
    # Note on /rtx/post/dof/enabled: pinning it False is belt-and-braces, NOT what keeps
    # this capture sharp. It already reads False by default and the frame is blurred
    # anyway if the camera focuses at zero distance -- Kit's depth of field reads the
    # prim's own fStop/focusDistance. The real fix is focusDistance = 10 in
    # camera_settings.usda. See docs/usd_transfer_losses.md section 6.
    for key in ("/rtx/post/dof/enabled",
                "/rtx/post/motionblur/enabled",
                "/rtx/post/lensFlares/enabled",
                "/rtx/post/chromaticAberration/enabled",
                "/rtx/post/tvNoise/enabled",
                "/rtx/post/backgroundZeroAlpha/enabled"):
        pin(key, False)

    # --- capture -------------------------------------------------------------
    rep.orchestrator.set_capture_on_play(False)
    os.makedirs(args.out, exist_ok=True)

    backend = rep.backends.get("DiskBackend")
    backend.initialize(output_dir=args.out)
    writer = rep.writers.get("BasicWriter")
    writer.initialize(backend=backend, rgb=True)

    resolution = tuple(args.resolution)
    rp = rep.create.render_product(args.camera, resolution, name="bench", force_new=True)
    writer.attach([rp])

    hdr = attach_hdr_annotator(rp)

    # Let the stage -- payloads, materials, BVH -- finish before asking for a frame.
    for _ in range(120):
        simulation_app.update()

    if args.subframes:
        subframes = args.subframes
    elif path_traced:
        # +1 so the accumulator is definitely at totalSpp when the frame is read back.
        subframes = -(-args.spp // max(1, args.spp_per_subframe)) + 1
    else:
        subframes = 32
    print(f"[render_frame] capturing {resolution[0]}x{resolution[1]}, {subframes} subframes ...")
    rep.orchestrator.step(delta_time=0.0, rt_subframes=subframes)
    rep.orchestrator.wait_until_complete()

    hdr_stats = write_hdr(hdr, args.out)
    write_srgb_png(args.out)

    writer.detach()
    rp.destroy()

    meta = {
        "engine": "isaac-sim",
        "stage": args.stage,
        "camera": args.camera,
        "resolution": list(resolution),
        "condition": args.condition,
        "spp": args.spp if path_traced else None,
        "subframes": subframes,
        "focal_length": focal,
        "horizontal_aperture": h_ap,
        "vertical_aperture": v_ap,
        "hfov_deg": hfov,
        "vfov_deg": vfov,
        "linear_exposure_scale": exposure_scale,
        "exposure_time_override": args.exposure_time,
        "hdr": hdr_stats,
    }
    with open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    # --- verify --------------------------------------------------------------
    pngs = sorted(f for f in os.listdir(args.out) if f.endswith(".png"))
    checks = [("a PNG was written", bool(pngs))]
    if pngs:
        size = os.path.getsize(os.path.join(args.out, pngs[-1]))
        print(f"[render_frame] wrote {os.path.join(args.out, pngs[-1])} ({size} bytes)")
        # A uniformly flat 800x600 PNG compresses to roughly 2 kB.
        checks.append(("frame is not blank", size > 20_000))
    checks.append(("linear HDR was captured", hdr_stats is not None))

    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if any(not ok for _, ok in checks):
        return 1

    print(f"[render_frame] OK -- {args.out}")
    return 0


class Usd_session_layer:
    """Edit on the session layer, so --exposure-time never reaches the file on disk."""

    def __init__(self, stage):
        self._stage = stage

    def __enter__(self):
        self._prev = self._stage.GetEditTarget()
        self._stage.SetEditTarget(self._stage.GetSessionLayer())
        return self

    def __exit__(self, *exc):
        self._stage.SetEditTarget(self._prev)
        return False


def attach_hdr_annotator(render_product):
    """Linear scene colour, if this Isaac build exposes it.

    Named differently across Replicator versions, and not guaranteed to exist at
    all -- so try, report, and let the PNG carry the run if it is missing rather
    than failing the render.
    """
    for getter in (lambda n: rep.annotators.get(n), lambda n: rep.AnnotatorRegistry.get_annotator(n)):
        for name in ("HdrColor", "LdrColorHdr", "hdr_color"):
            try:
                annot = getter(name)
                annot.attach([render_product])
                print(f"[render_frame] linear annotator: {name}")
                return annot
            except Exception:
                continue
    print("[render_frame] WARNING -- no HDR annotator on this build; PNG only.")
    print("               analysis/compare_frame.py will be limited to 8-bit, and any")
    print("               clipped frame becomes unmeasurable. See docs/usd_transfer_losses.md.")
    return None


def write_srgb_png(out_dir):
    """Write rgb_srgb.png: the linear frame through linear -> sRGB, nothing else.

    Kit's own rgb_0000.png is LdrColor, which carries its tonemapper's separate camera
    exposure (fNumber 5.0, cameraShutter 50, exposureTime 0.02, responsivity 1.103, plus
    whatever the stage's customLayerData overrides). That is a second exposure on top of
    the UsdGeomCamera one and it made the PNG ~3x darker in 8-bit than Unreal's for
    identical linear data -- 8-bit median 35 against 101.

    Those settings are NOT reachable from carb on the Replicator path: pinning
    /rtx/post/tonemap/{op,fNumber,cameraShutter,exposureTime,responsivity,filmIso,
    whiteScale,exposureKey,enableSrgbToGamma} all read back correctly and changed the
    output not at all. So rather than fight it, this writes the comparable PNG directly.

    Unreal's MRQ PNG is linear with the tone curve disabled, i.e. exactly this transform,
    so rgb_srgb.png and Unreal's frame.0000.png are directly comparable by eye.
    rgb_0000.png is kept as the engine-native artifact.
    """
    npy = os.path.join(out_dir, "hdr.npy")
    if not os.path.exists(npy):
        return None
    lin = np.load(npy)[:, :, :3].astype(np.float32)
    x = np.clip(lin, 0.0, 1.0)
    srgb = np.where(x <= 0.0031308, x * 12.92,
                    1.055 * np.power(np.maximum(x, 1e-8), 1 / 2.4) - 0.055)
    out = os.path.join(out_dir, "rgb_srgb.png")
    try:
        from PIL import Image
    except ImportError:
        print("[render_frame] Pillow not available in this interpreter; skipping rgb_srgb.png")
        return None
    Image.fromarray((np.clip(srgb, 0, 1) * 255).astype(np.uint8)).save(out)
    print(f"[render_frame] wrote {out}  (linear->sRGB, comparable to Unreal's PNG)")
    return out


def write_hdr(annot, out_dir):
    if annot is None:
        return None
    try:
        data = annot.get_data()
    except Exception as exc:
        print(f"[render_frame] WARNING -- HDR annotator produced nothing: {exc}")
        return None

    arr = np.asarray(data)
    if arr.ndim == 3 and arr.shape[2] >= 3:
        arr = arr[:, :, :3]
    arr = arr.astype(np.float32)
    np.save(os.path.join(out_dir, "hdr.npy"), arr)

    lum = arr @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    stats = {
        "shape": list(arr.shape),
        "luminance_median": float(np.median(lum)),
        "luminance_mean": float(lum.mean()),
        "luminance_p99": float(np.percentile(lum, 99)),
        "max": float(arr.max()),
    }
    print(f"[render_frame] hdr.npy {arr.shape}  median luma {stats['luminance_median']:.6f}"
          f"  p99 {stats['luminance_p99']:.6f}  max {stats['max']:.6f}")
    return stats


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except BaseException:
        # SimulationApp.close() runs under --/app/fastShutdown=True, which hard-exits the
        # process. Anything not already flushed is lost, traceback included -- so print it
        # here rather than letting an exception vanish into a silent exit 0.
        import traceback

        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        simulation_app.close()
    sys.exit(code)
