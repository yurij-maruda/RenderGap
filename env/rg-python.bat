@echo off
:: ---------------------------------------------------------------------------
:: Run a script under the Isaac Sim Kit interpreter with the repo's asset root.
::
::     env\rg-python.bat isaac\tools\hello_isaac.py
::
:: Adds nothing that python.bat does not already do, except:
::   * RENDERGAP_ROOT      -> repo root, so scripts never guess their location.
::     This overrides whatever local.bat set, so a stale path there cannot send
::     a script at the wrong tree.
::
:: ISAACSIM_ASSET_ROOT comes from local.bat and is NOT touched here: the mirrored
:: closure lives under data\warehouse_source, and both engines read that same
:: tree. Its state is only reported, never corrected.
:: ---------------------------------------------------------------------------
setlocal
for %%I in ("%~dp0..") do set "REPO=%%~fI"

if not exist "%~dp0local.bat" (
    echo [rendergap] env\local.bat not found.
    echo [rendergap] Copy env\local.bat.example to env\local.bat and set ISAACSIM_ROOT.
    exit /b 1
)
call "%~dp0local.bat"

if not exist "%ISAACSIM_ROOT%\python.bat" (
    echo [rendergap] No python.bat under ISAACSIM_ROOT=%ISAACSIM_ROOT%
    exit /b 1
)

set "RENDERGAP_ROOT=%REPO%"
if not defined ISAACSIM_ASSET_ROOT (
    echo [rendergap] assets: ISAACSIM_ASSET_ROOT unset in env\local.bat
) else if exist "%ISAACSIM_ASSET_ROOT%\warehouse_source\Isaac" (
    echo [rendergap] assets: local  ^(%ISAACSIM_ASSET_ROOT%\warehouse_source^)
) else (
    echo [rendergap] assets: empty -- run isaac\scene\fetch_warehouse_source.py
)

call "%ISAACSIM_ROOT%\python.bat" %*
exit /b %errorlevel%
