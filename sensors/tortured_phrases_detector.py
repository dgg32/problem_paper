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

ROUTINE STATUS (2026-08-17): moved OUT of pipeline_app.py's automatic "Run
all" (Stage now optional=True), same demotion ai_text_tell_detector.py got
on 2026-07-22 and for the same reason -- a near-full-corpus run (--limit 800,
this project's own routine stage) found only 1/794 hits, and every run costs
~30 min of full-text fetching regardless of hit count. The Favourites-list
precision isn't in question (both matches on the one hit were genuine, exact
phrases); the corpus just doesn't have much of this particular tell in it.
Kept runnable manually -- via pipeline_app.py's single-button opt-in, or
directly as below -- for whoever wants a full-corpus sweep occasionally.

--doi is the intended everyday path (2026-08-17): the paperconan-style
"on a hunch" workflow -- a reviewer sees a PubPeer tip, or something reads
oddly, and spot-checks that one paper. Unlike the old stdout-only behaviour,
--doi now ALSO writes tortured_phrase_flag_count / tortured_phrase_flags
straight onto that one Paper node (same properties wire_sensor_flags.py
writes in the batch path), so a manual hit shows up in scoring and the
review page exactly like a routine-run hit would, not just printed once and
lost. Only that single DOI's properties are touched -- no reset of any
other paper, so this is always safe to run standalone between full sweeps.

SCORING: tortured_phrase_flag_count is a scored signal (see WEIGHTS in
tier_a_scoring.py), unlike cabanac_chatgpt_checker.py's deliberately-
unscored calibration list -- this detector's own full-text match is direct,
low-false-positive evidence about THIS paper's own text (an exact multi-word
phrase match, not an ecological/other-paper signal), which is exactly what
plan.md §0 asks a scored point to be.

Usage:
  python sensors/tortured_phrases_detector.py                  # all candidates (--limit 800 for full coverage)
  python sensors/tortured_phrases_detector.py --doi 10.xxx/xxx  # single paper -- prints AND writes graph properties
  python sensors/tortured_phrases_detector.py --sample 5         # spot-check N (stdout report only, no graph write)
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
    severity = "high" if obvious else "medium"  # OBVIOUS_PHRASES are the high-confidence subset
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
    try:
        with driver.session(database=conn["database"]) as s:
            limit = 1 if args.doi else (args.sample or args.limit)
            rows = [dict(r) for r in s.run(QUERY, doi=args.doi, limit=limit)]

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
            paper_flags = [build_flag(doi, title, phrase, snippet) for phrase, snippet in matches]
            all_flags.extend(paper_flags)
            if args.doi:
                if not paper_flags:
                    print(f"no tortured phrases found in {doi}")
                for f in paper_flags:
                    print(json.dumps(f, indent=2))
                # Single-paper mode writes straight to this one Paper node --
                # same tortured_phrase_flag_count/tortured_phrase_flags
                # properties wire_sensor_flags.py writes in the batch path
                # (see module docstring, 2026-08-17), so a manual, hunch-
                # driven run shows up in scoring/review like a routine-run
                # hit would, instead of vanishing once the terminal scrolls.
                # Only this DOI is touched -- no reset of any other paper's
                # existing flags, unlike the batch path's corpus-wide reset.
                with driver.session(database=conn["database"]) as s:
                    s.run(
                        "MATCH (p:Paper {doi: $doi}) "
                        "SET p.tortured_phrase_flag_count = $count, "
                        "    p.tortured_phrase_flags = $flags_json",
                        doi=doi, count=len(paper_flags), flags_json=json.dumps(paper_flags),
                    )
                print(f"\n  wrote tortured_phrase_flag_count={len(paper_flags)} to {doi}")
                return

        # Aggregate report (full/batch runs only -- single-DOI mode returns above)
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
    finally:
        driver.close()


if __name__ == "__main__":
    main()
