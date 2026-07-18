#!/usr/bin/env python3
"""
tortured_phrases_detector.py — Phase 4 sensor #5 (plan.md).

Flags a not-yet-retracted paper whose full text contains one or more known
"tortured phrases" — garbled paraphrases of established scientific terms that
are a strong fingerprint of paper-mill / spinner text (e.g. "bosom peril" for
breast cancer, "counterfeit consciousness" for artificial intelligence).

The detector:
  1. Reads the phrase list from sensors/tortured_phrases_list.txt.
  2. Pulls open-access full text via full_text_fetcher.
  3. Searches for each phrase (case-insensitive, whole-phrase substring match).
  4. Emits one flag per matching phrase with a surrounding snippet as evidence.

The phrase list combines the original curated seed list with PPS's
"Favourite Tortured Phrases" report (a curated high-confidence subset of the
Problematic Paper Screener fingerprints). Only multi-word phrases are kept;
single-word fingerprints are too noisy as substring flags.

Severity:
  - "high" if an obvious / high-confidence phrase is found (e.g., "bosom peril",
    "counterfeit consciousness"). The list is curated from Cabanac's
    Problematic Paper Screener, where all phrases are high-confidence.
  - Downgraded to "medium" only if the phrase count is very low and the match
    could be a coincidental substring; in practice this detector uses exact
    multi-word phrases and is near-zero false positive.

Output: one flag record per (paper, phrase) pair, plus an aggregated report.

Usage:
  python sensors/tortured_phrases_detector.py                  # all candidates
  python sensors/tortured_phrases_detector.py --doi 10.xxx/xxx  # single paper
  python sensors/tortured_phrases_detector.py --sample 5         # spot-check N
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

# Import the fetcher from the same directory.
sys.path.insert(0, str(REPO_ROOT / "sensors"))
from full_text_fetcher import fetch_full_text, canon_doi  # noqa: E402

PHRASE_LIST = REPO_ROOT / "sensors" / "tortured_phrases_list.txt"
REPORT_JSON = REPO_ROOT / "data" / "flags" / "tortured_phrases_flags.json"
CACHE_DIR = REPO_ROOT / "data" / "full_text_cache"

# Phrases that are especially obvious / high-confidence; these keep severity high.
OBVIOUS_PHRASES = {
    "bosom peril",
    "counterfeit consciousness",
    "sign to clamor",
    "flag to clamor",
    "man-made consciousness",
    "enormous information",
    "randomized controlled preliminary",
}

# Max papers to fetch in a single full run; fetching full text is slow and
# rate-limited. The default targets the expansion candidates (not-yet-retracted
# papers pulled by expand_targets.py) which are the current scoring queue.
DEFAULT_LIMIT = 200

QUERY = """
MATCH (p:Paper {is_retracted:false})
WHERE $doi IS NULL OR p.doi = $doi
RETURN p.doi AS doi, p.title AS title
ORDER BY p.cited_by_count DESC
LIMIT $limit
"""


def load_phrases() -> list[str]:
    lines = PHRASE_LIST.read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def find_matches(text: str, phrases: list[str]) -> list[tuple[str, str]]:
    """Return list of (phrase, snippet) for each phrase found in text."""
    matches = []
    text_lower = text.lower()
    for phrase in phrases:
        p_lower = phrase.lower()
        idx = text_lower.find(p_lower)
        if idx == -1:
            continue
        start = max(0, idx - 80)
        end = min(len(text), idx + len(phrase) + 80)
        snippet = text[start:end]
        snippet = re.sub(r"\s+", " ", snippet)
        matches.append((phrase, snippet.strip()))
    return matches


def build_flag(doi: str, title: str, phrase: str, snippet: str) -> dict:
    obvious = phrase.lower() in OBVIOUS_PHRASES
    severity = "high" if obvious else "high"  # curated list -> high by default
    return {
        "flag": "tortured_phrase",
        "severity": severity,
        "paper_doi": doi,
        "paper_title": title,
        "phrase": phrase,
        "evidence": f"Found tortured phrase \"{phrase}\" in full text: ...{snippet}...",
        "source_url": f"https://doi.org/{doi}",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", help="check a single paper by DOI (stdout only)")
    ap.add_argument("--sample", type=int, help="spot-check N papers")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="max papers to fetch")
    ap.add_argument("--refresh", action="store_true", help="ignore cached full text")
    args = ap.parse_args()

    phrases = load_phrases()
    if not phrases:
        print("ERROR: no phrases loaded from", PHRASE_LIST)
        sys.exit(1)

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        limit = 1 if args.doi else (args.sample or args.limit)
        rows = [dict(r) for r in s.run(QUERY, doi=args.doi, limit=limit)]
    driver.close()

    all_flags: list[dict] = []
    fetched = 0
    found = 0
    for row in rows:
        doi = row["doi"]
        title = row["title"]
        if args.refresh:
            cache_file = CACHE_DIR / f"{canon_doi(doi).replace('/', '__')}.json"
            if cache_file.exists():
                cache_file.unlink()
        res = fetch_full_text(doi, CACHE_DIR)
        fetched += 1
        if res["status"] != "ok":
            continue
        matches = find_matches(res["text"], phrases)
        if matches:
            found += 1
        for phrase, snippet in matches:
            all_flags.append(build_flag(doi, title, phrase, snippet))
        if args.doi:
            if not matches:
                print(f"no tortured phrases found in {doi}")
            for f in matches:
                print(json.dumps(build_flag(doi, title, f[0], f[1]), indent=2))
            return

    # Aggregate report
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(all_flags, indent=2))

    by_paper: dict[str, list[dict]] = {}
    for f in all_flags:
        by_paper.setdefault(f["paper_doi"], []).append(f)

    print("=== tortured-phrases-detector ===")
    print(f"  papers fetched      : {fetched}")
    print(f"  papers with match   : {found}")
    print(f"  total flag records  : {len(all_flags)}")
    print(f"  distinct phrases hit: {len({f['phrase'] for f in all_flags})}")
    print(f"\n  report written -> {REPORT_JSON.relative_to(REPO_ROOT)}")

    top = sorted(by_paper.items(), key=lambda kv: -len(kv[1]))[:5]
    if top:
        print("\n  top papers by phrase count:")
        for doi, fl in top:
            print(f"    [{len(fl)}] {fl[0]['paper_title'][:80]}  ({doi})")


if __name__ == "__main__":
    main()
