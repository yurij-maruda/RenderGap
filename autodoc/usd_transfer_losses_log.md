# USD transfer losses

What does not survive the trip from Isaac Sim to Unreal, and what each survivor
costs. RenderGap's whole claim is that the only thing differing between two
renders is the renderer, so every entry here is a hole in that claim until it is
either closed or measured.

The rule for this document: an entry is added only after it has been traced to a
line of engine source or an authored attribute, not after it has been guessed
from a screenshot.

---

## 1. Rect-light intensity carries an area factor

**Symptom.** `RectLight_01` / `RectLight_02` rendered roughly 40x brighter in
Unreal than in Isaac Sim.

**Authored USD.** `inputs:intensity = 15000`, `4 x 10 m`,
`inputs:normalize = false`, `inputs:exposure = 0`, stage `metersPerUnit = 1`.

**What Unreal does.** `UsdToUnreal::ConvertRectLightIntensityAttr`
(`USDCore/Source/USDUtilities/Private/USDLightConversion.cpp:321`):

```
Lumens = intensity * 2^exposure * PI * Area(m2)
       = 15000 * 1 * PI * 40
       = 1 884 955.6
```

which is exactly what the imported `URectLightComponent` reports. This is the
spec-conformant UsdLux reading for `normalize = false`: `intensity` is a
luminance in nits, so total flux scales with the emitter's area.

Unreal is also self-consistent downstream.
`URectLightComponent::ComputeLightBrightness()`
(`Engine/Source/Runtime/Engine/Private/Components/RectLightComponent.cpp:183`)
converts each unit to the same internal quantity:

```cpp
Nits:   LightBrightness *= AreaInCm2;               // SourceWidth * SourceHeight
Lumens: LightBrightness *= (100.f * 100.f / UE_PI);
```

Setting those equal recovers `Lumens = Nits * PI * Area(m2)` -- the importer's
formula -- and the base `ULightComponent::ComputeLightBrightness()`
(`LightComponent.cpp:524`) simply returns `Intensity`. There is no unit bug on
the Unreal side; it really is shading 15000 nits over 40 m2.

**What Isaac does.** It renders the same prims at ~375 nits -- `15000 / 40` -- as
if the light were area-normalized, despite `normalize = false`. Confirmed
empirically: dividing the Unreal lumen figure by the area in m2 matches Isaac.

**What does not fix it.** `omni:rtx:usdluxVersion = 2505` -- the attribute behind
`LightUsdLuxVersionAPI`
(`kit/dev/fabric/include/usdrt/population/Tokens.h:766`), which Kit stamps on
lights it authors itself, e.g. `/Environment/defaultLight` in
`root_warehouse.usda`. Authoring it onto both rect lights and re-rendering in
Isaac produced no visible change. The "legacy versus current UsdLux semantics"
lever does not exist for these prims.

**Measured, and the model above is wrong.** Two successive derivations both failed
against a rendered frame, and the working value is fitted rather than derived.

The area-normalised reading -- that Isaac renders the prim as `I/Area` nits, so Unreal's
lumen figure should be divided by `Area` -- predicts a 1.00x match. It measured **22x too
dark**. Before that, dividing by `PI * Area^2` measured 1730x too dark.

The 22x was pinned to the lights by elimination, not assumption:

| candidate | bound | how |
|---|---|---|
| global illumination | <= **1.32x** | UE Path Tracer vs UE NoGI, same scene |
| material conversion | <= **1.18x** | five material regions, all 20.9x-24.6x |
| remainder | ~22x, near-uniform | the light intensity itself |

Working backwards from the frame:

```
importer raw   I * PI * Area = 15000 * PI * 40 = 1.885e6 lumens
required       1.885e6 / 22                    = 1.037e6 lumens
=> divisor     1.82  ->  taken as 2, NOT the 40 that Area gives
```

With `IntensityDivisor = 2` the rendered median ratio is **1.06x** (1.11x over the 93.8%
of pixels carrying signal in both frames), against 21.11x before. Per region: floor 1.058x,
cardboard 1.112x, crates 1.114x, labels 1.198x, back wall 1.250x. The residual spread is
now consistent with genuine light-transport difference -- the back wall, which is the most
indirectly lit surface in frame, is the furthest off -- which is the thing the benchmark
exists to measure.

**This constant is not explained.** 2 is the right magnitude for a one-sided versus
two-sided emission convention, or for how `inputs:normalize = false` is interpreted, but
neither has been traced to engine source, and this document's rule is that entries are
added only once they have been. It is recorded here as a fitted value with its evidence,
explicitly pending a derivation.

