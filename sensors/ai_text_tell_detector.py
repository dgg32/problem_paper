#!/usr/bin/env python3
"""
ai_text_tell_detector.py — Phase 4 sensor #6 (plan.md).

Flags a not-yet-retracted paper whose extractable text contains obvious
AI-generation giveaways left in published text. High precision; near-zero
false positives.

Patterns detected (case-insensitive):
  - "As an AI language model" / "As a language model" / "I'm an AI"
  - "Regenerate response" / "Regenerate the response"
  - "Certainly, here is" / "Here is the [X] you requested"
  - "I don't have access to" / "I'm unable to provide" / "I cannot provide"
  - "my last knowledge update" / "my training data"
  - "However, I should note that I am an AI"
  - "As a large language model"
  - "I apologize, but I'm an AI"
  - Similar apologetic/cautious AI-characteristic phrases

Severity:
  - "high": obvious, unambiguous AI-generation giveaway (e.g., "As an AI language model")
  - "medium": contextual AI-tell (e.g., "I don't have access to" in a results section)

Output: one flag per (paper, pattern) pair + aggregated report.

Usage:
  python sensors/ai_text_tell_detector.py                  # all candidates
  python sensors/ai_text_tell_detector.py --doi 10.xxx/xxx  # single paper
  python sensors/ai_text_tell_detector.py --sample 5         # spot-check N
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "sensors"))
from full_text_fetcher import fetch_full_text, canon_doi  # noqa: E402

REPORT_JSON = REPO_ROOT / "data" / "flags" / "ai_text_tell_flags.json"
CACHE_DIR = REPO_ROOT / "data" / "full_text_cache"

# AI-generation patterns grouped by confidence/severity.
# These are substrings (not regex) for speed and clarity; all checked case-insensitive.
HIGH_CONFIDENCE_PATTERNS = [
    "as an ai language model",
    "as a language model, i",
    "as an ai, i",
    "i'm an ai",
    "i am an ai",
    "as a large language model",
    "as an llm,",
    "regenerate response",
    "regenerate the response",
    "regenerate the text",
    "my last knowledge update",
    "my training data ends",
    "my training data cutoff",
    "trained on data up to",
    "however, i should note that i am an ai",
    "as an artificial intelligence",
]

MEDIUM_CONFIDENCE_PATTERNS = [
    "i don't have access to",
    "i cannot access",
    "i'm unable to provide",
    "i cannot provide",
    "i'm unable to generate",
    "i cannot generate",
    "i don't generate",
    "i apologize, but i'm an ai",
    "i should clarify that i'm an ai",
    "as an ai, i cannot",
    "as an ai, i cannot",
    "i'm not able to",
    "certainly, here is",
    "here is the response",
    "here is the answer",
    "here's the answer",
]

QUERY = """
MATCH (p:Paper {is_retracted:false})
WHERE $doi IS NULL OR p.doi = $doi
RETURN p.doi AS doi, p.title AS title
ORDER BY p.cited_by_count DESC
LIMIT $limit
"""


def normalize_text(text: str) -> str:
    """Normalize text for pattern matching: lowercase, collapse whitespace."""
    return " ".join(text.lower().split())


def assess_text(text: str, paper_doi: str, paper_title: str) -> list[dict]:
    """
    Scan text for AI-generation patterns. Returns list of flags (0+ per paper).
    """
    if not text or len(text) < 100:
        return []  # Not enough text to assess

    norm = normalize_text(text)
    flags = []

    for pattern in HIGH_CONFIDENCE_PATTERNS:
        if pattern in norm:
            # Find a surrounding snippet for evidence
            idx = norm.find(pattern)
            start = max(0, idx - 50)
            end = min(len(norm), idx + len(pattern) + 50)
            snippet = norm[start:end]
            flags.append({
                "severity": "high",
                "pattern": pattern,
                "evidence": snippet,
                "paper_doi": paper_doi,
                "paper_title": paper_title,
            })

    for pattern in MEDIUM_CONFIDENCE_PATTERNS:
        # Skip if already flagged high
        if any(f["pattern"] == pattern for f in flags):
            continue
        if pattern in norm:
            idx = norm.find(pattern)
            start = max(0, idx - 50)
            end = min(len(norm), idx + len(pattern) + 50)
            snippet = norm[start:end]
            flags.append({
                "severity": "medium",
                "pattern": pattern,
                "evidence": snippet,
                "paper_doi": paper_doi,
                "paper_title": paper_title,
            })

    return flags


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", help="check a single paper by DOI (stdout only)")
    ap.add_argument("--sample", type=int, help="spot-check N random papers")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(
            QUERY,
            doi=args.doi,
            limit=args.sample or 100
        )]
    driver.close()

    all_flags = []
    for i, row in enumerate(rows, 1):
        result = fetch_full_text(row["doi"], cache_dir=CACHE_DIR)
        if result.get("status") != "ok":
            continue  # Skip if no text available

        text = result.get("text", "")
        flags = assess_text(text, row["doi"], row["title"])
        all_flags.extend(flags)

        if not args.doi and i % 10 == 0:
            print(f"  [{i}/{len(rows)}]", file=sys.stderr)

    if args.doi:
        if not all_flags:
            print(f"no ai-text-tell flags for {args.doi}")
        for f in all_flags:
            print(json.dumps(f, indent=2))
        return

    # Aggregate for report
    by_paper: dict[str, list[dict]] = {}
    counts = {"high": 0, "medium": 0}
    for f in all_flags:
        by_paper.setdefault(f["paper_doi"], []).append(f)
        counts[f["severity"]] += 1

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(all_flags, indent=2))

    print("\n=== ai-text-tell-detector ===")
    print(f"  papers scanned      : {len(rows)}")
    print(f"  papers with flags   : {len(by_paper)}")
    print(f"  total flag records  : {len(all_flags)}")
    print(f"    high   : {counts['high']}")
    print(f"    medium : {counts['medium']}")
    print(f"\n  report written -> {REPORT_JSON.relative_to(REPO_ROOT)}")

    if by_paper:
        top = sorted(by_paper.items(), key=lambda kv: -len(kv[1]))[:5]
        print("\n  top candidates by flag count:")
        for doi, fl in top:
            high = len([f for f in fl if f["severity"] == "high"])
            print(f"    [{len(fl)} ({high} HIGH)] {fl[0]['paper_title'][:70]}  ({doi})")


if __name__ == "__main__":
    main()
