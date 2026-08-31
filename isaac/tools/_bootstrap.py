r"""Make ``pxr`` importable without booting Kit.

Isaac's ``python.bat`` puts ``site/sitecustomize.py`` on ``PYTHONPATH``, which
adds Kit's kernel and bindings to ``sys.path`` -- but *not* USD. In a Kit
process USD arrives as the ``omni.usd.libs`` extension, loaded only after
``SimulationApp`` starts. So ``python.bat -c "import pxr"`` fails.

Two ways out, and this module handles both:

* A plain venv with ``pip install usd-core``. ``import pxr`` just works and
  this module is a no-op. Preferred for authoring and validating scene layers,
  because a layer that opens under stock USD is a layer Unreal can also open.
* The Kit interpreter. This module locates the packaged extension in
  ``extscache`` and registers its DLL directory.

Import it before ``pxr`` in any script that does *not* create a SimulationApp.
"""

from __future__ import annotations

import glob
import os
import sys


def _usd_loads() -> bool:
    """Can USD actually be used, not merely found?

    `import pxr` is not a sufficient test. pxr is a namespace package, so the bare
    import succeeds whenever any directory named pxr is on sys.path -- including the
    Kit extension, whose modules then fail with "DLL load failed while importing _tf"
    because their bin/ directory was never registered. Importing a DLL-backed submodule
    is the real check.
    """
    try:
        from pxr import Usd  # noqa: F401

        return True
    except ImportError:
        return False


def ensure_pxr() -> str:
    """Return the name of the USD backend now usable: 'usd-core' or 'kit'."""
    if _usd_loads():
        import pxr

        # A namespace package has __file__ = None; use __path__ to locate it.
        where = (list(getattr(pxr, "__path__", [])) or [""])[0]
        isaac = os.environ.get("ISAAC_PATH") or ""
        return "kit" if isaac and os.path.normcase(where).startswith(os.path.normcase(isaac)) else "usd-core"

    isaac_path = os.environ.get("ISAAC_PATH")
    print(isaac_path)
    if not isaac_path:
        raise RuntimeError(
            "pxr is not importable and ISAAC_PATH is unset.\n"
            r"Run under env\rg-python.bat, or create the USD venv:" "\n"
            r"    python -m venv .usdvenv && .usdvenv\Scripts\pip install usd-core"
        )

    matches = sorted(glob.glob(os.path.join(isaac_path, "extscache", "omni.usd.libs-*")))
    if not matches:
        raise RuntimeError(rf"No omni.usd.libs-* extension under {isaac_path}\extscache")

    ext = matches[-1]
    if ext not in sys.path:
        sys.path.insert(0, ext)
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(os.path.join(ext, "bin"))

    if not _usd_loads():
        raise RuntimeError(
            f"USD still will not load after registering {ext}\\bin.\n"
            "If a .pth put pxr on sys.path without its DLL directory, remove it with:\n"
            r"    python isaac\tools\ide_paths.py --remove"
        )
    return "kit"

    import pxr  # noqa: F401

    return "kit"


def repo_root() -> str:
    """Repo root, from RENDERGAP_ROOT if the env wrapper set it, else by path."""
    env = os.environ.get("RENDERGAP_ROOT")
    if env:
        return os.path.abspath(env)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
