@echo off
:: ---------------------------------------------------------------------------
:: One offline frame from Unreal, no editor.
::
::     unreal_rendergap\Scripts\render_frame.bat              :: MRQ_PathTracer (spec condition 2)
::     unreal_rendergap\Scripts\render_frame.bat MRQ_LumenHW  :: conditions 3-5
::
:: UnrealEditor-Cmd.exe ... -game runs the EDITOR binary in game mode, so
:: editor-only code is present. That is what lets the UsdStageActor build
:: UStaticMesh render data when it translates the stage at load; a packaged
:: build could not, which is why this is the -game route and not a cooked one.
::
:: The camera comes from ARenderGapBenchGameMode, which points the player view
:: target at the USD prim -- see Scripts\setup_bench.py for why there is no
:: Camera Cut track.
::
:: env\rg-unreal.bat checks the module BuildId before launching; a mismatch
:: makes the engine exit during startup with no output and no error.
:: ---------------------------------------------------------------------------
setlocal
set "CONDITION=%~1"
if "%CONDITION%"=="" set "CONDITION=MRQ_PathTracer"

for %%I in ("%~dp0..\..") do set "REPO=%%~fI"
set "OUTDIR=%REPO%\results\unreal\%CONDITION%"

call "%~dp0..\..\env\rg-unreal.bat" L_UsdWarehouse -game ^
    -MoviePipelineConfig=/Game/Bench/%CONDITION% ^
    -LevelSequence=/Game/Bench/LS_BenchFrame ^
    -resx=800 -resy=600 ^
    -RenderOffscreen -NoLoadingScreen -NoScreenMessages -notexturestreaming
set "RC=%errorlevel%"
if not "%RC%"=="0" exit /b %RC%

:: Movie Render Queue can exit 0 having rendered nothing -- a missing config
:: asset, a render pass that produced no output. Check rather than assume.
if not exist "%OUTDIR%\*.exr" (
    echo.
    echo [rendergap] The engine exited cleanly but wrote no frame to:
    echo [rendergap]   %OUTDIR%
    echo [rendergap] Check that /Game/Bench/%CONDITION% exists -- run
    echo [rendergap]   env\rg-unreal.bat -run=pythonscript -script="unreal_rendergap\Scripts\setup_bench.py"
    echo [rendergap] and search the log above for "LogMovieRenderPipeline".
    exit /b 1
)

echo [rendergap] wrote:
dir /b "%OUTDIR%"
exit /b 0
