#!/usr/bin/env python3
"""Screen image sets for near-duplicate images and transformed reuse."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List, Tuple

from integrity_common import IMAGE_EXTS, add_common_args, emit_result, hamming, iter_files, make_finding, make_result


def require_pillow():
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise SystemExit("Image screening requires Pillow: python3 -m pip install Pillow") from exc
    return Image, ImageOps


def ahash(image, size: int = 8) -> str:
    gray = image.convert("L").resize((size, size))
    # tobytes() gives one byte per pixel for mode "L" — stable across Pillow
    # versions (avoids the getdata() deprecation slated for Pillow 14). Vendored
    # patch, see NOTICE.md.
    pixels = list(gray.tobytes())
    avg = sum(pixels) / len(pixels)
    return "".join("1" if pixel >= avg else "0" for pixel in pixels)


def variant_hashes(path: Path) -> Dict[str, str]:
    Image, ImageOps = require_pillow()
    with Image.open(path) as image:
        base = image.convert("RGB")
        variants = {
            "original": base,
            "flip_lr": ImageOps.mirror(base),
            "flip_tb": ImageOps.flip(base),
            "rot90": base.rotate(90, expand=True),
            "rot180": base.rotate(180, expand=True),
            "rot270": base.rotate(270, expand=True),
        }
        return {name: ahash(variant) for name, variant in variants.items()}


def run(paths: List[str], threshold: int) -> Dict[str, object]:
    files = iter_files(paths, IMAGE_EXTS)
    hash_map = {str(path): variant_hashes(path) for path in files}
    findings = []
    for i, left in enumerate(files):
        for right in files[i + 1 :]:
            best: Tuple[int, str, str] = (999, "", "")
            for left_variant, left_hash in hash_map[str(left)].items():
                for right_variant, right_hash in hash_map[str(right)].items():
                    distance = hamming(left_hash, right_hash)
                    if distance < best[0]:
                        best = (distance, left_variant, right_variant)
            if best[0] <= threshold:
                severity = "HIGH" if best[0] <= 3 else "MEDIUM"
                findings.append(
                    make_finding(
                        method="image_similarity",
                        severity=severity,
                        location=f"{left.name} vs {right.name}",
                        description="Potential image reuse or transformed reuse candidate.",
                        evidence_files=[str(left), str(right)],
                        statistics={"hamming_distance": best[0], "left_variant": best[1], "right_variant": best[2]},
                        recommendation="Inspect source images and panel labels; confirm whether reuse is valid and disclosed.",
                    )
                )
    return make_result(
        tool="image_similarity_screen",
        input_value=paths,
        findings=findings,
        evidence_files=[str(path) for path in files],
        limitations=["Average-hash screening is coarse; it can miss partial reuse and can flag visually similar but legitimate images."],
        metadata={"image_count": len(files), "threshold": threshold},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen images for potential reuse.")
    parser.add_argument("paths", nargs="+", help="Image files or directories")
    parser.add_argument("--threshold", type=int, default=6, help="Average-hash Hamming distance threshold")
    add_common_args(parser)
    args = parser.parse_args()
    emit_result(run(args.paths, args.threshold), args.format, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
