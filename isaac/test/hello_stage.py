r"""Smoke test USD only, no Isaac Sim.

Proves you can author and compose layers, and demonstrates the composition rule
that Gate 1 depends on: **stage metadata is read from the root layer only.**
A sublayer declaring ``upAxis = "Y"`` is silently ignored. Get this wrong in the
layer Unreal imports and you get a Y-up centimetre stage, which shows up as a
mask IoU near 0.8 instead of 0.99 -- a full week later.

    env\rg-python.bat isaac\test\hello_stage.py

Exit code 0 on success, 1 on any failed assertion.
"""

from __future__ import annotations

import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from isaac.tools._bootstrap import ensure_pxr, repo_root  # noqa: E402

BACKEND = ensure_pxr()

from pxr import Sdf, Usd, UsdGeom  # noqa: E402

OUT = os.path.join(repo_root(), "data", "_smoke")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    print(f"[hello_stage] USD backend : {BACKEND}")
    print(f"[hello_stage] USD version : {Usd.GetVersion()}")
    print(f"[hello_stage] output dir  : {OUT}")

    # --- a sublayer, deliberately authored with the WRONG stage metadata ------
    sub_path = os.path.join(OUT, "sub_wrong_metadata.usda")
    sub = Usd.Stage.CreateNew(sub_path) if not os.path.exists(sub_path) else Usd.Stage.Open(sub_path)
    UsdGeom.SetStageUpAxis(sub, UsdGeom.Tokens.y)  # <- wrong on purpose
    UsdGeom.SetStageMetersPerUnit(sub, 0.01)  # <- centimetres, wrong on purpose
    cube = UsdGeom.Cube.Define(sub, "/World/Box")
    cube.CreateSizeAttr(1.0)
    sub.GetRootLayer().Save()

    # --- the root layer -------------------------------------------------------
    root_path = os.path.join(OUT, "root.usda")
    stage = Usd.Stage.CreateNew(root_path) if not os.path.exists(root_path) else Usd.Stage.Open(root_path)
    stage.GetRootLayer().subLayerPaths = ["./sub_wrong_metadata.usda"]

    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    stage.SetTimeCodesPerSecond(30.0)
    stage.SetStartTimeCode(0.0)
    stage.SetEndTimeCode(0.0)

    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    stage.GetRootLayer().Save()

    # --- reopen and read the COMPOSED result ---------------------------------
    composed = Usd.Stage.Open(root_path)
    up = UsdGeom.GetStageUpAxis(composed)
    mpu = UsdGeom.GetStageMetersPerUnit(composed)
    fps = composed.GetTimeCodesPerSecond()
    default = composed.GetDefaultPrim().GetPath()
    prims = [str(p.GetPath()) for p in composed.Traverse()]

    print(f"[hello_stage] upAxis           = {up}")
    print(f"[hello_stage] metersPerUnit    = {mpu}")
    print(f"[hello_stage] timeCodesPerSec  = {fps}")
    print(f"[hello_stage] defaultPrim      = {default}")
    print(f"[hello_stage] composed prims   = {prims}")

    checks = [
        ("root layer wins on upAxis", up == UsdGeom.Tokens.z),
        ("root layer wins on metersPerUnit", mpu == 1.0),
        ("timeCodesPerSecond survives", fps == 30.0),
        ("defaultPrim is /World", default == Sdf.Path("/World")),
        ("sublayer geometry composed in", "/World/Box" in prims),
    ]

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    if failed:
        print(f"[hello_stage] FAILED: {len(failed)} check(s)")
        return 1
    print("[hello_stage] OK -- USD authoring and composition work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
