#!/usr/bin/env python3
"""
p_value_hacking_detector.py — Phase 4 sensor #2 (plan.md).

Scans a paper's extractable text for exact reported p-values and looks for
one-sided clustering just under the 0.05 significance threshold (the "caliper
test" signature of p-hacking: researchers stop tweaking a model/exclusion
criterion the moment p crosses below .05, producing a hump in [.040,.050) and
a corresponding gap in [.050,.060) that has no natural statistical reason to
exist). Distinct from paperconan (Phase 3): that tool checks arithmetic
consistency of summary statistics in raw data tables (GRIM/GRIMMER); this
checks the distribution of p-values *as reported in the paper's prose*. No
overlap -- see plan.md Phase 3.

Important honesty caveat (plan.md's own guidance: "skill precision... p-value
sensors are noisy; weight them low, always show evidence"): a real caliper
test (Gerber & Malhotra 2008) is a chi-square test over many pooled p-values,
typically hundreds, often across many papers/studies. A single paper usually
reports only a handful of exact p-values, so this sensor does NOT run a
statistical test -- it applies a simple, transparent descriptive threshold
(>=3 values in [.040,.050) vs <=1 in [.050,.060)) and reports the raw
extracted values as evidence. Severity is capped at "medium"; never "high".
Only p-values reported with an exact "=" are used for clustering ("p < 0.05"
threshold-style reporting carries no exploitable number and is recorded as
context only, never counted toward the cluster).

Text source priority (best available text first):
  1. Local converted PDF markdown (pdf_markdown_path on the Paper node) --
     guaranteed real text for papers we've downloaded and converted
     (graph_processing/pdf_to_markdown.py).
  2. full_text_fetcher's open-access lookup (Crossref/Unpaywall/EuropePMC/PMC),
     same as ai_text_tell_detector.py and tortured_phrases_detector.py. Expect
     most candidates to come back not_open_access -- this project's full-text
     coverage is thin (see session status: both the above sensors return 0
     flags on the current graph for the same reason).

Usage:
  python sensors/p_value_hacking_detector.py                  # all candidates
  python sensors/p_value_hacking_detector.py --doi 10.xxx/xxx  # single paper
  python sensors/p_value_hacking_detector.py --sample 5        # spot-check N
"""
from __future__ import annotations

import argparse
import re
import sys
import json
from collections import Counter
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "sensors"))
from full_text_fetcher import fetch_full_text  # noqa: E402

REPORT_JSON = REPO_ROOT / "data" / "flags" / "p_value_hacking_flags.json"
CACHE_DIR = REPO_ROOT / "data" / "full_text_cache"
PDFS_ROOT = REPO_ROOT / "data" / "pdfs"

# Caliper bins around the .05 threshold (Gerber & Malhotra 2008 convention).
LOWER_BIN = (0.040, 0.050)   # just under significance
UPPER_BIN = (0.050, 0.060)   # just over significance
MIN_LOWER_FOR_FLAG = 3
MAX_UPPER_FOR_FLAG = 1

EXACT_P_RE = re.compile(
    r"(?i)\bp\s*(?:[-_]?\s*value)?\s*=\s*(0?\.\d{2,4}|\d\.\d+e-?\d+)"
)
THRESHOLD_P_RE = re.compile(
    r"(?i)\bp\s*(?:[-_]?\s*value)?\s*[<≤]\s*(0?\.\d{1,4})"
)

QUERY = """
MATCH (p:Paper {is_retracted:false})
WHERE $doi IS NULL OR p.doi = $doi
RETURN p.doi AS doi, p.title AS title, p.pdf_markdown_path AS pdf_markdown_path
ORDER BY p.cited_by_count DESC
LIMIT $limit
"""


def get_text(doi: str, pdf_markdown_path: str | None) -> tuple[str, str]:
    """Return (text, source) using the best available source."""
    if pdf_markdown_path:
        md_path = PDFS_ROOT / pdf_markdown_path
        if md_path.exists():
            text = md_path.read_text(errors="ignore")
            if len(text) > 200:
                return text, "local_markdown"

    result = fetch_full_text(doi, cache_dir=CACHE_DIR)
    if result.get("status") == "ok":
        return result.get("text", ""), result.get("source", "unknown")
    return "", ""


def _snippet(text: str, idx: int, span: int) -> str:
    start = max(0, idx - 40)
    end = min(len(text), idx + span + 40)
    return " ".join(text[start:end].split())


def extract_p_values(text: str) -> tuple[list[dict], list[dict]]:
    """Returns (exact_values, threshold_mentions), each a list of {value, evidence}."""
    exact = []
    for m in EXACT_P_RE.finditer(text):
        try:
            val = float(m.group(1))
        except ValueError:
            continue
        if 0.0 < val < 1.0:
            exact.append({"value": val, "evidence": _snippet(text, m.start(), len(m.group(0)))})

    threshold = []
    for m in THRESHOLD_P_RE.finditer(text):
        try:
            val = float(m.group(1))
        except ValueError:
            continue
        if 0.0 < val < 1.0:
            threshold.append({"value": val, "evidence": _snippet(text, m.start(), len(m.group(0)))})

    return exact, threshold


