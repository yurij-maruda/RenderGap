
# Overview

Cross-renderer domain-gap benchmark. One OpenUSD scene, one baked camera
trajectory, five render conditions across Isaac Sim and Unreal Engine 5.
Holds geometry, materials, camera poses and ground truth fixed, vary only light
transport, and measures what that alone costs an object detector.
Framed as an experiment, not a production-ready pipeline.

Status: in dev.

# Setup

## Bat scripts & ENV variables (Windows only, so no .sh scripts available)

Copy `env\local.bat.example` and setup ENV paths.

Main scripts:
* `rg-gui.bat <auto_open.usda>` launch Isaac Editor
* `rg-python.bat <isaac\relative_script_path.py>` to run Python under isaac Kit ecosystem
* `rg-unreal.bat <unreal arguments>` to run headless unreal cmd

Check setup with:
* `env\rg-python.bat isaac\tools\check_env.py`
* `env\rg-python.bat isaac\test\hello_stage.py`
* `env\rg-python.bat isaac\test\hello_isaac.py`

## Fetch an Unreal-compatible USD scene with the script:

* `env\rg-python.bat isaac\scene\fetch_warehouse_source.py` run CDN-to-local collection and USD linking fix script.
* `env\rg-python.bat isaac\scene\bake_preview_surfaces.py` converts MDL materials to UsdPreviewSurface format.
* `env\rg-python.bat isaac\test\hello_warehouse.py` smoke test of fetched content, render image probe of content.

`data\warehouse_payload\root_warehouse.usda` is the engine-shared scene that is based on the fetched scene:
`data\warehouse_source\Isaac\Environments\Simple_Warehouse\warehouse_multiple_shelves.usd` 
including Nova Carter and Warehouse dependencies around.
Does not touch `data\warehouse_source` after receiving. It is gitignored and may be hard-overridden.

## Enable IDE Indexing
`env\rg-python.bat isaac\tools\ide_paths.py --write`

## Compile unreal_rendergap project in Unreal Engine 5.7

Use Unreal USD importer (Window -> USD Stage Editor -> File -> Open) 
to import `data\warehouse_payload\root_warehouse.usda` into the scene.

You probably need to delete the old actor and the related cache (UsdAssetCache.uasset + UsdAssets in the Content folder) if artifacts appear.

# Launching render

## First frame image render (render check)

* `env\rg-python.bat isaac\render_frame.py` isaac render init frame. output `results\isaac\init_frame`.
* `unreal_rendergap\Scripts\render_frame.bat <MRQ_PathTracer (default) | MRQ_LumenHW | MRQ_LumenSW | MRQ_NoGI>` unreal render init frame. select needed renderer. output `results\unreal\<render_selection>`.

Unreal has additional probe scripts for diagnostics.

## Result analysis

`env\rg-python.bat -m pip install OpenEXR` is required for working with Unreal Movie Render Queue output.

`env\rg-python.bat isaac\analysis\compare_frame.py --mode <MRQ_PathTracer (default) | MRQ_LumenHW | MRQ_LumenSW | MRQ_NoGI>` to get analysis output in `results\analysis\compare` folder.

### compare_frame.py script result:

<table>
  <tr>
    <td align="center">
      <b>Isaac RTX PT</b><br />
      <img src="autodoc/image/isaac_display.png" width="100%" alt="Isaac RTX PT">
    </td>
    <td align="center">
      <b>UE Path Tracer</b><br />
      <img src="autodoc/image/unreal_display.png" width="100%" alt="UE Path Tracer">
    </td>
  </tr>
  <tr>
    <td align="center">
      <b>Difference</b><br />
      <img src="autodoc/image/difference.png" width="100%" alt="Difference">
    </td>
    <td align="center">
      <b>Histogram</b><br />
      <img src="autodoc/image/histograms.png" width="100%" alt="Histogram">
    </td>
  </tr>
</table>

# Implementation Log

## Omniverse USD to Unreal importing & visual sync

Any custom change of the scene on Unreal side 
and change on the USD side should be strictly in sync between engines.
The only way is to keep USD a the single source of truth.
It is required to modify the USD file and engines in a way 
that the scene will look the same automatically on both sides.
This includes compatible USD properties, materials, importing rules, ect…

### 1. Need to copy all USD dependencies locally.
Local copy with local dependencies requires for Unreal loading. 
Unreal does not have direct access to the Omniverse ecosystem, including CND or Nucleus cache.
Solution: use python script for fetching and resolving USD links to local in-project files.
See ```isaac/scene/fetch_warehouse_source.py```.

### 2. Unreal stock version does not include MDL SDK to resolve its material format.
The Solution is to convert the MDL format to UsdPreviewSurface format and force both engines to use that the same format.
Converting has losses. It is hard to sync it perfectly. 
In this way, even broken materials are looks the same in both engines.
It is an acceptable and cheap solution, since the goal is to get the same looking image, not the correct material look.
See ```isaac/scene/bake_preview_surfaces.py```.

### 3. Unreal has broken multi-material mesh composing. 
On loading, it adds the first material index as MI_DisplayColor which shifting every other material by +1 index.
On the Mesh Import level it works fine, the bug occurs as a scene-level override. 
Solution: Disable UsePrimKindsToCollapsing on USD stage actor. 
This will force the engine to load every section as a separate mesh.

### 4. Unreal lightning import is not match with Isaac because of additional gaming renderer properties. 
 * Unreal reads light brightness as unnormalized and converts it to normalized form, so the USD scene must always author unnormalized power. 

 * Unused light sources must be disabled via the `active` flag, not variants, or the engines diverge. 

 * Gaming light should be restricted by distance. However, Isaac Sim simulates the light across the whole scene. There is no AttenuationRadius property for a USD scene. The custom Unreal module setup a default huge AttenuationRadius automatically. 

 * The most important one: Unreal has an engine-related light implementation anomaly. Unreal renders rect lights at twice their authored brightness. Deep in the engine, the light color is divided by `0.5 * width * height` to get radiance, and nothing downstream ever takes that 0.5 back out. That factor of two hides in your renders until you go digging through the engine source with a frame comparison on the other hand. This was also fixed by the custom importing module, which divides imported brightness by 2 automatically.

see ```autodoc/usd_transfer_losses_log.md```.

### 5. Camera settings export as USD object.
Both engines implement the same physical exposure model: ISO, shutter time, f-stop, exposure compensation. You need to author every attribute explicitly as the defaults of Unreal and Isaac are different. However, if the input parameters are the same, the math cancels out exactly and both renders land at the same brightness. 

 Distances are scaled by a factor of 100 on import. It hits focalLength and both apertures, so the FOV ratio would survive. The target aspect ratio is 4:3, from the target image size 800x600 setup: focalLength, horizontalAperture, verticalAperture. 

 Isaac-only properties: the omni:rtx:autoExposure:* block (Kit's histogram auto-exposure, kept disabled here) and shutter:open / shutter:close, which are motion blur, not exposure. Unreal reads none of them. And I don't need them anyway. 

 Several parameters needed to be manually modified with the custom Unreal module. Unreal reads only the plain exposure attribute (as AutoExposureBias) and drops exposure:time, :iso, :fStop and :responsivity entirely. The custom Unreal module maps them after the stock conversion. 
 
see ```autodoc/usd_transfer_losses_log.md```.
