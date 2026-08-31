# RenderGap — Cross-Renderer Domain Gap Benchmark

**Technical specification and execution protocol**

A matched-pair study measuring how much object detection performance depends on the
rendering engine, holding scene, geometry, camera trajectory and ground truth
constant. One OpenUSD scene, driven by ROS 2 / Nav2 in Isaac Sim, baked to a
deterministic animation layer, then re-rendered under five light-transport
conditions across two engines.

---

## 1. Thesis

Published synthetic-data work varies assets, layout, sensor model, distribution
and renderer simultaneously, then reports a sim-to-real gap that cannot be
attributed to any single cause. This project isolates one variable — the
renderer — and measures its effect on a perception model.

The deliverable is not the pipeline. It is a number, with a validated claim that
the number means what it says.

**Primary question.** Given identical geometry, identical camera poses and
identical ground truth, how much detection performance is lost when the only
change is light transport?

**Secondary question.** How much of any observed gap is caused by the rendering
algorithm, and how much by OpenUSD losing fidelity in transit between engines?

---

## 2. Hypotheses and why every outcome is publishable

| Outcome | Interpretation | Value |
|---|---|---|
| Large gap (>10 mAP) | Renderer choice is a first-order SDG decision | Motivates renderer randomisation as an augmentation axis |
| Small gap (<3 mAP) | Real-time rendering is sufficient for detection training | ~100× cost argument: rasterise at 60fps instead of path-tracing at 0.2fps |
| Isaac-PT ≈ UE-PT, Lumen diverges | USD transfer is clean, GI approximation is the cause | The cleanest possible result |
| Isaac-PT ≠ UE-PT | USD import is lossy | Documents exactly what OpenUSD fails to carry between engines |

The last row is a finding, not a failure. Material, light and tonemapping loss
across an Isaac↔Unreal USD round trip is undocumented, and describing it
precisely requires having actually built the thing.

---

## 3. Experimental design

### 3.1 Controlled variables

Everything below is **identical** across every render condition:

- Scene geometry and prop placement (same USD stage)
- Camera pose per frame (same baked layer)
- Robot articulation per frame (same baked layer)
- Camera intrinsics, resolution, aspect ratio
- Ground-truth annotations (generated once, in Isaac)
- Exposure (fixed manual, no auto-exposure)
- Tonemapper and colour space
- Post-processing (bloom, DOF, motion blur, grain, vignette — all disabled)

### 3.2 Manipulated variable

Light transport only. Five conditions, Section 6.

### 3.3 Explicitly out of scope

- **Material randomisation.** Would destroy the Isaac-PT vs UE-PT diagnostic,
  since divergence could no longer be attributed to import loss. Belongs in a
  separate arm (Section 14).
- **Post-process variation.** Anything reproducible in numpy should be an offline
  augmentation, not a render pass. Rule of thumb for the README:
  *render what cannot be augmented, augment what can.*
- **Live cross-engine physics sync.** Non-deterministic; would introduce
  uncontrolled pose drift on top of the effect being measured. See Section 5.3.

---

## 4. System architecture

```
                    ┌──────────────────────────────┐
                    │  warehouse.usd  (authored     │
                    │  once — geometry, materials,  │
                    │  lights, semantics)           │
                    └──────────────┬────────────────┘
                                   │
        ┌──────────────────────────┴──────────────────────────┐
        │                                                     │
   PASS 1: RECORD                                      (stage is read-only
   Isaac Sim + PhysX                                    from here on)
   Nav2 over ROS 2 drives Nova Carter
   → writes traj_NN.usda  (timeSampled world-space
      transforms: camera, joints, moved props)
        │
        └──────────────────────────┬──────────────────────────┐
                                   │                          │
                    PASS 2A: ISAAC REPLAY          PASS 2B: UNREAL REPLAY
                    physics OFF, playback only     USD Stage import + layer
                    Replicator annotators:         Movie Render Queue steps
                      rgb                          the same poses
                      bounding_box_2d_tight        outputs: rgb
                      instance_segmentation                 stencil mask
                                   │                          │
                                   └────────┬─────────────────┘
                                            │
                                   OFFLINE (PyTorch)
                                   - mask IoU validation gate
                                   - train Faster R-CNN on Isaac train split
                                   - evaluate on all 5 conditions
                                   - mAP table + agreement metrics
```

