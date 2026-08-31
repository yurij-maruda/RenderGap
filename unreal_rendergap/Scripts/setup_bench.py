r"""Create the Movie Render Queue assets for the bench frame. Run once.

    env\rg-unreal.bat -run=pythonscript -script="unreal_rendergap\Scripts\setup_bench.py"

Creates, under /Game/Bench:

    LS_BenchFrame     one frame at 60 fps (the stage's timeCodesPerSecond), no
                      bindings at all.
    MRQ_PathTracer    spec condition 2 -- UE Path Tracer, the diagnostic pair for
                      Isaac RTX PT.
    MRQ_LumenHW       condition 3.
    MRQ_LumenSW       condition 4.
    MRQ_NoGI          condition 5, the calibration floor.

The four configs differ only in their console variables, which is the whole point
of the split: everything else about the render is held identical by construction
rather than by discipline.

No Camera Cut track, deliberately. The camera is spawned transiently by the
AUsdStageActor, and Sequencer possessables do not reliably bind to that. With no
cut, MRQ renders from the player's view target (MoviePipeline.cpp:1403/1442/1720,
and MoviePipelineBlueprintLibrary.cpp:993 handles an ACineCameraActor there
explicitly), and ARenderGapBenchGameMode points that at the USD camera by prim
path. So the frame comes from the real stage-spawned camera.

Exposure is NOT set here. It arrives from camera_settings.usda through
FRenderGapGeomCameraTranslator; a cvar override at this layer would silently
defeat that, which is why there are no r.EyeAdaptation or exposure cvars below.
"""

import sys

import unreal

PACKAGE_PATH = "/Game/Bench"
SEQUENCE_NAME = "LS_BenchFrame"
RESOLUTION = (800, 600)
DISPLAY_RATE = 60  # matches the stage's timeCodesPerSecond
OUTPUT_DIR = "{project_dir}/../results/unreal"
ENGINE_WARM_UP_FRAMES = 240  # the UsdStageActor has to open and translate the stage first

# Path tracer samples per pixel. This is NOT r.PathTracing.SamplesPerPixel and NOT the
# PostProcessVolume setting -- MoviePipelineImagePassBase.cpp:371-393 hard-overrides both
# ("override whatever settings came from PostProcessVolume or Camera") with
#     SampleCount = TemporalSampleCount * SpatialSampleCount   (motion blur off)
# so MRQ's own spatial sample count IS the path tracer's spp. Leaving it at 1 renders the
# frame at 1 spp, which leaves pixels with no light path at all -- hard black specks over
# 41.7% of the frame. Matched to isaac/render_frame.py --spp 512.
PATH_TRACER_SPP = 512

# Spatial samples for the RASTERISED conditions. Unlike the path tracer -- where MRQ
# rewrites PathTracingSamplesPerPixel from this number -- here MRQ accumulates this many
# independently jittered frames into one. Lumen is stochastic and normally leans on
# temporal accumulation across frames, which a single offline frame does not get, so at 1
# sample its indirect lighting arrives as raw noise: measured 18.5% (Lumen HW) and 20.6%
# (Lumen SW) relative noise in dark regions against the path tracer's 15.4%. Accumulating
# also supersamples, which is why AntiAliasingMethod can stay NONE.
DEFERRED_SPP = 64

# Lumen's screen-space history has to converge before the first accumulated sample is
# taken, which is what render warm-up frames are for. 32 was not enough for the dark,
# indirect-dominated parts of this scene.
RENDER_WARM_UP = 96

# Held identical across every condition. Spec section 3.1: post-processing is a controlled
# variable, so anything that would differ between engines for reasons unrelated to light
# transport is switched off rather than matched.
COMMON_CVARS = [
    ("r.MotionBlurQuality", 0.0),
    ("r.DepthOfFieldQuality", 0.0),
    ("r.BloomQuality", 0.0),
    ("r.SceneColorFringeQuality", 0.0),
    ("r.Tonemapper.Sharpen", 0.0),
    ("r.LocalExposure", 0.0),
    ("r.ScreenPercentage", 100.0),
    ("r.VolumetricFog", 0.0),
]

CONDITIONS = {
    "MRQ_PathTracer": dict(
        path_tracer=True,
        spatial_samples=PATH_TRACER_SPP,
        cvars=[
            ("r.PathTracing", 1.0),
            ("r.PathTracing.MaxBounces", 8.0),
            # Denoising is a post-process difference, not light transport -- and Isaac's
            # OptiX denoiser is off for the same reason.
            ("r.PathTracing.Denoiser", 0.0),
            # Firefly clamp, in EXPOSED units. 24 is the engine default
            # (FPostProcessSettings::PathTracingMaxPathIntensity, Scene.cpp:664); pinned
            # explicitly so it is reproducible rather than inherited. Isaac's analogue is
            # maxRayUnexposedIntensity 6400, which at this exposure is ~33 exposed.
            ("r.PathTracing.MaxPathIntensity", 24.0),
        ],
    ),
    "MRQ_LumenHW": dict(
        path_tracer=False,
        spatial_samples=DEFERRED_SPP,
        cvars=[
            ("r.DynamicGlobalIlluminationMethod", 1.0),
            ("r.ReflectionMethod", 1.0),
            ("r.Lumen.HardwareRayTracing", 1.0),
        ],
    ),
    "MRQ_LumenSW": dict(
        path_tracer=False,
        spatial_samples=DEFERRED_SPP,
        cvars=[
            ("r.DynamicGlobalIlluminationMethod", 1.0),
            ("r.ReflectionMethod", 1.0),
            ("r.Lumen.HardwareRayTracing", 0.0),
        ],
    ),
    "MRQ_NoGI": dict(
        path_tracer=False,
        spatial_samples=DEFERRED_SPP,
        cvars=[
            ("r.DynamicGlobalIlluminationMethod", 0.0),
            ("r.ReflectionMethod", 0.0),
        ],
    ),
}


