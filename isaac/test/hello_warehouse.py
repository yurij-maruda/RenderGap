r"""Smoke test the real assets through the real capture path.

Loads warehouse_multiple_shelves.usd and Nova Carter, points a camera down an
aisle, and writes one 800x600 PNG through Replicator's BasicWriter. That is a
miniature of Pass 2A, so a failure here is a failure you would otherwise meet
on day 7 with eighteen trajectories already recorded.

    env\rg-python.bat isaac\test\hello_warehouse.py

Assets come from ISAACSIM_ASSET_ROOT\warehouse_source and nowhere else. There is
no CDN fallback: if ISAACSIM_ASSET_ROOT is unset, or the warehouse is not under
it, the test fails. Isaac and Unreal have to read the same bytes off disk, so a
silent fall back to the network would make the comparison meaningless.

Output: data/_smoke/warehouse/rgb_0000.png
"""

from __future__ import annotations

import os
import sys

from isaacsim import SimulationApp

simulation_app = SimulationApp({"headless": True})

import carb  # noqa: E402
import omni.replicator.core as rep  # noqa: E402
from isaacsim.core.experimental.utils.stage import (  # noqa: E402
    add_reference_to_stage,
    create_new_stage,
    set_stage_time_code,
    set_stage_units,
    set_stage_up_axis,
)
from pxr import Gf, UsdGeom, UsdLux  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from isaac.tools._bootstrap import repo_root  # noqa: E402

ASSET_SUBDIR = "warehouse_source"  # the mirrored closure, under ISAACSIM_ASSET_ROOT
ENV_URL = "Isaac/Environments/Simple_Warehouse/warehouse_multiple_shelves.usd"
ROBOT_URL = "Isaac/Samples/ROS2/Robots/Nova_Carter_ROS.usd"

RESOLUTION = (800, 600)  # spec section 6.2 -- same resolution as the real runs
RT_SUBFRAMES = 32  # let the path tracer converge; a noisy frame is not evidence

OUT = os.path.join(repo_root(), "results", "isaac", "_smoke")


def asset_path(assets_root: str, rel: str) -> str:
    """Absolute path with forward slashes -- USD wants those even on Windows."""
    return os.path.join(assets_root, rel).replace("\\", "/")


def resolve_assets_root() -> str | None:
    """ISAACSIM_ASSET_ROOT/warehouse_source, or None after saying what is missing."""
    root = os.environ.get("ISAACSIM_ASSET_ROOT", "").strip().strip('"')
    if not root:
        print("[hello_warehouse] FAILED -- ISAACSIM_ASSET_ROOT is not set.")
        print("  Run through env\\rg-python.bat, and collect the assets first:")
        print("    env\\rg-python.bat isaac\\scene\\fetch_warehouse_source.py")
        return None

    assets_root = os.path.join(root, ASSET_SUBDIR)
    if not os.path.isdir(assets_root):
        print(f"[hello_warehouse] FAILED -- no {ASSET_SUBDIR} under ISAACSIM_ASSET_ROOT.")
        print(f"  expected: {assets_root}")
        print("  Populate it with isaac\\scene\\fetch_warehouse_source.py")
        return None

    missing = [rel for rel in (ENV_URL, ROBOT_URL)
               if not os.path.isfile(os.path.join(assets_root, rel))]
    if missing:
        print(f"[hello_warehouse] FAILED -- {assets_root} is missing:")
        for rel in missing:
            print(f"    {rel}")
        print("  Re-run isaac\\scene\\fetch_warehouse_source.py -- there is no CDN fallback.")
        return None

    return assets_root


def main() -> int:
    assets_root = resolve_assets_root()
    if assets_root is None:
        return 1
    print(f"[hello_warehouse] asset root : {assets_root}")

    # --- stage --------------------------------------------------------------
    # These four lines are the whole Gate 1 contract. Z-up, metres, 30 fps.
    create_new_stage()
    set_stage_up_axis("Z")
    set_stage_units(meters_per_unit=1.0)
    set_stage_time_code(start_time_code=0.0, end_time_code=0.0, time_codes_per_second=30.0)

    env_prim = add_reference_to_stage(asset_path(assets_root, ENV_URL), "/World/Environment")
    robot_prim = add_reference_to_stage(asset_path(assets_root, ROBOT_URL), "/World/Robot")
    print(f"[hello_warehouse] environment: {env_prim.GetPath()}  valid={env_prim.IsValid()}")
    print(f"[hello_warehouse] robot      : {robot_prim.GetPath()}  valid={robot_prim.IsValid()}")

    stage = env_prim.GetStage()

    # Insurance against a black frame: the warehouse ships its own lights, but a
    # dim dome guarantees the smoke test distinguishes "renderer broken" from
    # "lights did not load".
    dome = UsdLux.DomeLight.Define(stage, "/World/SmokeDomeLight")
    dome.CreateIntensityAttr(300.0)

    # --- camera --------------------------------------------------------------
    # Z-up world, camera looks down its local -Z. RotateX(+90) maps local -Z to
    # world +Y and local +Y to world +Z, so this looks horizontally along +Y
    # with the horizon level. Eye height 1.5 m.
    cam = UsdGeom.Camera.Define(stage, "/World/SmokeCam")
    cam.CreateFocalLengthAttr(24.0)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.01, 1000.0))
    xf = UsdGeom.Xformable(cam.GetPrim())
    xf.AddTranslateOp().Set(Gf.Vec3d(0.0, -6.0, 1.5))
    xf.AddRotateXYZOp().Set(Gf.Vec3f(90.0, 0.0, 0.0))

    # --- capture -------------------------------------------------------------
    rep.orchestrator.set_capture_on_play(False)
    carb.settings.get_settings().set("/rtx/post/dlss/execMode", 2)  # Quality

    os.makedirs(OUT, exist_ok=True)
    backend = rep.backends.get("DiskBackend")
    backend.initialize(output_dir=OUT)
    writer = rep.writers.get("BasicWriter")
    writer.initialize(backend=backend, rgb=True)

    rp = rep.create.render_product("/World/SmokeCam", RESOLUTION, name="smoke", force_new=True)
    writer.attach([rp])

    # Let the stage finish loading before asking for a frame.
    for _ in range(60):
        simulation_app.update()

    print(f"[hello_warehouse] capturing 1 frame at {RESOLUTION[0]}x{RESOLUTION[1]} ...")
    rep.orchestrator.step(delta_time=0.0, rt_subframes=RT_SUBFRAMES)
    rep.orchestrator.wait_until_complete()

    writer.detach()
    rp.destroy()

    # --- verify --------------------------------------------------------------
    pngs = sorted(f for f in os.listdir(OUT) if f.endswith(".png"))
    if not pngs:
        print(f"[hello_warehouse] FAILED -- no PNG written to {OUT}")
        return 1

    path = os.path.join(OUT, pngs[-1])
    size = os.path.getsize(path)
    print(f"[hello_warehouse] wrote {path} ({size} bytes)")

    checks = [
        ("environment reference resolved", env_prim.IsValid() and bool(env_prim.GetChildren())),
        ("robot reference resolved", robot_prim.IsValid() and bool(robot_prim.GetChildren())),
        ("a PNG was written", True),
        # A uniformly black 800x600 PNG compresses to roughly 2 kB. Anything
        # above 20 kB means the renderer actually put something in the frame.
        ("frame is not blank", size > 20_000),
    ]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    if any(not ok for _, ok in checks):
        print("[hello_warehouse] FAILED -- open the PNG and look at it.")
        return 1

    print("[hello_warehouse] OK -- assets resolve and the capture path works.")
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        simulation_app.close()
    sys.exit(code)
