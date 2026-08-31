@echo off
:: ---------------------------------------------------------------------------
:: Run UnrealEditor-Cmd against the repo's project, headless.
::
::     env\rg-unreal.bat -run=pythonscript -script=unreal\setup_bench.py
::     env\rg-unreal.bat L_UsdWarehouse -game -MoviePipelineConfig=...
::
:: The project path and -unattended/-nosplash are supplied here so no caller has
:: to hardcode a machine-specific path; everything else is passed straight on.
:: Mirrors env\rg-python.bat, including RENDERGAP_ROOT.
:: ---------------------------------------------------------------------------
setlocal
for %%I in ("%~dp0..") do set "REPO=%%~fI"

if not exist "%~dp0local.bat" (
    echo [rendergap] env\local.bat not found.
    echo [rendergap] Copy env\local.bat.example to env\local.bat and set UE_ROOT.
    exit /b 1
)
call "%~dp0local.bat"

set "UE_CMD=%UE_ROOT%\Engine\Binaries\Win64\UnrealEditor-Cmd.exe"
if not exist "%UE_CMD%" (
    echo [rendergap] No UnrealEditor-Cmd.exe under UE_ROOT=%UE_ROOT%
    exit /b 1
)

set "RENDERGAP_ROOT=%REPO%"
set "UPROJECT=%REPO%\unreal_rendergap\unreal_rendergap.uproject"

set "ENGINE_MODULES=%UE_ROOT%\Engine\Binaries\Win64\UnrealEditor.modules"
set "PROJECT_MODULES=%REPO%\unreal_rendergap\Binaries\Win64\UnrealEditor.modules"

if not exist "%PROJECT_MODULES%" (
    echo [rendergap] The project has no compiled modules yet:
    echo [rendergap]   %PROJECT_MODULES%
    goto :needbuild
)

set "ENGINE_BID="
set "PROJECT_BID="
for /f "usebackq tokens=1,* delims=:" %%A in (`findstr /C:"BuildId" "%ENGINE_MODULES%"`) do set "ENGINE_BID=%%B"
for /f "usebackq tokens=1,* delims=:" %%A in (`findstr /C:"BuildId" "%PROJECT_MODULES%"`) do set "PROJECT_BID=%%B"

if not "%ENGINE_BID%"=="%PROJECT_BID%" (
    echo [rendergap] BuildId mismatch -- the engine will skip this project's modules
    echo [rendergap] and exit during startup, producing no output and no error.
    echo [rendergap]   engine : %ENGINE_BID%
    echo [rendergap]   project: %PROJECT_BID%
    goto :needbuild
)

echo [rendergap] engine : %UE_ROOT%
echo [rendergap] project: %UPROJECT%

"%UE_CMD%" "%UPROJECT%" %* -unattended -nosplash -stdout -FullStdOutLogOutput
exit /b %errorlevel%

:needbuild
echo [rendergap]
echo [rendergap] Check UE_ROOT in env\local.bat first -- a plain number vs a GUID
echo [rendergap] above means the project was built against a different engine.
echo [rendergap] If UE_ROOT is right, rebuild:
echo [rendergap]
echo [rendergap]   "%UE_ROOT%\Engine\Build\BatchFiles\Build.bat" unreal_rendergapEditor Win64 Development -Project="%UPROJECT%" -WaitMutex
echo [rendergap]
exit /b 1