def log(msg):
    print(f"[setup_bench] {msg}")


def create_asset(name, factory, asset_class):
    path = f"{PACKAGE_PATH}/{name}"
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        log(f"replacing existing {path}")
        unreal.EditorAssetLibrary.delete_asset(path)
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    asset = tools.create_asset(name, PACKAGE_PATH, asset_class, factory)
    if asset is None:
        raise RuntimeError(f"could not create {path}")
    return asset, path


def build_sequence():
    seq, path = create_asset(SEQUENCE_NAME, unreal.LevelSequenceFactoryNew(), unreal.LevelSequence)
    seq.set_display_rate(unreal.FrameRate(DISPLAY_RATE, 1))
    seq.set_playback_start(0)
    seq.set_playback_end(1)
    unreal.EditorAssetLibrary.save_asset(path)
    log(f"{path}  1 frame @ {DISPLAY_RATE}fps, no bindings")
    return path


def build_config(name, spec):
    config, path = create_asset(
        name, unreal.MoviePipelinePrimaryConfigFactory(), unreal.MoviePipelinePrimaryConfig)

    render_pass_class = (unreal.MoviePipelineDeferredPass_PathTracer if spec["path_tracer"]
                         else unreal.MoviePipelineDeferredPassBase)
    config.find_or_add_setting_by_class(render_pass_class)

    # Linear, scene-referred output. Kit's tonemap operators and Unreal's filmic curve
    # cannot be made byte-identical, so the numeric comparison happens in linear and one
    # display transform is applied offline to both -- matching Isaac's hdr.npy, which is
    # also untonemapped.
    #
    # In legacy MRQ this lives on its own setting, not on the render pass: it flips
    # SceneCaptureSource from SCS_FinalToneCurveHDR to SCS_FinalColorHDR
    # (MoviePipelineRendering.cpp:447). It is per-job, so the PNG written alongside is
    # linear too and will look far too dark -- the EXR is the artifact, and
    # analysis/compare_frame.py writes the viewable PNGs for both engines.
    color = config.find_or_add_setting_by_class(unreal.MoviePipelineColorSetting)
    color.set_editor_property("disable_tone_curve", True)

    out = config.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
    out.set_editor_property("output_resolution", unreal.IntPoint(RESOLUTION[0], RESOLUTION[1]))
    out.set_editor_property("output_directory", unreal.DirectoryPath(f"{OUTPUT_DIR}/{name}"))
    out.set_editor_property("file_name_format", "frame.{frame_number}")
    out.set_editor_property("override_existing_output", True)
    out.set_editor_property("zero_pad_frame_numbers", 4)

    config.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
    # float16. Legacy MRQ has no 32-bit toggle for the beauty pass -- bHighPrecisionOutput
    # lives on FMoviePipelinePostProcessPass, i.e. only the extra post-process material
    # passes. Fine as long as the frame is exposed near middle grey; a badly
    # underexposed frame loses its shadows to half-float subnormals.
    config.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_EXR)

    aa = config.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
    aa.set_editor_property("spatial_sample_count", spec["spatial_samples"])
    aa.set_editor_property("temporal_sample_count", 1)
    aa.set_editor_property("override_anti_aliasing", True)
    # The path tracer does its own sampling; TAA on top would only add a temporal
    # difference between the two engines.
    aa.set_editor_property("anti_aliasing_method", unreal.AntiAliasingMethod.AAM_NONE)
    aa.set_editor_property("render_warm_up_count", RENDER_WARM_UP)
    aa.set_editor_property("engine_warm_up_count", ENGINE_WARM_UP_FRAMES)
    aa.set_editor_property("use_camera_cut_for_warm_up", False)

    game = config.find_or_add_setting_by_class(unreal.MoviePipelineGameOverrideSetting)
    game.set_editor_property("game_mode_override", unreal.RenderGapBenchGameMode)
    game.set_editor_property("cinematic_quality_settings", True)
    game.set_editor_property("texture_streaming",
                             unreal.MoviePipelineTextureStreamingMethod.DISABLED)
    game.set_editor_property("use_lod_zero", True)
    game.set_editor_property("disable_hlods", True)

    # The CVars array is private and marked ScriptNoExport, so it cannot be assigned
    # directly -- AddOrUpdateConsoleVariable is the supported scripting entry point.
    cvar_setting = config.find_or_add_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
    entries = COMMON_CVARS + spec["cvars"]
    for cvar_name, value in entries:
        if not cvar_setting.add_or_update_console_variable(cvar_name, value):
            raise RuntimeError(f"could not set cvar {cvar_name}={value}")

    unreal.EditorAssetLibrary.save_asset(path)
    log(f"{path}  {'path tracer' if spec['path_tracer'] else 'deferred'}, "
        f"{len(entries)} cvars, warm-up {ENGINE_WARM_UP_FRAMES}")
    return path


def main():
    if not unreal.EditorAssetLibrary.does_directory_exist(PACKAGE_PATH):
        unreal.EditorAssetLibrary.make_directory(PACKAGE_PATH)

    build_sequence()
    for name, spec in CONDITIONS.items():
        build_config(name, spec)

    log("done. Render with:")
    log(r"    env\rg-unreal.bat L_UsdWarehouse -game "
        r"-MoviePipelineConfig=/Game/Bench/MRQ_PathTracer "
        r"-LevelSequence=/Game/Bench/LS_BenchFrame ...")
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except BaseException:
        import traceback
        traceback.print_exc()
    sys.stdout.flush()
    log(f"exit {code}")
    if code != 0:
        raise SystemExit(code)