### Component responsibilities

| Component | Role | Why it is here |
|---|---|---|
| **OpenUSD** | Single source of truth for scene + baked animation layer | The interchange claim being tested |
| **Isaac Sim** | Physics authority, ground-truth generator, control renderer | Owns PhysX, Replicator annotators, RTX path tracer |
| **ROS 2 / Nav2** | Generates the robot trajectories in Pass 1 | Real navigation, not scripted paths — load-bearing during generation |
| **Unreal Engine 5** | Second and third and fourth renderer | Lumen, Path Tracer, deferred; the comparison target |
| **PyTorch** | Instrument (detector) and analysis | Defines what "renderer difference" means perceptually |

---

## 5. Scene and trajectory specification

### 5.1 Scene

- Base: Isaac Sim SimReady warehouse assets, **scoped to a single aisle**
  (VRAM constraint, Section 12).
- Classes: `pallet`, `box`. Two classes is deliberate — enough for a meaningful
  mAP, few enough to converge on 5k frames.
- Semantics applied explicitly to every labelled prim via `rep.modify.semantics`
  or stage tagging. **Untagged geometry is invisible to Replicator** — this is
  the most common first-day failure.
- Lighting: fixed, physically-based, authored once. No variation between
  trajectories (lighting is scene content, and varying it would confound with
  the manipulated variable).
- Prop layout randomised **per trajectory** by seed, from a manifest.

### 5.2 Trajectories

| Split | Trajectories | Frames each | Total | Rendered in |
|---|---|---|---|---|
| Train | 15 | ~330 | ~5000 | Isaac only |
| Eval | 3 | ~170 | ~500 | All 5 conditions |

Only the **eval set must be paired**. Training frames never need to exist in
Unreal — the model only needs to learn what a pallet looks like. This is what
keeps render cost tractable: 500 × 5, not 5000 × 5.

**Temporal subsampling:** keep every 3rd frame. Consecutive frames from a slow
AMR are near-duplicates; 15 shorter trajectories beat 10 longer ones for the
same frame count.

**Split discipline:** held out **by trajectory, not by frame.** A random frame
split leaks near-identical images into both sides and inflates every number.

**Leakage guard:** maintain `manifest.json` recording trajectory ID, layout seed
and split assignment. Eval layouts drawn from a reserved seed range that training
never touches.

### 5.3 Why the animation is baked

Nav2 is non-deterministic — re-running it produces a different path. If images
were captured during the driving pass, any later decision to re-render (different
sample count, fixed material, new render condition) would require re-driving, and
Isaac frames would no longer correspond to Unreal frames.

Baking to USD `timeSamples` gives bit-exact pose parity by construction, makes
every re-render free and safe, and removes network jitter and interpolation drift
from the measurement entirely.

**Bake world-space transforms, not joint angles.** This sidesteps having to make
Unreal's imported skeleton hierarchy match Isaac's exactly — a fiddly source of
silent error.

Record per frame: camera transform, every robot joint, every prop that moved.
Sample at render rate. Unreal must **step** the sequence frame by frame, never
interpolate.

**README framing:** state plainly that baking was chosen for determinism and why.
A robotics reader will notice that ROS terminates before the engine boundary;
getting ahead of it reads as judgement. Claiming live sync and having someone
find the drift reads as the opposite.

---

## 6. Render conditions

All at **800×600**, identical intrinsics, PNG.

| # | Condition | Engine | Role in the table |
|---|---|---|---|
| 1 | RTX path-traced, converged | Isaac Sim | **Control** — same renderer as training data |
| 2 | Path Tracer (Movie Render Queue) | UE5 | **Diagnostic** — same light transport, different engine → isolates USD import loss |
| 3 | Lumen, hardware ray tracing | UE5 | Primary real-time condition |
| 4 | Lumen, software tracing | UE5 | Throughput/quality tradeoff people actually make |
| 5 | Deferred, GI disabled | UE5 | **Floor** — calibration anchor, not a finding |

### 6.1 Resolution rationale

