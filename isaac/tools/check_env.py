r"""Preflight the environment before anything slow happens.

Pure stdlib in its default mode, so it runs in well under a second under any
interpreter. Everything it checks would otherwise surface 30 seconds into a Kit
boot, as an error that does not name the variable at fault.

    python isaac\tools\check_env.py                   static checks
    env\rg-python.bat isaac\tools\check_env.py        + interpreter identity
    env\rg-python.bat isaac\tools\check_env.py --deep + boot Kit and confirm
                                                        the asset root resolved

Three levels of "valid", and they are not the same question:

  1. Is the variable set?                     os.environ
  2. Does it point at something real,
     and at the RIGHT something?              filesystem + a sentinel file
  3. Did the process actually honour it?      only observable at runtime

Most env-var bugs pass (1), fail (2) silently, and are only diagnosed at (3).
The classic on this project: a PyCharm run configuration with every variable
set correctly, pointed at the system interpreter instead of Kit's.

Exit code 0 if no check failed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import repo_root  # noqa: E402

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"
_results: list[tuple[str, str, str]] = []


def record(level: str, name: str, detail: str = "") -> None:
    _results.append((level, name, detail))


def check(name: str, ok: bool, detail: str = "", soft: bool = False) -> bool:
    record(PASS if ok else (WARN if soft else FAIL), name, detail)
    return ok


def norm(p: str | None) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(p))) if p else ""


# --------------------------------------------------------------------------
# Who am I?
# --------------------------------------------------------------------------
def check_interpreter() -> bool:
    exe = sys.executable
    record(INFO, "sys.executable", exe)
    record(INFO, "python version", sys.version.split()[0])

    is_kit = os.path.join("kit", "python") in norm(exe)
    record(INFO, "interpreter", "Kit" if is_kit else "plain CPython")

    if is_kit:
        # Kit's interpreter is pinned. A mismatch means the build was replaced
        # under a run configuration that still points at the old path.
        check(
            "Kit interpreter is Python 3.12",
            sys.version_info[:2] == (3, 12),
            "found %d.%d" % sys.version_info[:2],
            soft=True,
        )
    return is_kit


# --------------------------------------------------------------------------
# The four variables python.bat sets
# --------------------------------------------------------------------------
def check_kit_vars(required: bool) -> None:
    """A variable is only 'valid' if it points at the right thing.

    Each check pairs the path with a sentinel file that must exist inside it.
    Existence alone is not evidence: ISAAC_PATH pointing at a sibling build
    directory is a real directory and produces a real, confusing failure.
    """
    isaac_path = os.environ.get("ISAAC_PATH")
    carb_app = os.environ.get("CARB_APP_PATH")
    exp_path = os.environ.get("EXP_PATH")
    pythonpath = os.environ.get("PYTHONPATH", "")

    def var(name: str, value: str | None, sentinel: str, what: str) -> str | None:
        if not value:
            record(FAIL if required else INFO, name + " set", "unset")
            return None
        if not os.path.isdir(value):
            check(name + " is a directory", False, value)
            return None
        found = os.path.exists(os.path.join(value, sentinel))
        detail = value if found else value + "  (no " + sentinel + ")"
        check(name + " contains " + what, found, detail)
        return value if found else None

    isaac_ok = var("ISAAC_PATH", isaac_path, "python.bat", "python.bat")
    carb_ok = var("CARB_APP_PATH", carb_app, "kit.exe", "the Kit executable")
    var("EXP_PATH", exp_path, "isaacsim.exp.base.kit", "the .kit experiences")

    # Consistency. Two valid paths from two different builds is the worst
    # failure mode here: everything imports, and the extension versions differ.
    if isaac_ok and carb_ok:
        expected = os.path.join(isaac_ok, "kit")
        check(
            "CARB_APP_PATH is ISAAC_PATH + kit",
            norm(carb_ok) == norm(expected),
            carb_ok + "  vs  " + expected,
        )

    # PYTHONPATH is not checked for a literal string -- what matters is that
    # some entry on it actually holds sitecustomize.py.
    entries = [e for e in pythonpath.split(os.pathsep) if e]
    holder = next((e for e in entries if os.path.exists(os.path.join(e, "sitecustomize.py"))), None)
    if required:
        check("PYTHONPATH reaches sitecustomize.py", holder is not None, holder or pythonpath or "unset")
        # Set is not the same as honoured: PYTHONPATH assigned after the
        # interpreter started has no effect at all.
        imported = "sitecustomize" in sys.modules
        check(
            "sitecustomize was actually imported",
            imported,
            "" if imported else "not in sys.modules -- PYTHONPATH was set too late",
        )
    elif holder:
        record(INFO, "PYTHONPATH reaches sitecustomize.py", holder)


# --------------------------------------------------------------------------
# Project variables
# --------------------------------------------------------------------------
def check_repo_var() -> str:
    root = os.environ.get("RENDERGAP_ROOT")
    if not root:
        record(INFO, "RENDERGAP_ROOT set", "unset -- derived from script location")
    else:
        check("RENDERGAP_ROOT is the repo", os.path.exists(os.path.join(root, "README.md")), root)
    return repo_root()


def check_asset_root(root: str) -> None:
    """ISAACSIM_ASSET_ROOT must be <repo>\\data, and hold warehouse_source under it.

    The subdirectory is not incidental. hello_warehouse.py joins
    ISAACSIM_ASSET_ROOT\\warehouse_source, while data\\warehouse_payload\\
    root_warehouse.usda payloads ..\\warehouse_source relative to itself. Both
    resolve to the same tree only while the variable points at <repo>\\data --
    otherwise Isaac's smoke test and the shared scene read different bytes, and
    the whole cross-renderer comparison is measuring the wrong thing.
    """
    value = os.environ.get("ISAACSIM_ASSET_ROOT")

    if not value:
        record(FAIL, "ISAACSIM_ASSET_ROOT set", r"unset -- set it in env\local.bat to <repo>\data")
        return

    if value.startswith(("http://", "https://", "omniverse://")):
        record(FAIL, "ISAACSIM_ASSET_ROOT is remote", f"{value} -- Unreal cannot resolve it; use <repo>\\data")
        return

    if not check("ISAACSIM_ASSET_ROOT is a directory", os.path.isdir(value), value):
        return

    expected = os.path.join(root, "data")
    check("ISAACSIM_ASSET_ROOT is <repo>\\data", norm(value) == norm(expected), f"{value} (expected {expected})")

    source = os.path.join(value, "warehouse_source")
    if not os.path.isdir(os.path.join(source, "Isaac")):
        record(WARN, "warehouse_source populated", rf"{source} is empty -- run isaac\scene\fetch_warehouse_source.py")
        return

    record(PASS, "warehouse_source populated", source)
    manifest = os.path.join(source, "_fetch_manifest.json")
    check("fetch manifest present", os.path.isfile(manifest), manifest, soft=True)


def check_usd() -> None:
    try:
        from pxr import Usd  # noqa: F401

        # pxr is a namespace package: __file__ is None, so locate it via __path__.
        # A bare `import pxr` would pass even when the DLLs cannot load, which is the
        # exact failure a .pth on sys.path without its bin/ directory produces.
        import pxr

        where = (list(getattr(pxr, "__path__", [])) or ["?"])[0]
        record(PASS, "pxr imports and its DLLs load", where)
    except ImportError:
        record(
            INFO,
            "pxr not importable directly",
            "expected under Kit -- USD loads with the omni.usd.libs extension; "
            "scripts use _bootstrap.ensure_pxr()",
        )


# --------------------------------------------------------------------------
# Level 3: did the runtime honour any of it?
# --------------------------------------------------------------------------
def check_deep() -> None:
    """Boot Kit and ask it what it resolved. Slow, and the only real answer."""
    from isaacsim import SimulationApp

    app = SimulationApp({"headless": True})
    try:
        import carb
        from isaacsim.storage.native import get_assets_root_path

        setting = carb.settings.get_settings().get("/persistent/isaac/asset_root/default")
        resolved = get_assets_root_path()
        record(INFO, "asset_root setting", str(setting))
        record(INFO, "get_assets_root_path()", str(resolved))

        check("Isaac resolved an asset root", resolved is not None)

        env_value = os.environ.get("ISAACSIM_ASSET_ROOT")
        if env_value and resolved:
            # ISAACSIM_ASSET_ROOT has the highest precedence of any source --
            # above the .kit file and above the command line. If the resolved
            # root differs, the variable was set after Kit read it.
            same = norm(env_value) == norm(resolved) or env_value.rstrip("/") == resolved.rstrip("/")
            check(
                "ISAACSIM_ASSET_ROOT took effect",
                same,
                "env=" + env_value + "  resolved=" + str(resolved),
            )
    finally:
        app.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the RenderGap environment.")
    ap.add_argument("--deep", action="store_true", help="Boot Kit and confirm the settings took effect.")
    args, _ = ap.parse_known_args()

    is_kit = check_interpreter()
    check_kit_vars(required=is_kit)
    root = check_repo_var()
    record(INFO, "repo root", root)
    check_asset_root(root)
    check_usd()

    if args.deep:
        if not is_kit:
            record(WARN, "--deep skipped", "requires the Kit interpreter (env" + chr(92) + "rg-python.bat)")
        else:
            check_deep()

    width = max(len(name) for _lvl, name, _d in _results)
    print()
    for level, name, detail in _results:
        line = "  [" + level + "] " + name.ljust(width)
        print(line + "  " + detail if detail else line)

    failed = sum(1 for lvl, _n, _d in _results if lvl == FAIL)
    warned = sum(1 for lvl, _n, _d in _results if lvl == WARN)
    print()
    if failed:
        print("[check_env] FAILED -- %d failed, %d warnings" % (failed, warned))
        return 1
    print("[check_env] OK -- no failures, %d warnings" % warned)
    return 0


if __name__ == "__main__":
    sys.exit(main())
