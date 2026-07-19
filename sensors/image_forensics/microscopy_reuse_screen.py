#!/usr/bin/env python3
"""Screen microscopy images for repeated fields or local cloned regions."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List, Tuple

from integrity_common import IMAGE_EXTS, add_common_args, emit_result, hamming, iter_files, make_finding, make_result


def require_pillow():
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise SystemExit("Microscopy screening requires Pillow: python3 -m pip install Pillow") from exc
    return Image, ImageOps


def ahash(image, size: int = 8) -> str:
    gray = image.convert("L").resize((size, size))
    pixels = list(gray.getdata())
    avg = sum(pixels) / len(pixels)
    return "".join("1" if pixel >= avg else "0" for pixel in pixels)


def tile_hashes(path: Path, grid: int) -> List[Dict[str, object]]:
    Image, ImageOps = require_pillow()
    records = []
    with Image.open(path) as image:
        base = ImageOps.exif_transpose(image).convert("RGB")
        width, height = base.size
        tile_w = max(width // grid, 1)
        tile_h = max(height // grid, 1)
        for y_idx in range(grid):
            for x_idx in range(grid):
                left = x_idx * tile_w
                upper = y_idx * tile_h
                right = width if x_idx == grid - 1 else min((x_idx + 1) * tile_w, width)
                lower = height if y_idx == grid - 1 else min((y_idx + 1) * tile_h, height)
                crop = base.crop((left, upper, right, lower))
                records.append({"path": str(path), "tile": f"{x_idx},{y_idx}", "box": [left, upper, right, lower], "hash": ahash(crop)})
    return records


def run(paths: List[str], grid: int, threshold: int) -> Dict[str, object]:
    files = iter_files(paths, IMAGE_EXTS)
    tiles = []
    for file_path in files:
        tiles.extend(tile_hashes(file_path, grid))
    findings = []
    for i, left in enumerate(tiles):
        for right in tiles[i + 1 :]:
            if left["path"] == right["path"] and left["tile"] == right["tile"]:
                continue
            distance = hamming(str(left["hash"]), str(right["hash"]))
            if distance <= threshold:
                findings.append(
                    make_finding(
                        method="microscopy_tile_reuse",
                        severity="HIGH" if distance <= 3 else "MEDIUM",
                        location=f"{Path(str(left['path'])).name}:{left['tile']} vs {Path(str(right['path'])).name}:{right['tile']}",
                        description="Microscopy tiles have highly similar low-resolution structure.",
                        evidence_files=[str(left["path"]), str(right["path"])],
                        statistics={"hamming_distance": distance, "left_box": left["box"], "right_box": right["box"]},
                        recommendation="Inspect original microscopy fields for reused views, local cloning, or valid repeated controls.",
                    )
                )
    return make_result(
        tool="microscopy_reuse_screen",
        input_value=paths,
        findings=findings,
        evidence_files=[str(path) for path in files],
        limitations=["Tile hashing is a coarse screen; fluorescence channels, sparse fields, and uniform backgrounds can create false positives."],
        metadata={"image_count": len(files), "tile_count": len(tiles), "grid": grid, "threshold": threshold},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen microscopy images for repeated tiles/fields.")
    parser.add_argument("paths", nargs="+", help="Microscopy image files or directories")
    parser.add_argument("--grid", type=int, default=3, help="Tile grid per image side")
    parser.add_argument("--threshold", type=int, default=4, help="Average-hash Hamming threshold")
    add_common_args(parser)
    args = parser.parse_args()
    emit_result(run(args.paths, args.grid, args.threshold), args.format, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