800×600 is not arbitrary. torchvision's Faster R-CNN transform resizes internally
to `min_size` / `max_size`; at 800×600 the short side is 600, so set
`min_size=600, max_size=800` and **nothing is resampled**. Rendering lower (e.g.
700×500) causes upscaling to the default 800 — paying the compute anyway on a
blurrier image.

Resolution is an experimental variable, not an efficiency knob. Downsampling is a
low-pass filter, and the renderer differences under investigation (shadow
boundary softness, GI gradients, specular highlights) live in high frequencies.
A null result caused by resolution is the worst available outcome.

### 6.2 Convergence and post-process discipline

- Path-traced conditions (1 and 2) must be **converged**. Residual sampler noise
  is a between-render difference unrelated to light transport and will
  contaminate the gap. Use Movie Render Queue for UE — a naive `SceneCapture2D`
  writes unconverged frames.
- Fixed manual exposure everywhere. **Auto-exposure is scene-dependent** and
  therefore silently couples to frame content.
- Same tonemapper and colour space in both engines. UE's Path Tracer output still
  passes through the post-process chain.
- Validate with a histogram comparison on one static frame before trusting
  anything (Section 9, Gate 2).

---

## 7. Ground truth

Generated **once**, in Isaac, in Pass 2A. Reused unchanged for every render
condition.

```json
{
  "frame": "traj_11_000042.png",
  "objects": [
    {"bbox": [312, 208, 447, 361], "class": "pallet"},
    {"bbox": [498, 190, 566, 240], "class": "box"}
  ]
}
```

- Annotator: `bounding_box_2d_tight` → `(x_min, y_min, x_max, y_max)` pixel
  coords + semantic ID, exactly the format torchvision expects.
- Use **tight**, not loose. Loose boxes project the full 3D extent including
  occluded parts.
- **Filter zero-area boxes.** Fully occluded objects yield degenerate tight
  boxes; torchvision will error on them.
- Convert `BasicWriter` output to COCO format — `torchmetrics` and every
  dataloader example expect it.
- Enable **only** `rgb`, `bounding_box_2d_tight`, `instance_segmentation`. Each
  additional annotator is another render pass with its own targets and costs
  VRAM. Skip normals, 3D boxes and depth unless pursuing the depth stretch goal.

**Why labels transfer for free:** a bounding box is a statement about geometry and
camera, not pixels. Changing light transport changes how pixels look; it does not
move the pallet. Hence one label set, five renders — and hence Gate 1, which
verifies that assumption actually held.

---

## 8. Model and training protocol

The detector is an **instrument**, not the subject. It must be calibrated and
honest, not state of the art.

| Parameter | Value | Rationale |
|---|---|---|
| Architecture | `fasterrcnn_resnet50_fpn`, COCO-pretrained | From-scratch on 5k frames underfits; a weak instrument reads noise |
| Fallback | `fasterrcnn_mobilenet_v3_large_fpn` | If 12GB is tight — legitimate, since only the *difference* matters. Declare it in the README |
| Transform | `min_size=600, max_size=800` | Prevents resampling |
| Batch size | 2 (4 with AMP) | ~7–9GB fp32, ~5–6GB with AMP |
| Precision | AMP (`autocast` + `GradScaler`) | ~35% VRAM saving, no accuracy cost |
| Augmentation | **Horizontal flip only** | Colour jitter and lighting augmentation make the model renderer-robust — i.e. destroy the effect being measured |
| Seeds | 3 | Seed variance is the noise floor |
| Training data | Isaac train split only | Single model, five evaluations |

**Augmentation as phase two:** rerun with full augmentation enabled and show the
gap shrinking. That is renderer-invariance-as-mitigation, and it is worth more
than the baseline alone.

---

## 9. Metrics

### 9.1 Primary result

```
gap(condition) = mAP(model, Isaac eval frames) − mAP(model, condition frames)
```

Same weights, same labels, same poses. The only thing that changed is the
renderer, so the difference is attributable to the renderer.

The individual mAP values are intermediate readings. If Isaac scores 34 and Lumen
scores 26, the result is **8**.

