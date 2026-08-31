r"""Open a USD stage inside a running Kit app. Internal -- do not run directly.

`kit.exe` has no positional argument for a stage; its usage line is

    kit [APP_CONFIG] [--exec SCRIPT ARGS...] [--</path/to/key>=<value>] ...

so a USD path handed to `isaac-sim.bat` is passed through to `kit.exe` and dropped on the
floor. `--exec` is the supported way in, and this is the script it runs.

Launched by env\rg-gui.bat as:

    --/isaac/startup/create_new_stage=false --exec "<this file>"

with the stage path in RENDERGAP_OPEN_STAGE. The path travels by environment variable
rather than as an argument because `--exec SCRIPT ARGS...` does not forward trailing
arguments to the script -- a trailing "%STAGE%" arrives as an empty sys.argv, tested.

`create_new_stage=false` is also required: without it isaacsim.app.setup schedules
startup.create_new_stage() and replaces whatever this opens.
"""

import os
import sys

import omni.usd

PREFIX = "[rendergap]"


def main() -> None:
    path = os.environ.get("RENDERGAP_OPEN_STAGE") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not path:
        print(f"{PREFIX} _open_stage.py: no stage path given (RENDERGAP_OPEN_STAGE unset)")
        return
    result = omni.usd.get_context().open_stage(path)

    # A stage that failed to open must not be mistaken for an empty scene -- that
    # ambiguity is the whole reason this file exists.
    if result:
        print(f"{PREFIX} opened stage: {path}")
    else:
        print(f"{PREFIX} FAILED to open stage: {path}")
        print(f"{PREFIX} the viewport is empty because the open failed, not because the stage is")


main()
