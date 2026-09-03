#!/usr/bin/env python3
"""
run_content_sensors_on_selection.py — on-demand forensic-layer runner.

Runs the content-based sensors (the 3 full-text detectors + paperconan) on a
reviewer-curated list of DOIs, e.g. the top-scoring candidates from a
metadata-only tier_a_scoring.py pass. Each sensor's --doi mode writes
straight to that one Paper node (see sensors/tortured_phrases_detector.py,
ai_text_tell_detector.py, p_value_hacking_detector.py), so nothing here
touches wire_sensor_flags.py or any paper outside the given list.

Usage:
  python graph_processing/run_content_sensors_on_selection.py --doi-file shortlist.txt
  python graph_processing/run_content_sensors_on_selection.py --doi-file shortlist.txt --skip-paperconan
  python graph_processing/run_content_sensors_on_selection.py --doi-file shortlist.txt --refresh

shortlist.txt: one DOI per line, blank lines skipped (same format as
add_manual_target_by_doi.py's --doi-file).

After this finishes (and after adjudicating any paperconan runs it flags),
re-run graph_processing/tier_a_scoring.py as usual -- it re-queries Neo4j
and runs/*/meta.yaml fresh every time, no separate wiring step needed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
RUNS_DIR = REPO_ROOT / "runs"

CONTENT_SENSOR_CMDS: list[tuple[str, list[str], bool]] = [
    # (name, base argv, supports --refresh today?)
    ("tortured_phrases_detector", [PYTHON, "sensors/tortured_phrases_detector.py", "--doi"], True),
    ("ai_text_tell_detector",     [PYTHON, "sensors/ai_text_tell_detector.py", "--doi"], False),
    ("p_value_hacking_detector",  [PYTHON, "sensors/p_value_hacking_detector.py", "--doi"], False),
]


def run_one(name: str, cmd: list[str], doi: str) -> bool:
    print(f"    -- {name} --", file=sys.stderr)
    result = subprocess.run(cmd + [doi], cwd=REPO_ROOT)
    ok = result.returncode == 0
    if not ok:
        print(f"    !! {name} exited {result.returncode} for {doi} -- logged, continuing", file=sys.stderr)
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi-file", required=True,
                     help="one DOI per line (blank lines skipped) -- e.g. a shortlist "
                          "copied by eye from review/index.html or data/tier_a_triage_full.csv's "
                          "doi column after the metadata-only scoring pass")
    ap.add_argument("--skip-paperconan", action="store_true",
                     help="skip runs/run_paperconan.py (text sensors only)")
    ap.add_argument("--skip-text-sensors", action="store_true",
                     help="skip the 3 full-text sensors (paperconan only)")
    ap.add_argument("--paperconan-images", action="store_true",
                     help="pass --images through to run_paperconan.py")
    ap.add_argument("--refresh", action="store_true",
                     help="ignore cached full text (text sensors that support it) "
                          "and cached data files (--force-fetch to run_paperconan.py)")
    args = ap.parse_args()

    dois = [ln.strip() for ln in Path(args.doi_file).read_text().splitlines() if ln.strip()]
    if not dois:
        sys.exit(f"no DOIs found in {args.doi_file}")

    print(f"=== running content-based sensors on {len(dois)} selected DOI(s) ===", file=sys.stderr)
    results: dict[str, dict[str, bool]] = {}
    for i, doi in enumerate(dois, 1):
        print(f"\n[{i}/{len(dois)}] {doi}", file=sys.stderr)
        results[doi] = {}
        if not args.skip_text_sensors:
            for name, base_cmd, supports_refresh in CONTENT_SENSOR_CMDS:
                cmd = list(base_cmd)
                if args.refresh and supports_refresh:
                    cmd.append("--refresh")
                results[doi][name] = run_one(name, cmd, doi)
        if not args.skip_paperconan:
            pc_cmd = [PYTHON, "runs/run_paperconan.py", doi]
            if args.paperconan_images:
                pc_cmd.append("--images")
            if args.refresh:
                pc_cmd.append("--force-fetch")
            results[doi]["run_paperconan"] = run_one("run_paperconan", pc_cmd, doi)

    print("\n=== summary ===", file=sys.stderr)
    for doi, r in results.items():
        failed = [name for name, ok in r.items() if not ok]
        status = "OK" if not failed else f"FAILED: {', '.join(failed)}"
        print(f"  {doi}: {status}", file=sys.stderr)

    if not args.skip_paperconan:
        needs_adj = []
        for doi in dois:
            meta_path = RUNS_DIR / doi.replace("/", "__") / "meta.yaml"
            if meta_path.exists():
                try:
                    meta = yaml.safe_load(meta_path.read_text()) or {}
                except (yaml.YAMLError, OSError):
                    continue
                if meta.get("needs_adjudication"):
                    needs_adj.append(doi)
        if needs_adj:
            print(f"\n  {len(needs_adj)} paperconan run(s) need MANUAL ADJUDICATION before they'll "
                  f"count toward the score (see runs/<doi>/meta.yaml -- plan.md §0: paperconan "
                  f"output is signal, not verdict; a human/agent must hand-edit `adjudicated:` "
                  f"to needs_human/confirmed/benign/false_positive and write CONCLUSION.md):",
                  file=sys.stderr)
            for doi in needs_adj:
                print(f"    - runs/{doi.replace('/', '__')}/meta.yaml", file=sys.stderr)

    print("\nNext: adjudicate any paperconan runs listed above, then re-run "
          "graph_processing/tier_a_scoring.py to pick up the new evidence "
          "(it re-queries Neo4j + runs/*/meta.yaml fresh every run -- no wiring step needed).",
          file=sys.stderr)


if __name__ == "__main__":
    main()