Report as **mean ± std across 3 seeds**. If seed-to-seed variance on the same
renderer is 5 mAP, an 8-point cross-renderer gap is barely signal. This single
detail is what makes it an experiment rather than a demo.

Metric: COCO `mAP@[.50:.95]` via `torchmetrics.detection.MeanAveragePrecision`
(wraps `pycocotools` — do not implement it). Report `mAP@0.5` alongside for
interpretability, and `mAP@0.75` for the localisation story below.

### 9.2 Error decomposition

mAP is a scalar and hides mechanism. Spend an hour here:

- **Missed detections** (recall collapse) → appearance changed enough that
  features no longer match.
- **Localisation drift** (mAP@0.75 drops much harder than mAP@0.5) → edges and
  shadows moved. Exactly what a GI change predicts.

Plot precision-recall curves for all conditions on shared axes. This figure will
carry more of the README than the table.

### 9.3 Prediction agreement (label-free, secondary)

Most domain-gap studies cannot do this because their domains are not
frame-matched. Yours are pixel-aligned, so predictions can be compared one-to-one.

Per frame, Hungarian-match Isaac predictions to condition predictions by box IoU:

- **Flip rate** — detected in one render but not the other, per direction
- **Box drift** — mean IoU between matched pairs
- **Confidence delta** — mean score change for matched detections

Confidence delta is the most sensitive of the three: a detection can survive a
renderer change while confidence slides 0.95 → 0.62, which mAP@0.5 barely
registers but which matters enormously if thresholding in production.

**This requires no labels**, so the same measurement transfers to unlabelled real
footage. Frame it in the README as a deployment tool, not just a benchmark.

### 9.4 Pixel metrics

Compute PSNR and LPIPS between paired frames. Nearly free. The interesting result
is disagreement: **large LPIPS with small mAP gap** means the renderers look
different to humans but not to models — a real finding with a real cost
implication.

### 9.5 Validation gates

These are pass/fail conditions. If a gate fails, every downstream number is
meaningless.

**Gate 1 — geometric alignment (critical).**
Per-frame IoU between Isaac's instance segmentation and Unreal's stencil mask.

- ≥0.99 → geometry, units, handedness and intrinsics are correct; residual gap is
  genuinely appearance
- ~0.8 → coordinate or scale bug; **stop and fix before rendering anything else**

This gate is what separates the project from a blog post. It also catches the
silent failure where a mesh does not import and the model is trained against
phantom labels.

The Unreal stencil masks exist **only** for this gate. They are not a label
source.

**Gate 2 — photometric sanity.**
Histogram comparison on one static frame, Isaac-PT vs UE-PT. Large divergence
indicates tonemapping or exposure mismatch, i.e. measuring colour grading.

**Gate 3 — instrument noise floor.**
3-seed std on the control condition. Must be small relative to the smallest
reported gap.

---

## 10. Result table

| Eval condition | mAP@[.5:.95] | mAP@0.5 | mAP@0.75 | Gap vs control | Mask IoU |
|---|---|---|---|---|---|
| Isaac RTX PT (held out) | — | — | — | control | 1.00 |
| UE Path Tracer | — | — | — | — | — |
| UE Lumen (HW RT) | — | — | — | — | — |
| UE Lumen (SW) | — | — | — | — | — |
| UE deferred, no GI | — | — | — | floor | — |

All values mean ± std over 3 seeds. Every row scored against the same Isaac
ground truth.

Read the table as two independent axes:

- **Row 1 vs Row 2** — same light transport, different engine → **pipeline loss**
- **Rows 2 → 3 → 4 → 5** — same engine, different light transport → **GI fidelity**

If rows 1 and 2 nearly match, the USD round trip is clean and the Lumen deltas
are trustworthy. Without row 2, a Lumen gap cannot be distinguished from a
material that failed to import.

---

## 11. Execution protocol

12 working days. Each day ends in something verifiable.

