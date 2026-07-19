#!/usr/bin/env python3
"""Lightweight Western blot / gel lane-integrity screen."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List

from integrity_common import IMAGE_EXTS, add_common_args, emit_result, hamming, iter_files, make_finding, make_result


def require_pillow():
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit("Blot/gel screening requires Pillow: python3 -m pip install Pillow") from exc
    return Image


def column_profile(image) -> List[float]:
    gray = image.convert("L")
    width, height = gray.size
    pixels = gray.load()
    return [sum(pixels[x, y] for y in range(height)) / height for x in range(width)]


def detect_boundaries(profile: List[float], z_threshold: float) -> List[int]:
    if len(profile) < 3:
        return []
    diffs = [abs(profile[i + 1] - profile[i]) for i in range(len(profile) - 1)]
    mean_diff = sum(diffs) / len(diffs)
    variance = sum((diff - mean_diff) ** 2 for diff in diffs) / max(len(diffs) - 1, 1)
    sd = variance ** 0.5
    if sd == 0:
        return []
    return [idx for idx, diff in enumerate(diffs) if (diff - mean_diff) / sd >= z_threshold]


def lane_hashes(image, lanes: int) -> List[str]:
    gray = image.convert("L")
    width, height = gray.size
    lane_width = max(width // lanes, 1)
    hashes = []
    for lane in range(lanes):
        left = lane * lane_width
        right = width if lane == lanes - 1 else min((lane + 1) * lane_width, width)
        crop = gray.crop((left, 0, right, height)).resize((8, 16))
        pixels = list(crop.getdata())
        avg = sum(pixels) / len(pixels)
        hashes.append("".join("1" if pixel >= avg else "0" for pixel in pixels))
    return hashes


def run(paths: List[str], lanes: int, seam_z: float, lane_threshold: int) -> Dict[str, object]:
    Image = require_pillow()
    files = iter_files(paths, IMAGE_EXTS)
    findings = []
    for file_path in files:
        with Image.open(file_path) as image:
            profile = column_profile(image)
            boundaries = detect_boundaries(profile, seam_z)
            if len(boundaries) >= 2:
                findings.append(
                    make_finding(
                        method="blot_gel_boundary_screen",
                        severity="MEDIUM" if len(boundaries) < 8 else "HIGH",
                        location=file_path.name,
                        description="Abrupt vertical intensity changes may indicate lane boundaries or splicing candidates.",
                        evidence_files=[str(file_path)],
                        statistics={"boundary_count": len(boundaries), "boundary_positions": boundaries[:25]},
                        recommendation="Inspect the uncropped source image and confirm whether lane rearrangement was disclosed.",
                    )
                )
            hashes = lane_hashes(image, lanes)
            for i, left_hash in enumerate(hashes):
                for j, right_hash in enumerate(hashes[i + 1 :], i + 1):
                    distance = hamming(left_hash, right_hash)
                    if distance <= lane_threshold:
                        findings.append(
                            make_finding(
                                method="blot_gel_repeated_lane_screen",
                                severity="HIGH" if distance <= 3 else "MEDIUM",
                                location=f"{file_path.name}: lane {i + 1} vs lane {j + 1}",
                                description="Two estimated lanes have highly similar intensity patterns.",
                                evidence_files=[str(file_path)],
                                statistics={"hamming_distance": distance, "lanes": [i + 1, j + 1]},
                                recommendation="Check whether lanes are duplicated, adjacent technical replicates, or a valid repeated control.",
                            )
                        )
    return make_result(
        tool="blot_gel_lane_audit",
        input_value=paths,
        findings=findings,
        evidence_files=[str(path) for path in files],
        limitations=["Lane splitting is approximate; use uncropped source scans for final interpretation."],
        metadata={"image_count": len(files), "estimated_lanes": lanes},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen blot/gel images for lane and boundary anomalies.")
    parser.add_argument("paths", nargs="+", help="Blot/gel image files or directories")
    parser.add_argument("--lanes", type=int, default=8, help="Estimated number of lanes for repeated-lane screening")
    parser.add_argument("--seam-z", type=float, default=3.0, help="Z-score threshold for vertical boundary candidates")
    parser.add_argument("--lane-threshold", type=int, default=6, help="Hamming threshold for repeated lane hashes")
    add_common_args(parser)
    args = parser.parse_args()
    emit_result(run(args.paths, args.lanes, args.seam_z, args.lane_threshold), args.format, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