**It is also under-determined.** Both rect lights in this scene are 4x10 m, so a constant
divisor of 2 and an area-dependent `Area/20` fit the data identically. Distinguishing them
needs a second light of a different size.

**Consequence.** One `inputs:intensity` cannot satisfy both engines. Setting it
to 375 in the shared layer corrects Unreal and darkens Isaac by the same 40x.
The compensation has to be explicit.

**Resolution.** `data/warehouse_payload/root_warehouse_unreal.usda` -- a second
root layer that **sublayers** the Isaac root and overrides only the two
intensities:

```usda
(
    subLayers = [ @./root_warehouse.usda@ ]
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Z"
    ...
)

over "World" { over "warehouse_multiple_shelves" {
    over "RectLight_01" { float inputs:intensity = 375 }
    over "RectLight_02" { float inputs:intensity = 375 }
}}
```

Unreal imports `375 * PI * 40 = 47 123.9` lm -- verified against the real
importer -- and Isaac keeps opening `root_warehouse.usda` untouched.

Two constraints on that file:

* **`subLayers`, never `payload` or `references`.** A payload with no target prim
  pulls only the referenced layer's `defaultPrim` subtree and mounts it under a
  wrapper, so every path gains a segment (`/World/root_warehouse/...`) and
  `/Environment` and `/PhysicsScene` vanish from the stage. Path parity between
  the two engines is what Pass 1's trajectory layer and Pass 2B's pose walker
  address prims by; losing it is a worse problem than the one being fixed.
* **The stage metadata block is mandatory.** Stage metadata is read from the root
  layer only -- a sublayer's `upAxis` / `metersPerUnit` are silently ignored.
  Omitting it hands Unreal a Y-up centimetre stage. This is the failure
  `isaac/test/hello_stage.py` exists to catch.

The same area factor applies to disk and sphere lights with their own area terms
(`ConvertDiskLightIntensityAttr`, `ConvertSphereLightIntensityAttr`, same file).
Note the factor is the area in m2, not the constant 40 -- for the nine
`SM_LampCeilingA_*/RectLight` prims (~0.56 m2 each, currently `active = false`)
it **inverts**, and Isaac would be the brighter of the two.

---

## 2. `inputs:normalize` is dropped

Unreal's importer never reads it. The string does not appear anywhere in
`USDLightConversion.cpp`; the rect-light path reads exactly four attributes --
`intensity`, `exposure`, `width`, `height`.

The practical consequence is that `normalize` cannot be used to reconcile the two
engines. Authoring `normalize = true` changes Isaac (possibly) and Unreal
(never), which widens the gap rather than closing it.

---

## 3. `AttenuationRadius` has no USD counterpart

Imported rect lights get `URectLightComponent`'s default
`AttenuationRadius = 1000` (10 m). The importer never touches the field --
`ConvertRectLight` (`USDLightConversion.cpp:126`) sets only `SourceWidth`,
`SourceHeight`, `Intensity` and `IntensityUnits`.

In a ~40 m warehouse lit by two 4x10 m panels hung at 5.87 m, that clips both
lights 10 m out: Unreal is relatively hotter near the lamps and black beyond
them, while Isaac has no cutoff at all. It is a falloff-shape difference on top
of the intensity difference, and it does not show up as a single brightness
ratio.

There is no way to author it from USD. The importer supports no generic
`unreal:`-prefixed property override -- the only `unreal` identifier in the
plugin is a material render context (`UnrealUSDWrapper.cpp:518`). This has to be
set on the Unreal side, and because the level hosts a `UsdStageActor`, component
edits do not survive a stage reload; it needs an editor script or a
project-level default.

**Status: open.** Still 1000 as of the last import.

---

## 4. Unreal-only light fields with no USD source

`BarnDoorAngle = 88` and `BarnDoorLength = 20` are `URectLightComponent`
defaults. UsdLux has no equivalent, so they are not a *loss* so much as an
addition Isaac never sees. At 88 degrees the barn doors are nearly open and the
effect is small, but it is not zero and it is not in the USD.

---

## 5. The physical exposure model does not transfer

Both engines implement the *same* physical exposure model, and Unreal drops four fifths
of it.

