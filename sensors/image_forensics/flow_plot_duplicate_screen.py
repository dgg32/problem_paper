#!/usr/bin/env python3
"""Screen flow-cytometry plot images for duplicated dot-cloud plots."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List

from integrity_common import IMAGE_EXTS, add_common_args, emit_result, hamming, iter_files, make_finding, make_result


def require_pillow():
    try:
        from PIL import Image, ImageOps, ImageFilter
    except ImportError as exc:
        raise SystemExit("Flow plot screening requires Pillow: python3 -m pip install Pillow") from exc
    return Image, ImageOps, ImageFilter


def plot_hash(path: Path) -> str:
    Image, ImageOps, ImageFilter = require_pillow()
    with Image.open(path) as image:
        gray = ImageOps.exif_transpose(image).convert("L")
        # Emphasize dot-cloud structure over text labels.
        edges = gray.filter(ImageFilter.FIND_EDGES).resize((16, 16))
        pixels = list(edges.getdata())
        avg = sum(pixels) / len(pixels)
        return "".join("1" if pixel >= avg else "0" for pixel in pixels)


def run(paths: List[str], threshold: int) -> Dict[str, object]:
    files = iter_files(paths, IMAGE_EXTS)
    hashes = {str(path): plot_hash(path) for path in files}
    findings = []
    for i, left in enumerate(files):
        for right in files[i + 1 :]:
            distance = hamming(hashes[str(left)], hashes[str(right)])
            if distance <= threshold:
                findings.append(
                    make_finding(
                        method="flow_plot_duplicate_screen",
                        severity="HIGH" if distance <= 8 else "MEDIUM",
                        location=f"{left.name} vs {right.name}",
                        description="Flow-cytometry plot images have highly similar edge/dot-cloud structure.",
                        evidence_files=[str(left), str(right)],
                        statistics={"hamming_distance": distance},
                        recommendation="Check source FCS files, gating hierarchy, controls, and panel labels.",
                    )
                )
    return make_result(
        tool="flow_plot_duplicate_screen",
        input_value=paths,
        findings=findings,
        evidence_files=[str(path) for path in files],
        limitations=["Image-only flow screening cannot validate compensation, gating hierarchy, or FCS-level data."],
        metadata={"image_count": len(files), "threshold": threshold},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen flow-cytometry plot images for duplication.")
    parser.add_argument("paths", nargs="+", help="Flow plot image files or directories")
    parser.add_argument("--threshold", type=int, default=18, help="Edge-hash Hamming threshold")
    add_common_args(parser)
    args = parser.parse_args()
    emit_result(run(args.paths, args.threshold), args.format, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
