r"""How close are the two engines, and is the difference one number or many?

    env\rg-python.bat isaac\analysis\compare_frame.py [--mode MRQ_PathTracer]

--mode picks which Unreal render to read: results/unreal/<mode>, one of
MRQ_PathTracer (default), MRQ_LumenHW, MRQ_LumenSW, MRQ_NoGI.

Reads the linear frame each engine wrote for the same USD camera --
Isaac's hdr.npy and Unreal's MRQ EXR -- and answers, in order of what
invalidates what:

  1. Is the measurement even valid?   clipping and crush on each side
  2. Do the cameras agree?            2D cross-correlation peak shift
  3. Does the geometry agree?         edge-overlap IoU
  4. How much of the difference is a single scalar?   best-fit exposure gain
  5. What is left after that?         PSNR / MAE before and after the gain

Readings 1-3 are the ones that can void everything else: a non-zero
correlation shift means the cameras are not pointed the same way, and no
photometric number after it means anything.

Reading 4 is the deliverable. With exposure matched analytically in
camera_settings.usda, a residual gain far from 1.0 is the light and material
transfer loss, isolated and quantified -- the number that says whether
FRenderGapLuxLightTranslator's PI * SourceArea * SourceArea divisor is wrong.

Everything is compared in LINEAR. Kit's tonemap operators and Unreal's filmic
curve cannot be made byte-identical, so comparing 8-bit sRGB would measure
colour grading -- the exact failure spec section 6.2 warns about. One display
transform is applied here, to both, only for the images a human looks at.

Writes results/analysis/compare_<mode>/: side_by_side.png, difference.png,
histograms.png, summary.json.
Exits non-zero if a gate fails, so it works as a CI step.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# Unreal render modes, one results/unreal/<mode> folder each. First entry is the default.
MODES = ("MRQ_PathTracer", "MRQ_LumenHW", "MRQ_LumenSW", "MRQ_NoGI")
MODE_LABELS = {
    "MRQ_PathTracer": "UE Path Tracer",
    "MRQ_LumenHW": "UE Lumen HW",
    "MRQ_LumenSW": "UE Lumen SW",
    "MRQ_NoGI": "UE No GI",
}

# Fractions above which a frame is too clipped or too crushed to measure through.
MAX_CLIPPED = 0.02
MAX_CRUSHED = 0.20
# Middle grey, the exposure target.
GREY = 0.18


def load_isaac(path):
    npy = os.path.join(path, "hdr.npy")
    if not os.path.exists(npy):
        raise SystemExit(f"[compare] no {npy} -- run isaac\\render_frame.py first")
    return np.load(npy)[:, :, :3].astype(np.float32)


def load_unreal(path):
    exrs = sorted(glob.glob(os.path.join(path, "*.exr")))
    if not exrs:
        raise SystemExit(f"[compare] no .exr under {path} -- run unreal\\render_frame.bat first")
    try:
        import OpenEXR
    except ImportError:
        raise SystemExit("[compare] pip install OpenEXR (needed to read Movie Render Queue output)")

    part = OpenEXR.File(exrs[-1]).parts[0]
    # MRQ writes a single interleaved RGBA channel; older/other writers split R,G,B.
    if "RGBA" in part.channels:
        pixels = part.channels["RGBA"].pixels[:, :, :3]
    elif "RGB" in part.channels:
        pixels = part.channels["RGB"].pixels[:, :, :3]
    else:
        pixels = np.stack([part.channels[c].pixels for c in ("R", "G", "B")], axis=-1)
    return np.ascontiguousarray(pixels).astype(np.float32)


def luma(img):
    return img @ LUMA


def gradient_magnitude(y, floor=1e-7):
    """Edge magnitude in LOG luminance, so it is exposure independent by construction.

    A brightness difference between the engines is a multiplicative gain, which in log
    space is a constant offset -- and cross-correlation subtracts the mean anyway. Doing
    this on linear values instead lets a large exposure gap dominate the gradients and
    turns the alignment reading into a measurement of brightness, which is exactly the
    confusion this whole file exists to avoid.
    """
    v = np.log2(np.maximum(y, floor))
    gx = np.zeros_like(v)
    gy = np.zeros_like(v)
    gx[:, 1:-1] = v[:, 2:] - v[:, :-2]
    gy[1:-1, :] = v[2:, :] - v[:-2, :]
    return np.hypot(gx, gy)


def correlation_shift(a, b, max_shift=24):
    """Peak of the normalised cross-correlation, by FFT. (0, 0) means aligned."""
    a = a - a.mean()
    b = b - b.mean()
    a /= (a.std() + 1e-12)
    b /= (b.std() + 1e-12)
    corr = np.fft.irfft2(np.fft.rfft2(a) * np.conj(np.fft.rfft2(b)), s=a.shape)
    corr = np.fft.fftshift(corr)
    cy, cx = np.array(corr.shape) // 2
    window = corr[cy - max_shift:cy + max_shift + 1, cx - max_shift:cx + max_shift + 1]
    dy, dx = np.unravel_index(np.argmax(window), window.shape)
    return int(dy - max_shift), int(dx - max_shift), float(window.max() / (a.size))


def edge_iou(ga, gb, quantile=90):
    """Agreement of the strongest edges. Threshold per image, so brightness cancels."""
    ma = ga > np.percentile(ga, quantile)
    mb = gb > np.percentile(gb, quantile)
    union = np.logical_or(ma, mb).sum()
    return float(np.logical_and(ma, mb).sum() / union) if union else 0.0


def display(img, gain=1.0):
    """One shared display transform: exposure, Reinhard, sRGB. Never used for numbers."""
    x = np.clip(img * gain, 0.0, None)
    x = x / (1.0 + x)
    x = np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(np.maximum(x, 1e-8), 1 / 2.4) - 0.055)
    return (np.clip(x, 0, 1) * 255).astype(np.uint8)


def save_figures(isaac, unreal, gain, out_dir, label="UE Path Tracer"):
    from PIL import Image, ImageDraw

    os.makedirs(out_dir, exist_ok=True)
    # ONE exposure for both panels, set from Isaac (the control condition). Any brightness
    # difference between the engines therefore SHOWS in this figure.
    #
    # This used to normalise each panel to its own median, which was necessary while the
    # gap was ~2000x -- the Unreal frame was otherwise invisible -- but it also meant the
    # figure looked identical no matter how far apart the two engines were. That is the
    # opposite of what a comparison figure is for. If you need the old behaviour to inspect
    # structure through a large brightness gap, use analysis/preview_exr.py, which states
    # the gain it applied.
    shared = GREY / max(np.median(luma(isaac)), 1e-12)
    a, b = display(isaac, shared), display(unreal, shared)

    h, w = a.shape[:2]
    canvas = Image.new("RGB", (w * 2 + 12, h + 22), (18, 18, 18))
    canvas.paste(Image.fromarray(a), (0, 22))
    canvas.paste(Image.fromarray(b), (w + 12, 22))
    d = ImageDraw.Draw(canvas)
    d.text((4, 6), f"Isaac RTX PT", fill=(235, 235, 235))
    d.text((w + 16, 6), f"{label}", fill=(235, 235, 235))
    canvas.save(os.path.join(out_dir, "side_by_side.png"))

    # Engine-native PNGs are NOT comparable to each other: Isaac writes Kit-tonemapped
    # LdrColor, Unreal writes linear with the tone curve disabled, so they differ by ~3x
    # in 8-bit for identical linear data. These two come from the linear pair through one
    # identical transform, so they are.
    Image.fromarray(a).save(os.path.join(out_dir, "isaac_display.png"))
    Image.fromarray(b).save(os.path.join(out_dir, "unreal_display.png"))

    # Difference after the best-fit gain: what a single scalar cannot explain.
    diff = np.abs(luma(isaac) - luma(unreal) * gain)
    scale = np.percentile(diff, 99) or 1.0
    heat = np.clip(diff / scale, 0, 1)
    rgb = np.stack([heat, heat ** 2.2, np.zeros_like(heat)], axis=-1)
    Image.fromarray((rgb * 255).astype(np.uint8)).save(os.path.join(out_dir, "difference.png"))

    hist_png(isaac, unreal, gain, os.path.join(out_dir, "histograms.png"), label)


def hist_png(isaac, unreal, gain, path, label="UE Path Tracer"):
    """Log-luminance histograms, no matplotlib dependency."""
    from PIL import Image, ImageDraw

    W, H, pad = 720, 260, 34
    img = Image.new("RGB", (W, H), (18, 18, 18))
    d = ImageDraw.Draw(img)
    li, lu = luma(isaac), luma(unreal) * gain
    lo, hi = -14.0, 6.0
    bins = 160

    def curve(y, colour, label, ypos):
        v = np.log2(np.maximum(y, 1e-9))
        counts, _ = np.histogram(np.clip(v, lo, hi), bins=bins, range=(lo, hi))
        counts = counts / max(counts.max(), 1)
        pts = [(pad + i * (W - 2 * pad) / (bins - 1), H - pad - c * (H - 2 * pad))
               for i, c in enumerate(counts)]
        d.line(pts, fill=colour, width=2)
        d.text((pad + 6, ypos), label, fill=colour)

    d.rectangle([pad, pad, W - pad, H - pad], outline=(70, 70, 70))
    curve(li, (120, 200, 255), "Isaac RTX PT", 6)
    curve(lu, (255, 170, 90), f"{label} (x{gain:.4g})", 18)
    d.text((pad, H - pad + 6), f"log2 luminance   {lo:g}", fill=(150, 150, 150))
    d.text((W - pad - 20, H - pad + 6), f"{hi:g}", fill=(150, 150, 150))
    img.save(path)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--isaac", default=os.path.join(REPO, "results", "isaac", "init_frame"))
    p.add_argument("--mode", choices=MODES, default=MODES[0],
                   help="Unreal render mode: reads results/unreal/<mode>, writes "
                        "results/analysis/compare_<mode> (default: %(default)s)")
    p.add_argument("--unreal", default=None,
                   help="override the input folder implied by --mode")
    p.add_argument("--figures", default=None,
                   help="override the output folder implied by --mode")
    args = p.parse_args(argv)

    if args.unreal is None:
        args.unreal = os.path.join(REPO, "results", "unreal", args.mode)
    if args.figures is None:
        args.figures = os.path.join(REPO, "results", "analysis", f"compare_{args.mode}")
    label = MODE_LABELS.get(args.mode, args.mode)

    isaac, unreal = load_isaac(args.isaac), load_unreal(args.unreal)
    print(f"[compare] isaac  {args.isaac}   {isaac.shape}")
    print(f"[compare] unreal {args.unreal}  {unreal.shape}")

    if isaac.shape != unreal.shape:
        print(f"[compare] FAILED -- shape mismatch {isaac.shape} vs {unreal.shape}")
        return 1

    li, lu = luma(isaac), luma(unreal)
    checks = []
    measurable = True

    # 1 -- is the measurement valid at all
    print("\n-- validity ------------------------------------------------------")
    for name, y in (("isaac", li), ("unreal", lu)):
        clipped = float((y >= 1.0).mean())
        crushed = float((y <= 1e-6).mean())
        print(f"   {name:7} median {np.median(y):.6g}   p99 {np.percentile(y, 99):.6g}   "
              f"max {y.max():.6g}   clipped {clipped * 100:.2f}%   crushed {crushed * 100:.2f}%")
        ok_clip, ok_crush = clipped <= MAX_CLIPPED, crushed <= MAX_CRUSHED
        checks.append((f"{name} not clipped past measuring", ok_clip))
        checks.append((f"{name} not crushed past measuring", ok_crush))
        measurable = measurable and ok_clip and ok_crush

    # 2 -- do the cameras agree
    print("\n-- alignment -----------------------------------------------------")
    gi, gu = gradient_magnitude(li), gradient_magnitude(lu)
    dy, dx, peak = correlation_shift(gi, gu)
    print(f"   cross-correlation peak shift   dy {dy:+d} px   dx {dx:+d} px")

    # 3 -- does the geometry agree
    iou = edge_iou(gi, gu)
    print(f"   edge-overlap IoU               {iou:.4f}   (chance is ~0.05 at this threshold)")

    # Only assert on alignment when both frames were actually measurable. A frame that is
    # mostly crushed carries amplified sampling noise where its detail should be, and the
    # correlation then measures that noise -- reporting it as a camera misalignment would
    # be worse than reporting nothing, because it points at the wrong subsystem.
    if measurable:
        checks.append(("cameras aligned (zero shift)", dy == 0 and dx == 0))
        checks.append(("edges agree well above chance", iou > 0.25))
    else:
        print("   NOT MEASURABLE -- one frame is clipped or crushed past the point where")
        print("   its gradients are signal. Fix the exposure gap below, then re-read these.")

    # 4 -- how much is one scalar
    print("\n-- photometry ----------------------------------------------------")
    # Least squares on luminance is dominated by highlights, so use a median ratio -- but
    # only over pixels that carry signal in BOTH frames. A frame with a large crushed block
    # puts the 50th percentile inside that block, where the "ratio" is the float16 noise
    # floor rather than the image: with 41.7% of the Unreal frame at exact zero, the naive
    # median ratio read 35.5x while the p99 and max both said ~125x.
    floor_i = np.percentile(li[li > 0], 5) if np.any(li > 0) else 0.0
    floor_u = np.percentile(lu[lu > 0], 5) if np.any(lu > 0) else 0.0
    mask = (li > floor_i) & (lu > floor_u)
    coverage = float(mask.mean())

    naive = float(np.median(li) / max(np.median(lu), 1e-12))
    if mask.sum() > 1000:
        gain = float(np.median(li[mask] / lu[mask]))
    else:
        gain = naive
        print("   WARNING -- too few pixels carry signal in both frames; falling back")
        print("   to the naive median ratio, which a crushed frame will bias.")

    print(f"   median linear ratio isaac/unreal   {gain:.2f}x   ({np.log2(gain):+.2f} stops)")
    print(f"     over {coverage * 100:.1f}% of pixels with signal in both frames")
    print(f"     (naive whole-frame median ratio {naive:.2f}x -- biased by the crushed block)")

    # 5 -- what a scalar cannot explain
    def psnr(a, b):
        peak_v = max(np.percentile(a, 99), 1e-8)
        mse = float(np.mean((a - b) ** 2))
        return 10 * np.log10(peak_v ** 2 / mse) if mse > 0 else float("inf")

    print(f"   PSNR before gain                   {psnr(li, lu):.2f} dB")
    print(f"   PSNR after  gain                   {psnr(li, lu * gain):.2f} dB")
    print(f"   MAE  after  gain                   {np.mean(np.abs(li - lu * gain)):.6g}")

    save_figures(isaac, unreal, gain, args.figures, label)
    print(f"\n[compare] figures -> {args.figures}")

    summary = {
        "mode": args.mode,
        "isaac": {"median": float(np.median(li)), "p99": float(np.percentile(li, 99)),
                  "max": float(li.max())},
        "unreal": {"median": float(np.median(lu)), "p99": float(np.percentile(lu, 99)),
                   "max": float(lu.max())},
        "shift_px": [dy, dx],
        "edge_iou": iou,
        "median_linear_ratio": gain,
        "naive_median_ratio": naive,
        "signal_coverage": coverage,
        "stops": float(np.log2(gain)),
        "psnr_before_gain_db": float(psnr(li, lu)),
        "psnr_after_gain_db": float(psnr(li, lu * gain)),
    }
    with open(os.path.join(args.figures, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    print()
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