def assess_paper(doi: str, title: str, text: str, text_source: str) -> dict | None:
    exact, threshold = extract_p_values(text)
    if not exact and not threshold:
        return None  # nothing to say either way -- not a "clean" verdict, just no data

    lower = [e for e in exact if LOWER_BIN[0] <= e["value"] < LOWER_BIN[1]]
    upper = [e for e in exact if UPPER_BIN[0] <= e["value"] <= UPPER_BIN[1]]

    # Count DISTINCT values per bin, not raw mentions: papers routinely restate
    # the same headline result across abstract/results/discussion (same test
    # quoted 2-3x), which would otherwise look like a multi-test cluster from a
    # single number. Confirmed live on 10.1371/journal.pone.0213338: 3 mentions
    # of "p = 0.042" turned out to be the same 24/85-controls finding restated
    # in three sentences, not three different tests.
    distinct_lower = {e["value"] for e in lower}
    distinct_upper = {e["value"] for e in upper}

    flags_out = {
        "paper_doi": doi,
        "paper_title": title,
        "text_source": text_source,
        "n_exact_p_values": len(exact),
        "n_threshold_mentions": len(threshold),
        "n_in_lower_bin_040_050": len(lower),
        "n_in_upper_bin_050_060": len(upper),
        "n_distinct_in_lower_bin": len(distinct_lower),
        "n_distinct_in_upper_bin": len(distinct_upper),
    }

    if len(distinct_lower) >= MIN_LOWER_FOR_FLAG and len(distinct_upper) <= MAX_UPPER_FOR_FLAG:
        flags_out.update({
            "flag": "p_value_clustering",
            "severity": "medium",
            "reason": (
                f"{len(distinct_lower)} distinct exact p-value(s) reported in "
                f"[0.040, 0.050) vs only {len(distinct_upper)} in [0.050, 0.060) "
                f"-- one-sided cluster just under the significance threshold. "
                f"Distinct-value counts, so this isn't just the same result "
                f"restated across sections. Still a descriptive pattern on a "
                f"small per-paper sample, NOT a formal caliper/chi-square test; "
                f"treat as a weak, human-checkable lead."
            ),
            "evidence_lower_bin": lower[:10],
            "evidence_upper_bin": upper[:10],
        })
        return flags_out

    # Repeated identical exact p-values across distinct mentions: weak copy/fabrication lead.
    value_counts = Counter(round(e["value"], 4) for e in exact)
    repeats = {v: c for v, c in value_counts.items() if c >= 3}
    if repeats:
        flags_out.update({
            "flag": "p_value_repetition",
            "severity": "low",
            "reason": (
                f"identical exact p-value(s) repeated 3+ times across the text "
                f"({repeats}) -- could be legitimate (same test reused across "
                f"panels) or copy-paste artifact; weak lead only."
            ),
            "evidence": [e for e in exact if round(e["value"], 4) in repeats][:10],
        })
        return flags_out

    return None  # p-values present but no suspicious pattern


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", help="check a single paper by DOI (writes p_value_hacking_flag_count/p_value_hacking_flags to that Paper node)")
    ap.add_argument("--sample", type=int, help="spot-check N papers")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    try:
        with driver.session(database=conn["database"]) as s:
            rows = [dict(r) for r in s.run(
                QUERY,
                doi=args.doi,
                limit=1 if args.doi else (args.sample or 100),
            )]

        all_flags = []
        for i, row in enumerate(rows, 1):
            text, source = get_text(row["doi"], row.get("pdf_markdown_path"))
            if not text:
                continue
            flag = assess_paper(row["doi"], row["title"], text, source)
            if flag:
                all_flags.append(flag)
            if not args.doi and i % 10 == 0:
                print(f"  [{i}/{len(rows)}]", file=sys.stderr)

        if args.doi:
            if not all_flags:
                print(f"no p-value-hacking flags for {args.doi} (checked {len(rows)} paper(s); "
                      f"either no text available or no suspicious pattern found)")
            for f in all_flags:
                print(json.dumps(f, indent=2))
            # Single-paper mode writes straight to this one Paper node -- same
            # p_value_hacking_flag_count/p_value_hacking_flags properties
            # wire_sensor_flags.py writes in the batch path. Only this DOI is
            # touched -- no reset of any other paper's existing flags.
            with driver.session(database=conn["database"]) as s:
                s.run(
                    "MATCH (p:Paper {doi: $doi}) "
                    "SET p.p_value_hacking_flag_count = $count, "
                    "    p.p_value_hacking_flags = $flags_json",
                    doi=args.doi, count=len(all_flags), flags_json=json.dumps(all_flags),
                )
            print(f"\n  wrote p_value_hacking_flag_count={len(all_flags)} to {args.doi}")
            return

        counts = {"medium": 0, "low": 0}
        for f in all_flags:
            counts[f["severity"]] += 1

        REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
        REPORT_JSON.write_text(json.dumps(all_flags, indent=2))

        print("\n=== p-value-hacking-detector ===")
        print(f"  papers scanned      : {len(rows)}")
        print(f"  papers with flags   : {len(all_flags)}")
        print(f"    medium : {counts['medium']}")
        print(f"    low    : {counts['low']}")
        print(f"\n  report written -> {REPORT_JSON.relative_to(REPO_ROOT)}")

        if all_flags:
            top = sorted(all_flags, key=lambda f: -f.get("n_in_lower_bin_040_050", 0))[:5]
            print("\n  top candidates:")
            for f in top:
                print(f"    [{f['severity'].upper()}] {f['paper_title'][:70]}  ({f['paper_doi']})")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