| Day | Task | Done when |
|---|---|---|
| 1 | Isaac Sim install, GPU verified, warehouse stage opens headless | `python.sh --no-window` loads the stage without OOM |
| 2 | Nova Carter + Nav2 driving between waypoints over ROS 2 | `ros2 topic echo /tf` shows the robot moving on a planned path |
| 3 | Replicator annotators wired; semantics tagged on all pallets/boxes | 20 frames written with non-empty tight boxes; overlay visually correct |
| 4 | **Trajectory recorder** — write camera/joint/prop transforms to a USD layer | `traj_00.usda` contains timeSamples; reloading reproduces the drive exactly |
| 5 | Recorder hardened; batch-generate 15 train + 3 eval trajectories | `manifest.json` complete, no seed overlap between splits |
| 6 | **Start Isaac path-traced eval render (overnight).** Meanwhile: UE5 USD import | 500 control frames queued; UE opens the stage |
| 7 | UE unit/handedness conversion + pose walker; **Gate 1** | Mask IoU ≥0.99 on 20 frames |
| 8 | UE render conditions 2–5 via Movie Render Queue; **Gate 2** | 4 × 500 frames on disk; histograms match |
| 9 | COCO conversion, dataloader, training run seed 0 | Control mAP is non-degenerate (>20) |
| 10 | Seeds 1–2; **Gate 3**; evaluate all 5 conditions | Full table populated with ± std |
| 11 | Error decomposition, PR curves, agreement metrics, LPIPS | Figures generated |
| 12 | README, side-by-side comparison GIF, write-up | Repo publishable |

### Scheduling notes

- **Day 4 is the only genuinely novel code on the Isaac side.** Spend care there;
  everything downstream depends on the bake being correct.
- **Day 7 will take longer than expected.** Budget the full day for cm↔m,
  left↔right handedness and material loss. This is where the project usually
  slips.
- **Start the path-traced render on day 6, not day 10.** Test throughput on 10
  frames first and measure. At 8s/frame, 500 frames is ~1 hour; at 40s/frame it
  is 5.5 hours and sample count needs cutting. Assume at least one full
  re-render after finding a bug.
- Editor lag is **not** render throughput. Headless batch is much faster per
  frame than the viewport suggests — do not extrapolate the budget from editor
  feel.

### Cut list (in order)

1. Train-on-UE symmetry row (the 2×2 second row) → future work
2. Lumen SW condition (keep HW RT)
3. Depth and 3D box annotators
4. Third seed (report 2, note the limitation)
5. Nav2 → scripted waypoint paths

**Minimum viable artifact:** one USD scene, two engines, frame-aligned,
Gate 1 passing, one gap number.

---

## 12. Hardware and storage budget

**GPU: RTX, 12GB VRAM.** Nothing runs concurrently — Isaac renders, exits, then
training runs. Two separate 12GB budgets.

Isaac VRAM is consumed by: BVH acceleration structures (rebuilt per frame for
moving geometry), fully-resident geometry and textures (ray tracing cannot cull
to frustum or stream by visibility), SimReady 4K PBR texture sets, compiled MDL
materials, PhysX GPU collision state, denoiser and render targets — **plus one
render pass per enabled annotator**.

Mitigations, in order of effect:

1. Run headless (`--no-window`) — frees 2–4GB from viewport, UI compositing and
   editor render targets
2. Enable only the three required annotators
3. Scope the scene to one aisle
4. Use USD **payloads** for prop groups; unload while authoring
5. Reduce texture resolution on props that never approach the camera
6. Editor viewport: RTX Real-Time (never Path Traced), 50% resolution scale

Measure before optimising: `nvidia-smi` during a run, plus Isaac's memory stats
panel. One 8K texture set may be half the problem.

**Disk: plan 300GB free, 400 comfortable.**

| Item | Size |
|---|---|
| Isaac Sim installed | 25–30GB |
| SimReady asset pack (local; or stream from Nucleus) | 50GB+ |
| UE5 + compiled editor + DDC | 100–150GB |
| ROS 2 + PyTorch environment | ~15GB |
| Training frames (5k, JPEG q95) | ~1GB |
| Eval frames (500 × 5, PNG) | 3–4GB |
| **Re-render headroom (3×)** | ~15GB |

Store **training** frames as JPEG q95 — 5–8× smaller, no detection impact. Keep
**eval** frames as PNG: compression artifacts are exactly the high-frequency
between-render difference that could contaminate the comparison. Clear UE's DDC
between major asset changes.

---

