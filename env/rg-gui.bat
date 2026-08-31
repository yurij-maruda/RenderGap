@echo off
:: ---------------------------------------------------------------------------
:: Launch the Isaac Sim GUI against the same asset root the scripts use.
:: Use it to READ the stage -- find prim paths, check the robot spawned upright,
:: frame a camera. Do not author scene layers here: a Save As from the GUI
:: flattens or re-paths references, and mouse gestures do not enter git.
::
::     env\rg-gui.bat
::     env\rg-gui.bat isaac\scene\record_root.usda
:: ---------------------------------------------------------------------------
setlocal
for %%I in ("%~dp0..") do set "REPO=%%~fI"

if not exist "%~dp0local.bat" (
    echo [rendergap] env\local.bat not found. Copy env\local.bat.example first.
    exit /b 1
)
call "%~dp0local.bat"

:: ISAACSIM_ASSET_ROOT is left exactly as local.bat set it -- the mirrored closure
:: lives under data\warehouse_source and both engines read that same tree.
set "RENDERGAP_ROOT=%REPO%"

if "%~1"=="" (
    call "%ISAACSIM_ROOT%\isaac-sim.bat"
    exit /b %errorlevel%
)

:: Resolve to an absolute path -- Kit's working directory is not this one.
for %%F in ("%~1") do set "STAGE=%%~fF"

:: Fail loudly on a missing file. Kit would otherwise come up with an empty scene,
:: which looks identical to success and hides a one-character typo (root.usd for
:: root.usda cost an afternoon).
if not exist "%STAGE%" (
    echo [rendergap] no such file: %STAGE%
    exit /b 1
)

:: Two flags, both required:
::   create_new_stage=false  isaacsim.app.setup otherwise schedules
::                           startup.create_new_stage^(^) and replaces our stage
::   --exec                  kit.exe has no positional argument for a USD path,
::                           so the open has to happen from inside the app
::
:: The path goes in an environment variable, not on the command line: despite the
:: "--exec SCRIPT ARGS..." usage line, a trailing argument reaches the script as an
:: empty sys.argv. Tested.
set "RENDERGAP_OPEN_STAGE=%STAGE%"
call "%ISAACSIM_ROOT%\isaac-sim.bat" --/isaac/startup/create_new_stage=false --exec "%REPO%\isaac\tools\_open_stage.py"
exit /b %errorlevel%
