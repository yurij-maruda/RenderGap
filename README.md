
# Overview

Cross-renderer domain-gap benchmark. One OpenUSD scene, one baked camera
trajectory, five render conditions across Isaac Sim and Unreal Engine 5.
Holds geometry, materials, camera poses and ground truth fixed, vary only light
transport, and measures what that alone costs an object detector.
Framed as an experiment, not production-ready pipeline.

Status: in dev.

# Setup

## Bat scripts & ENV variables (Windows only, so no .sh scripts available)

Copy `env\local.bat.example` and setup ENV paths.

Main scripts:
* `rg-gui.bat <auto_open.usda>` launch isaac editor
* `rg-python.bat <isaac\relative_script_path.py>` to run python under isaac Kit ecosystem
* `rg-unreal.bat <unreal arguments>` to run headless unreal cmd

Check setup with:
* `env\rg-python.bat isaac\tools\check_env.py`
* `env\rg-python.bat isaac\test\hello_stage.py`
* `env\rg-python.bat isaac\test\hello_isaac.py`

## Fetch an Unreal-compatible USD scene by the script:

* `env\rg-python.bat isaac\scene\fetch_warehouse_source.py` run CDN-to-local collection and USD linking fix script.
* `env\rg-python.bat isaac\scene\bake_preview_surfaces.py` convert MDL materials to UsdPreviewSurface format.
* `env\rg-python.bat isaac\test\hello_warehouse.py` smoke test of fetched content, render image probe of content.

`data\warehouse_payload\root_warehouse.usda` is the engine-shared scene that is based on fetched scene:
`data\warehouse_source\Isaac\Environments\Simple_Warehouse\warehouse_multiple_shelves.usd` 
including Nova Carter and Warehouse dependencies around.
Does not touch `data\warehouse_source` after receiving. It is gitignored and may be hard-overrided.

## Enable IDE Indexing
`env\rg-python.bat isaac\tools\ide_paths.py --write`

## Compile unreal_rendergap project unreal Unreal Engine 5.7

Use Unreal USD importer (Window -> USD Stage Editor -> File -> Open) 
to import `data\warehouse_payload\root_warehouse.usda` on the scene.

Probably you need to delete the old actor and related cache (UsdAssetCache.uasset + UsdAssets in Content folder) if artifacts occur.

# Launching render

## First frame image render (render check)

* `env\rg-python.bat isaac\render_frame.py` isaac render init frame. output `results\isaac\init_frame`.
* `unreal_rendergap\Scripts\render_frame.bat <MRQ_PathTracer (default) | MRQ_LumenHW | MRQ_LumenSW | MRQ_NoGI>` unreal render init frame. select needed renderer. output `results\unreal\<render_selection>`.

Unreal has additional probe scripts for diagnostic.

## Result analysis

`env\rg-python.bat -m pip install OpenEXR` require for working with Unreal Movie Render Queue output.
`env\rg-python.bat isaac\analysis\compare_frame.py --mode <MRQ_PathTracer (default) | MRQ_LumenHW | MRQ_LumenSW | MRQ_NoGI>` to get analysis output in `results\analysis\compare` folder:
/place side_to_side, histogram and difference images/

# Implementation Log

## Omniverse USD to Unreal importing & visual sync

Any custom change of the scene on Unreal side 
and change on the USD side should be strictly in sync between engines.
The only way is to keep USD a the single source of truth.
It is required to modify the USD file and engines in a way 
that the scene will look the same automatically on both sides.
This is including compatible USD properties, materials, importing rules, ect…

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
Custom RenderGapUSD Unreal module to fix the import. Isaac uses unnormalized power, but unreal uses normalized only.
Also, attenation for the full area is set up.

### 5. Camera settings export as USD object.