## 13. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| USD import mangles MDL materials | Isaac-PT ≠ UE-PT | **Do not fight this.** Document precisely what failed to transfer — materials, lights, units. This section becomes the most credible page in the README |
| Coordinate/scale bug undetected | Every number invalid | Gate 1 on day 7, before bulk rendering |
| Auto-exposure left enabled | Measuring post-process | Explicit control checklist, Gate 2 |
| Unconverged path tracing | Measuring sampler noise | Fixed high sample count, MRQ not SceneCapture |
| Frame-level train/eval split | Inflated scores | Split by trajectory; manifest enforcement |
| Seed reuse across splits | Layout leakage | Reserved eval seed range |
| Model underfits | Weak instrument | COCO-pretrained backbone; trajectory diversity before frame count |
| Isaac OOM at 12GB | Blocked | Headless + scene scoping + annotator pruning |

---

## 14. Future work (README section, not day 12 work)

- **Train-on-UE symmetry row.** Asymmetric gaps would indicate one renderer
  produces intrinsically harder or more varied data.
- **Renderer randomisation as augmentation.** Train on 2–3 conditions, hold out a
  fourth. If cross-render generalisation improves, that is a *technique*, not
  just a table.
- **Setting sweep from a fixed baseline** (one variable off nominal at a time,
  ~10 runs): shadow method (ray-traced / VSM / none), reflection method, Lumen
  quality tiers. Frame as **sensitivity**, not quality — without real
  photographs as an anchor, "which renderer is better" is undefined; a flat
  under-detailed render could top the table simply by being easier.
- **Material randomisation as a separate arm.** "How much renderer gap does
  material randomisation buy back?" — unanswered, and a legitimate follow-up
  provided the matched pair is left intact.
- **Monocular depth as a second task**, riding free on the same data.
- **ONNX inference inside Unreal** — a natural extension given existing UE/ONNX
  experience, though it adds risk and zero experimental value inside two weeks.
- **Live bidirectional sync + XR.** Same USD scene, same ROS topics, opposite
  architectural requirement: baking is correct when reproducibility is the goal,
  live sync is correct when a human is in the loop and interaction latency is the
  point. Being able to explain *why the choice differs* is worth more than either
  project alone.

---

## 15. Repository structure

```
rendergap/
├── README.md                    # result table first, then method
├── isaac/
│   ├── scene/warehouse.usda
│   ├── record_trajectory.py     # Pass 1 — Nav2 drive → USD layer
│   ├── replay_capture.py        # Pass 2A — playback + Replicator
│   └── manifest.json
├── unreal/
│   ├── RenderGap.uproject
│   └── Source/                  # pose walker, stencil masks, MRQ config
├── analysis/
│   ├── to_coco.py
│   ├── train.py
│   ├── evaluate.py              # mAP across all conditions
│   ├── gate_mask_iou.py         # Gate 1
│   └── agreement.py             # flip rate, box drift, confidence delta
├── results/
│   ├── table.md
│   └── figures/
└── docs/
    └── usd_transfer_losses.md   # what OpenUSD failed to carry
```

### README ordering

1. **The result table.** A hiring manager reads it in ten seconds.
2. Side-by-side comparison GIF, same frame across five conditions.
3. Method, one page, with the controlled-variable list.
4. Gate 1 mask-IoU proof — this is the credibility anchor.
5. `usd_transfer_losses.md` — the section only someone who built it could write.
6. Limitations, stated plainly: baked animation, one scene, one detector
   architecture, no real-data anchor.
7. Future work.

---

## 16. What this demonstrates

Stated for the portfolio context, since the audience is hiring managers at
synthetic-data and simulation companies:

- A verified OpenUSD round trip between Isaac Sim and Unreal Engine, with frame
  alignment **proven** by mask IoU rather than asserted. This is the plumbing
  these teams struggle with internally and few candidates can show.
- ROS 2 / Nav2 used for what it is actually for, with a defensible architectural
  reason for where it stops.
- Deterministic, headless, re-runnable batch generation.
- Understanding of what synthetic data is *for* — the experiment exists to make
  the pipeline legible as engineering rather than as a tech demo.

Without the experiment, the pipeline reads as a toy. The gap number is what turns
it into a result.
