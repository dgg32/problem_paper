#!/usr/bin/env python3
"""
add_paper_by_doi.py — onboard a single new DOI into the graph, then run the
"usual" sensors + scoring against it, EXCLUDING every full-text-based sensor.

Two phases:

  1. Graph ingestion (fast, ~seconds): fetches the paper from OpenAlex by DOI,
     verifies title against Crossref, creates its Paper/AuthorInstance/WROTE/
     AFFILIATED_WITH/PUBLISHED_IN/CITES graph structure -- all via
     add_manual_target_by_doi.py (this script is a thin wrapper around it,
     reused as-is rather than reimplemented, since it already does exactly
     this for a DOI list; see that module for the fetch/verify/write details).
     Skips cleanly if the DOI is already in the graph or is itself retracted.

  2. Sensors + scoring (slower, several minutes): reruns the SAME full-corpus
     scripts the routine pipeline uses (review/pipeline_app.py's STAGES) --
     there is no cheaper "just this one DOI" mode for most of these without
     deep surgery, so this reruns the real thing. The new paper is picked up
     automatically since every one of these already scans ALL
     `Paper {is_retracted:false}` candidates.

     EXCLUDED, on purpose:
       - ai_text_tell_detector.py, p_value_hacking_detector.py,
         tortured_phrases_detector.py -- all three fetch and scan FULL TEXT
         (~30 min each over the whole corpus); this script's whole point is
         to skip that cost for a quick single-paper add. Run them manually
         later (each supports --doi for a single-paper spot-check, though
         note that mode is stdout-only and does NOT write the graph -- see
         each script's own --doi help text) if full-text analysis is wanted.
       - reference_integrity_checker.py -- already excluded from routine
         reruns project-wide (2026-07-20: ~71% false-positive rate on this
         corpus + ~2 HOURS runtime; see plan.md and tier_a_scoring.py's
         WEIGHTS comment). Same reasoning applies here, doubly so for a
         quick single-paper add.
       - The identity-pipeline build/cluster steps (build_instances,
         apply_overrides, expand_targets, link_instances, cluster_instances,
         mark_adjudication) -- those are for bulk (re)builds from the seed
         CSV or ORCID-based expansion, not for onboarding one already-fetched
         paper. Phase 1 above creates this paper's AuthorInstance/WROTE/
         AFFILIATED_WITH edges directly via add_manual_target_by_doi.py.
         (coauthor_other_misconduct and PROBABLY_SAME_AS clustering are
         cluster-based; a newly added author instance without a cluster_id
         won't get a coauthor_other_misconduct value until cluster_instances.py
         is next run as part of a full identity-pipeline rebuild -- expected
         and fine for a quick single-paper add, not silently wrong.)

Usage:
  python graph_processing/add_paper_by_doi.py --doi 10.1000/xyz123
  python graph_processing/add_paper_by_doi.py --doi 10.1000/xyz123 --skip-sensors  # graph only, no rerun
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

# Mirrors review/pipeline_app.py's STAGES (phase4 + scoring categories), minus
# the exclusions documented in the module docstring above. Order matters:
# sensors that write data/flags/*.json must run before wire_sensor_flags.py
# imports them into Neo4j properties; scoring must run after all graph
# properties are updated.
SENSOR_STAGES: list[tuple[str, list[str]]] = [
    ("refresh_retraction_status", [PYTHON, "graph_processing/refresh_retraction_status.py"]),
    ("refresh_editorial_notices", [PYTHON, "graph_processing/refresh_editorial_notices.py"]),
    ("refresh_ori_findings", [PYTHON, "graph_processing/refresh_ori_findings.py"]),
    ("retracted_citation_checker", [PYTHON, "sensors/retracted_citation_checker.py"]),
    ("external_retracted_citation_checker", [PYTHON, "sensors/external_retracted_citation_checker.py"]),
    ("journal_integrity_check", [PYTHON, "sensors/journal_integrity_check.py"]),
    ("pubpeer_comment_checker", [PYTHON, "sensors/pubpeer_comment_checker.py"]),
    ("wire_sensor_flags", [PYTHON, "graph_processing/wire_sensor_flags.py"]),
    ("institution_retraction_rate", [PYTHON, "graph_processing/institution_retraction_rate.py"]),
    ("refresh_correction_history", [PYTHON, "graph_processing/refresh_correction_history.py"]),
    ("publisher_retraction_rate", [PYTHON, "graph_processing/publisher_retraction_rate.py"]),
    ("country_retraction_rate", [PYTHON, "graph_processing/country_retraction_rate.py"]),
    ("journal_retraction_rate_external", [PYTHON, "graph_processing/journal_retraction_rate_external.py"]),
    ("author_retraction_rate_external", [PYTHON, "graph_processing/author_retraction_rate_external.py"]),
    ("gds_node_classification", [PYTHON, "graph_processing/gds_node_classification.py"]),
    ("tier_a_scoring", [PYTHON, "graph_processing/tier_a_scoring.py", "--top", "500",
                        "-o", "data/tier_a_triage_full.csv"]),
    ("build_review_page", [PYTHON, "graph_processing/build_review_page.py", "--top", "795"]),
]


def run_stage(name: str, cmd: list[str]) -> bool:
    print(f"\n=== {name} ===", file=sys.stderr)
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        print(f"!! {name} exited {result.returncode} -- stopping", file=sys.stderr)
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", required=True, help="the DOI to add, e.g. 10.1000/xyz123")
    ap.add_argument("--skip-sensors", action="store_true",
                     help="only add the paper + its connections to the graph; don't run sensors/scoring")
    args = ap.parse_args()

    doi = args.doi.strip()
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(doi + "\n")
        doi_file = f.name

    print(f"=== add_manual_target_by_doi (fetching {doi}) ===", file=sys.stderr)
    try:
        result = subprocess.run(
            [PYTHON, "graph_processing/add_manual_target_by_doi.py",
             "--doi-file", doi_file, "--label", doi],
            cwd=REPO_ROOT,
        )
    finally:
        Path(doi_file).unlink(missing_ok=True)

    if result.returncode != 0:
        print("!! add_manual_target_by_doi failed -- stopping", file=sys.stderr)
        sys.exit(1)

    if args.skip_sensors:
        print("\n--skip-sensors: graph updated (or skipped, see counts above), no sensors run.",
              file=sys.stderr)
        return

    print(f"\nRunning {len(SENSOR_STAGES)} usual sensor/scoring stages over the FULL "
          "not-yet-retracted candidate pool (full-text sensors and reference_integrity_checker "
          "excluded -- see this script's module docstring for why). This reruns the real "
          "pipeline stages, so expect several minutes, not a quick per-DOI operation.",
          file=sys.stderr)
    for name, cmd in SENSOR_STAGES:
        if not run_stage(name, cmd):
            sys.exit(1)

    print(f"\nDone. {doi} is in the graph, scored, and reflected in review/index.html.", file=sys.stderr)
    print("Not run (full-text-based, or already excluded from routine reruns project-wide): "
          "ai_text_tell_detector.py, p_value_hacking_detector.py, tortured_phrases_detector.py, "
          "reference_integrity_checker.py. Run any of these manually if you want that analysis "
          "for this paper.", file=sys.stderr)


if __name__ == "__main__":
    main()
