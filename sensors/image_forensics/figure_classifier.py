#!/usr/bin/env python3
"""figure_classifier.py — heuristic figure-type GATE for the wet-lab image screens.

Why this exists: the specialized screens (blot_gel_lane_audit, microscopy_reuse,
flow_plot_duplicate) assume the input image IS that assay type. Run on the wrong
type they false-positive to BLACK (evaluation: 44 spurious findings from running
the blot audit on ordinary composite figures). This gate decides which screen,
if any, an image may be fed to.

Reality that shapes the design: published figures are frequently multi-panel
COMPOSITES (charts + micrographs + blots in one JPEG), so no pixel classifier can
be exact. So this is deliberately CONSERVATIVE — it greenlights a specialized
screen only when an image is confidently that single type, and skips otherwise:
  - Skipping a real blot (false negative) is SAFE — the generic
    image_similarity_screen still runs on every image regardless.
  - Mis-routing a chart INTO the blot audit (false positive) is the catastrophe
    we must avoid — so ambiguous / white-page / colour images never reach it.

Pillow-only (no OpenCV/numpy). Types:
  chart_or_text   — white-page-dominant (line/bar charts, schematics, word clouds)
  color_image     — colour-dominant non-white content (histology, fluorescence,
                    colour composites)
  grayscale_image — grayscale, not white-dominant (candidate blot / gel / EM)

Routing (screen -> the ONLY type it may run on):
  blot_gel_lane_audit      <- grayscale_image
  microscopy_reuse_screen  <- color_image
  flow_plot_duplicate      <- (none; flow scatter isn't reliably pixel-detectable
                               — left opt-in/manual, never auto-routed)

Usage:
  python figure_classifier.py <dir-or-files> [--format json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

# Tunable thresholds (documented so the gate is transparent, not a black box).
WHITE_FRAC_CHART = 0.50   # >= this fraction near-white => chart/text page
DARK_FRAC_MICRO = 0.40    # >= this fraction near-black => dark-field (fluorescence/EM),
                          #   NOT a blot (blots are light-background) => micrograph
COLOR_FRAC_IMAGE = 0.15   # >= this fraction saturated => colour image
SAT_THRESHOLD = 40        # max-min channel spread above which a pixel is "coloured"
WHITE_LEVEL = 240         # all channels >= this => near-white
DARK_LEVEL = 40           # max channel < this => near-black

# Which specialized screen may run on which type.
ROUTING = {
    "grayscale_image": ["blot_gel_lane_audit"],
    "color_image": ["microscopy_reuse_screen"],
    "chart_or_text": [],
}


def require_pillow():
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("figure_classifier requires Pillow: pip install Pillow") from exc
    return Image


def features(path: Path, sample: int = 128) -> dict:
    Image = require_pillow()
    with Image.open(path) as im:
        rgb = im.convert("RGB")
        rgb.thumbnail((sample, sample))
        raw = rgb.tobytes()  # RGBRGB… — stdlib bytes, no getdata() deprecation
    n = max(1, len(raw) // 3)
    white = dark = colored = 0
    for i in range(0, len(raw) - 2, 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        if r >= WHITE_LEVEL and g >= WHITE_LEVEL and b >= WHITE_LEVEL:
            white += 1
        elif max(r, g, b) < DARK_LEVEL:
            dark += 1
        if max(r, g, b) - min(r, g, b) > SAT_THRESHOLD:
            colored += 1
    return {"white_frac": white / n, "dark_frac": dark / n, "colored_frac": colored / n}


def classify(path: Path) -> dict:
    f = features(path)
    if f["white_frac"] >= WHITE_FRAC_CHART:
        kind = "chart_or_text"
    elif f["dark_frac"] >= DARK_FRAC_MICRO:
        # dark-background => fluorescence / EM / darkfield micrograph, never a blot
        kind = "color_image"
    elif f["colored_frac"] >= COLOR_FRAC_IMAGE:
        kind = "color_image"
    else:
        kind = "grayscale_image"
    return {"file": path.name, "type": kind, "allowed_screens": ROUTING[kind],
            **{k: round(v, 3) for k, v in f.items()}}


def iter_images(paths):
    out = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            out += [q for q in sorted(pp.iterdir()) if q.suffix.lower() in IMAGE_EXTS]
        elif pp.suffix.lower() in IMAGE_EXTS:
            out.append(pp)
    return out


def route(paths) -> dict:
    """Returns {screen: [image paths]} — each image fed only to its allowed screen."""
    buckets: dict[str, list[str]] = {}
    classified = [classify(p) for p in iter_images(paths)]
    for c, p in zip(classified, iter_images(paths)):
        for screen in c["allowed_screens"]:
            buckets.setdefault(screen, []).append(str(p))
    return {"classified": classified, "routing": buckets}


def main() -> int:
    ap = argparse.ArgumentParser(description="Classify figures and route to wet-lab screens.")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--format", choices=["json", "text"], default="text")
    args = ap.parse_args()
    result = route(args.paths)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        for c in result["classified"]:
            print(f'{c["type"]:16} white={c["white_frac"]:.2f} color={c["colored_frac"]:.2f} '
                  f'dark={c["dark_frac"]:.2f}  {c["file"]}  -> {c["allowed_screens"] or "skip"}')
        print("\nrouting:", {k: len(v) for k, v in result["routing"].items()} or "nothing routed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
