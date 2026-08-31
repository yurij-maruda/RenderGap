r"""Smoke test boot Kit headless, step physics, exit.

Answers one question: does this machine start Isaac Sim from a script at all?
Failures here are environment failures, not project failures -- a wrong
interpreter, a missing DLL directory, a GPU the RTX renderer will not accept.
Isolate them before touching the scene.

    env\rg-python.bat isaac\tools\hello_isaac.py

First run downloads and compiles shaders and can take several minutes. Later
runs should reach the physics loop in well under a minute.
"""

from __future__ import annotations

import sys

# SimulationApp MUST be constructed before importing anything from omni.* or
# isaacsim.* -- it is what boots the Kit runtime those modules live in.
from isaacsim import SimulationApp

simulation_app = SimulationApp({"headless": True})

import omni.kit.app  # noqa: E402
from isaacsim.core.api import World  # noqa: E402
from isaacsim.core.api.objects import DynamicCuboid, GroundPlane  # noqa: E402


def main() -> int:
    app = omni.kit.app.get_app()
    print(f"[hello_isaac] Kit version   : {app.get_build_version()}")

    # physics_dt fixed at 1/60 and rendering_dt at 1/30 so the sim is
    # deterministic -- the same discipline Pass 1 recording needs.
    world = World(physics_dt=1.0 / 60.0, rendering_dt=1.0 / 30.0, stage_units_in_meters=1.0)
    # GroundPlane, not scene.add_default_ground_plane(). The latter fetches
    # /Isaac/Environments/Grid/default_environment.usd purely for the visual
    # grid, which drags the whole asset-root question into a test whose only
    # job is "does Kit boot and step physics". GroundPlane builds the collider
    # procedurally via PhysicsSchemaTools.addGroundPlane and touches no assets,
    # so this script now fails only for reasons it is actually testing.
    world.scene.add(GroundPlane(prim_path="/World/GroundPlane", name="ground_plane"))

    cube = world.scene.add(
        DynamicCuboid(prim_path="/World/Cube", name="cube", position=[0.0, 0.0, 1.0], size=0.2)
    )

    world.reset()
    for _ in range(120):  # 2 seconds of physics
        world.step(render=False)

    pos, _ = cube.get_world_pose()
    z = float(pos[2])
    print(f"[hello_isaac] cube z after 2 s of physics = {z:.4f} m")

    # A 0.2 m cube resting on the ground plane settles with its centre at 0.1 m.
    ok = 0.05 < z < 0.15
    print(f"  [{'PASS' if ok else 'FAIL'}] cube fell and came to rest on the ground plane")
    if not ok:
        print("[hello_isaac] FAILED -- physics did not behave as expected.")
        return 1

    print("[hello_isaac] OK -- Kit boots headless and physics steps.")
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        simulation_app.close()
    sys.exit(code)