**USD.** `UsdGeomCamera::ComputeLinearExposureScale` (`pxr/usd/usdGeom/camera.h:698`;
Isaac Sim 6.0.1 ships USD 25.11, and UE 5.7's bundled USD has it too):

```
linearExposureScale = exposure:responsivity
                    * exposure:time * (exposure:iso / 100) * 2^exposure
                    / exposure:fStop^2
```

**Unreal.** `GetPhysicalCameraEV100`
(`Renderer/Private/PostProcess/PostProcessEyeAdaptation.cpp:395`), where
`LuminanceMax = kISOSaturationSpeedConstant / LensAttenuation = 0.78 / 0.78 = 1` exactly
(`:249-261`), so the divisor is literally the product:

```
scale = 2^AutoExposureBias
      / (DepthOfFieldFstop^2 * CameraShutterSpeed * 100 / CameraISO)
```

**What Unreal reads.** `UsdToUnreal::ConvertGeomCamera` reads exactly one exposure
attribute -- the plain `exposure`, into `AutoExposureBias` (`USDPrimConversion.cpp:569`).
`exposure:time`, `exposure:iso`, `exposure:fStop` and `exposure:responsivity` are never
read. `CameraShutterSpeed` and `CameraISO` keep their `FPostProcessSettings` defaults of
60 and 100 (`Scene.cpp:487-488`), which have no relationship to the stage.

There is no `unreal:`-prefixed override for them either. The importer's camera vocabulary
is exactly ten attributes -- `focalLength`, `focusDistance`, `fStop`,
`horizontalAperture`, `verticalAperture`, `horizontalApertureOffset`,
`verticalApertureOffset`, `exposure`, `projection`, `clippingRange` -- and the exporter
`UnrealToUsd::ConvertCameraComponent` (`USDPrimConversion.cpp:2281`) writes back the same
ten. The set is closed.

**Measured cost.** Before the fix, `/OmniverseKit_Persp` carried `exposure:fStop = 6.01`,
`exposure:time = 0.02` -> `linearExposureScale = 5.537e-4`, a 10.8-stop darkening Unreal
ignored entirely. Neutralising those to 1.0 and rendering both engines gave a measured
**6.52-stop** disagreement against a **6.43-stop** prediction from the two formulas above
-- the exposure mismatch accounted for essentially the whole gap.

**Resolution.** `FRenderGapGeomCameraTranslator`
(`unreal_rendergap/Source/RenderGapUSD/`), registered under `UsdGeomCamera` the same way
the light translator is registered, reads the four dropped attributes and writes
`CameraShutterSpeed = 1/exposure:time`, `CameraISO = exposure:iso`,
`AutoExposureBias = exposure + log2(responsivity)`, and forces `AEM_Manual` with
`AutoExposureApplyPhysicalCameraExposure`. The `responsivity` fold is exact rather than a
fudge: `2^bias` is the only place Unreal can express that term.

`exposure:fStop` must be set equal to `fStop` -- required, because Unreal's exposure
aperture comes from `fStop` via `CurrentAperture` (section 6). With that, the two formulas
cancel. Verified end to end at f/4, 1/60 s, ISO 500: both engines compute `0.005208333`,
and `unreal_rendergap/Scripts/probe/probe_camera.py` reads back `CameraShutterSpeed 60.000004`, `CameraISO 500.0`,
`AutoExposureBias 0.0`.

**Kit does honour the standard attributes.** This was not safe to assume -- Kit has its own
`OmniRtxCameraExposureAPI_1`. Halving `exposure:time` from 1/60 to 1/120 halved the linear
frame: median `0.034835 -> 0.017456` (x0.5011), p99 x0.5012, max x0.5003.

---

## 6. `fStop` and `focusDistance` mean different things to the two engines

`fStop` is not only a depth-of-field control in Unreal.
`UCineCameraComponent::UpdateCameraLens` (`CineCameraComponent.cpp:706`) copies
`CurrentAperture` into `PostProcessSettings.DepthOfFieldFstop`, and that is the value the
renderer squares in the EV100 formula in section 5. So the USD `fStop` sets Unreal's
exposure.

That makes the obvious way to switch depth of field off actively harmful. Authoring
`fStop = 0` does disable focus -- `ConvertGeomCamera` sets
`FocusMethod = ECameraFocusMethod::Disable` off the *authored* value
(`USDPrimConversion.cpp:543-546`) -- but `SetCurrentAperture(0)` is then clamped to
`LensSettings.MinFStop = 1.2` by `RecalcDerivedData` (`CineCameraComponent.cpp:529`), and
**f/1.2 silently becomes the exposure aperture**. The USD file no longer states what
Unreal actually uses.

So the aperture has to be a real f-number inside `[MinFStop 1.2, MaxFStop 22]`. Depth of
field then has to be disabled some other way -- and this is where the two engines diverge.

**Kit reads the camera's physical focus directly.** With
`/rtx/post/dof/overrideEnabled = false` (the default), Kit's depth of field uses the
prim's own `fStop` and `focusDistance`. Authoring `focusDistance = 0` -- which looks
harmless, and is the USD schema fallback -- puts the focal plane at zero distance and
smears the entire frame.

Two things make this easy to miss:

* **The carb setting is not the switch.** `/rtx/post/dof/enabled` reads `False` in both
  `isaacsim.exp.base.python.kit` and the GUI's `isaacsim.exp.full.kit`, and the frame is
  blurred anyway. Reading that value and concluding depth of field is off is wrong.
* **It depends on the render mode.** Under `PathTracing` the frame is sharp; under
  `RealTimePathTracing` -- which is what the editor viewport uses -- it is destroyed.
  So an offline path-traced control frame looks perfect while the viewport, and every
  real-time condition, is unusable.

Measured on the same 800x600 frame, mean gradient magnitude as a sharpness proxy:

| | mean gradient | contrast (std) |
|---|---|---|
| real-time, `focusDistance = 0` | 0.392 | 22.10 |
| real-time, `focusDistance = 10` | 10.948 | 46.57 |
| path-traced control | 13.314 | 48.08 |

**Resolution.** Author a real subject distance -- `focusDistance = 10.0`, the racking
being about 10 m down the aisle. At 24 mm f/4 the hyperfocal distance is ~4.8 m, so
focusing at 10 m keeps roughly 2.4 m to infinity sharp and the frame is correct whether or
not a given viewport has depth of field enabled.

Unreal must not then acquire depth of field from that non-zero distance (spec section 3.1
makes it a controlled variable), so `FRenderGapGeomCameraTranslator` forces
`FocusSettings.FocusMethod = ECameraFocusMethod::Disable`. `Disable` rather than
`DoNotOverride`: it pins `DepthOfFieldFocalDistance = 0` (`CineCameraComponent.cpp:722`)
instead of deferring to whatever a post-process volume might say, and it still takes the
branch that sets `DepthOfFieldFstop = CurrentAperture` (`:707`), so the exposure aperture
survives. Verified: `FocusMethod = DISABLE`, `DepthOfFieldFocalDistance = 0.0`,
`DepthOfFieldFstop = 4.0`.

---

## 6b. Movie Render Queue overrides the path tracer's sample count

Not a USD transfer loss, but it invalidated two earlier conclusions in this document, so
it is recorded here.

Path-traced Movie Render Queue frames came out with **41.7% of pixels at exactly zero** --
hard black specks scattered through lit regions, all three channels zero together, alpha
1.0 everywhere (so geometry was hit). Brightening the lights 125x did not change the count
by even 0.01 percentage points, which ruled out float16 underflow: these were real zeros
from the renderer, i.e. pixels for which no light path was ever found.

The cause is that **none of the obvious ways to set path-tracer sample count work under
MRQ**:

* `r.PathTracing.SamplesPerPixel` is explicitly ignored for offline renders.
  `PathTracing.cpp:3687`:
  ```cpp
  int32 SamplesPerPixelCVar = View.bIsOfflineRender ? -1 : CVarPathTracingSamplesPerPixel...;
  uint32 MaxSPP = SamplesPerPixelCVar > -1 ? SamplesPerPixelCVar
                                           : View.FinalPostProcessSettings.PathTracingSamplesPerPixel;
  ```
  and MRQ sets `bIsOfflineRender = true` (`MoviePipelineImagePassBase.cpp:275`).

* The `PostProcessSettings.PathTracingSamplesPerPixel` it falls back to is then overwritten
  anyway. `MoviePipelineImagePassBase.cpp:371-393`, comment verbatim -- *"override whatever
  settings came from PostProcessVolume or Camera"*:
  ```cpp
  const int32 SampleCount = bAccumulateSpatialSamplesOnly
      ? InOutSampleState.SpatialSampleCount
      : InOutSampleState.TemporalSampleCount * InOutSampleState.SpatialSampleCount;
  View->FinalPostProcessSettings.bOverride_PathTracingSamplesPerPixel = true;
  View->FinalPostProcessSettings.PathTracingSamplesPerPixel = SampleCount;
  ```

**So MRQ's own spatial sample count IS the path tracer's spp.** `SpatialSampleCount = 1`
renders the frame at 1 spp. Setting it to 512, to match `isaac/render_frame.py --spp 512`:

| | 1 spp | 512 spp |
|---|---|---|
| exact zeros | 41.73% | **0.00%** |
| max | 2.658 | 0.689 |
| camera alignment (px shift) | (-23, 0) | **(0, 0)** |
| edge-overlap IoU | 0.065 | **0.444** |
| PSNR after gain | 11.15 dB | **20.77 dB** |

The alignment and edge readings were never a geometry problem -- they were measuring
1-spp noise. Both gates pass at the correct sample count.

**Retraction.** This section previously claimed the UE Path Tracer ignores the camera
exposure, on the evidence that a 1728x ISO change produced a byte-identical file. That test
was run at 1 spp and the conclusion was wrong. Re-run at 512 spp, doubling `exposure:iso`
from 500 to 1000 scales the frame by **2.0011x**. The path tracer honours exposure exactly,
like every other path. Sections 5 and 6 stand; the exposure model transfers correctly end
to end.

**What remains** is a pure scene difference: with exposure identical and both frames
converged, Isaac's linear median is 22.5x Unreal's. That is the light/material transfer
question, and it is now the only open number.

---

## 7. Focal length is in tenths of a stage unit, and the clamp makes it bite

`UsdToUnreal::ConvertDistance` (`USDTypesConversion.cpp:351`) is
`Value *= StageInfo.MetersPerUnit / 0.01` -- x100 on this metre stage. It is applied to
`focalLength` and to both apertures, so horizontal FOV, being the pure ratio
`2*atan(hAperture / (2*focal))`, would survive untouched.

It does not, because `RecalcDerivedData` (`CineCameraComponent.cpp:528`) clamps the
**focal length** against `LensSettings` while leaving the **sensor** unclamped, and the
default lens preset is `Universal Zoom`, 4-1000 mm (`BaseEngine.ini:3634-3635`):

```
authored  focalLength 24, horizontalAperture 20.955
arrives   focal 2400 -> clamped to 1000, sensor 2095.5 unclamped
intended  hFOV = 2*atan(20.955 / (2*24))   = 47.1686 deg
delivered hFOV = 2*atan(2095.5 / (2*1000)) = 92.6716 deg
```

Nearly 2x wrong, and nothing is logged. There is no USD route to `LensSettings`, so the
clamp range itself cannot be authored around.

**Resolution.** Pre-divide focal length and both apertures by 100 in
`camera_settings.usda`, so the x100 lands inside the clamp range. Isaac is unaffected --
`GfCamera` derives FOV from the ratio alone. Verified: 24/20.955, 0.24/0.20955 and
2400/2095.5 all give 47.1686 deg, and Unreal reports `CurrentHorizontalFOV 47.168552`.

---

## 8. `Filmback.SensorAspectRatio` is left stale

`ConvertGeomCamera` assigns `Filmback.SensorWidth` and `SensorHeight` as plain fields
(`USDPrimConversion.cpp:551-557`) and never re-runs `RecalcDerivedData` afterwards -- the
only calls to it come from the `SetCurrentFocalLength` / `SetCurrentAperture` setters
*earlier* in the same function.

So `SensorAspectRatio` keeps whatever the component was constructed with: **1.7778**, from
the `16:9 Digital Film` default preset, even when the imported sensor is 4:3. Measured on
the imported camera before the fix: `SensorWidth/SensorHeight = 1.3333` while
`SensorAspectRatio = 1.7778`.

That is not cosmetic. `UCameraComponent::AspectRatio` derives from it
(`CineCameraComponent.cpp:536`) and `ACineCameraActor` ships with
`bConstrainAspectRatio = true`, so an 800x600 Movie Render Queue job would letterbox
against a phantom 16:9 camera.

**Resolution.** `FRenderGapGeomCameraTranslator` re-assigns the filmback through its
setter (`SetFilmback`), which recomputes the derived field. The values are unchanged.

---

## 9. `clippingRange`'s far plane is dropped

`ConvertGeomCamera` (`USDPrimConversion.cpp:583-594`) sends the near value to both
`SetOrthoNearClipPlane` and `SetCustomNearClippingPlane` (with `bOverride_` set), but the
far value only reaches `SetOrthoFarClipPlane`. Unreal has no far clip plane in perspective
mode, so for a perspective camera the authored far distance has no effect.

Harmless here -- nothing in the warehouse is 1 km away -- but it is a real attribute that
does not survive, and a scene that relied on far-clipping would differ silently.

---

## A note on authoring

Both layers in `data/warehouse_payload/` must be hand-authored. Opening and
saving them in the Isaac GUI rewrites them: it re-roots sublayers as payloads,
injects `customLayerData`, a `/Render` scope and four `OmniverseKit_*` camera
`over`s, and leaves crumbs like `xformOp:translate = (0, 0, 8.88e-16)` and
redundant local opinions that shadow the source asset. `docs/tooling.md` already
states the rule; these files are the worked example.
