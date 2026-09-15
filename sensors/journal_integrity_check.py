#!/usr/bin/env python3
"""
journal_integrity_check.py — Phase 4 sensor #10 (plan.md).

Flags papers published in journals or publishers with integrity concerns:
  1. OA-only-publisher journal NOT indexed in DOAJ (see below — replaces the
     old static predatory-publisher substring guess, 2026-07-19)
  2. Journals delisted from Scopus or Web of Science

Severity:
  - "high": delisted journal
  - "medium": not DOAJ-indexed despite an OA-only publisher

REMOVED 2026-09-15: Check 3 ("journal has an elevated retraction rate,
measured from seed data") was cut entirely, not capped or reweighted. It
computed a journal's retraction rate against a denominator of ONLY the
papers from that journal already in this project's own Retraction-Watch-
seeded graph -- a small, deliberately retraction-biased sample, not that
journal's real publication volume. Measured live before removal: Nature
"50.0% (50 of ~200 papers in seed)" against a real-world rate of 0.035%;
Frontiers in Microbiology "75.0% (75 of ~133 papers in seed)" against
0.049%. Every one of the 225 journal_integrity flags live at removal time
came from this check alone, zero from checks 1/2. It also measured the
exact same underlying fact as journal_retr_rate_external
(graph_processing/journal_retraction_rate_external.py, already scored,
minmax-scaled, denominated against Crossref's real per-journal DOI count)
-- the same redundancy tier_a_scoring.py's WEIGHTS docstring already
documents for the standalone journal_retr_rate signal removed 2026-07-22,
which this check duplicated and outlived. See that docstring and
graph_processing/build_review_page.py's journal-integrity row comment for
the fuller history.

Check 1 redesigned 2026-07-19 (graph_processing/refresh_doaj_status.py):
the old PREDATORY_PUBLISHERS check flagged EVERY journal whose name
contained a substring like "mdpi" or "frontiers" as high-severity
"compromised peer review" -- a blunt guess, not a per-journal fact. Measured
live: 16 of 17 journals it caught are actually properly DOAJ-vetted
(including "Frontiers of Environmental Science & Engineering", an unrelated
Higher Education Press/Springer journal that only shares the word
"Frontiers" with Frontiers Media -- a pure name collision, the exact
failure mode substring matching invites). Now checks the graph's live
j.doaj_indexed fact (from DOAJ's public data dump) instead: still only
meaningful for known OA-only publishers (a legitimately-subscription
journal is correctly absent from DOAJ and that means nothing), but fires
on the SPECIFIC journal's real vetting status, not a name guess. Refresh
the underlying fact with `python graph_processing/refresh_doaj_status.py
--doaj-csv data/doaj_journals.csv` (re-download the CSV from
https://doaj.org/csv periodically; DOAJ updates it weekly).

Usage:
  python sensors/journal_integrity_check.py                  # all candidates
  python sensors/journal_integrity_check.py --doi 10.xxx/xxx  # single paper
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

REPORT_JSON = REPO_ROOT / "data" / "flags" / "journal_integrity_flags.json"

# Known OA-only publishers: journals from these publish open-access exclusively,
# so DOAJ absence is a real (if narrow) signal for them -- unlike a subscription
# journal, which is correctly absent from DOAJ and means nothing. This set
# scopes WHICH journals the DOAJ check applies to; it no longer flags anyone by
# itself (see module docstring -- that was the old, replaced behaviour).
# Matched against the graph's j.publisher field (canonical, from OpenAlex), NOT
# journal title substrings -- title matching is exactly what produced the false
# positive documented above ("Frontiers of Environmental Science &
# Engineering" is Higher Education Press, not Frontiers Media, but its TITLE
# contains "Frontiers"). Publisher field is the fix.
OA_ONLY_PUBLISHERS = {
    "multidisciplinary digital publishing institute",  # MDPI
    "frontiers media",
    "public library of science",  # PLOS
    "biomed central",
    "hindawi",
}

# Journals explicitly delisted from Scopus / Web of Science.
# Source: Scopus/WoS delisting notices, librarian literature.
DELISTED_JOURNALS = {
    "american journal of astrophysics",
    "journal of scientific exploration",
    "journal of cosmology",
    "international journal of advanced research",
    "research journal of pharmaceutical, biological and chemical sciences",
}

QUERY = """
MATCH (p:Paper {is_retracted:false})-[:PUBLISHED_IN]->(j:Journal)
WHERE $doi IS NULL OR p.doi = $doi
RETURN p.doi AS doi, p.title AS title, j.name AS journal_name, j.publisher AS publisher,
       j.doaj_indexed AS doaj_indexed, p.published_date AS pub_date
ORDER BY p.cited_by_count DESC
LIMIT $limit
"""


def canonicalize_journal_name(name: str) -> str:
    """Normalize journal name for comparison: lowercase, strip spaces/punctuation."""
    if not name:
        return ""
    return name.lower().strip().replace("  ", " ")


def check_doaj(journal_name: str, publisher: str, doaj_indexed: bool | None) -> tuple[bool, str]:
    """OA-only publisher whose specific journal isn't DOAJ-indexed. Returns (flagged, reason).
    See module docstring for why this replaced a title-substring predatory-publisher guess."""
    if doaj_indexed is None or doaj_indexed:
        return False, ""  # unknown status, or properly indexed -- no flag either way
    canon_pub = canonicalize_journal_name(publisher)
    if not any(p in canon_pub for p in OA_ONLY_PUBLISHERS):
        return False, ""  # not an OA-only publisher; DOAJ absence means nothing here
    return True, f"{publisher} journal '{journal_name}' is not indexed in DOAJ despite being an OA-only publisher"


def check_delisted(journal_name: str) -> tuple[bool, str]:
    """Check if journal is explicitly delisted. Returns (is_delisted, reason)."""
    canon = canonicalize_journal_name(journal_name)
    for delisted in DELISTED_JOURNALS:
        if delisted in canon:
            return True, f"Journal delisted from Scopus/Web of Science: {delisted}"
    return False, ""


def assess_paper(
    doi: str,
    title: str,
    journal_name: str,
    publisher: str,
    doaj_indexed: bool | None,
) -> dict | None:
    """
    Assess journal integrity for a single paper.
    Returns a flag dict or None (if journal passes all checks).
    """
    if not journal_name:
        return None

    # Check 1: OA-only publisher, specific journal not DOAJ-indexed
    is_flagged, doaj_reason = check_doaj(journal_name, publisher, doaj_indexed)
    if is_flagged:
        return {
            "flag": "journal_integrity",
            "severity": "medium",
            "reason": doaj_reason,
            "paper_doi": doi,
            "paper_title": title,
            "journal_name": journal_name,
        }

    # Check 2: Delisted journals
    is_del, del_reason = check_delisted(journal_name)
    if is_del:
        return {
            "flag": "journal_integrity",
            "severity": "high",
            "reason": del_reason,
            "paper_doi": doi,
            "paper_title": title,
            "journal_name": journal_name,
        }

    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", help="check a single paper by DOI (stdout only)")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(
            QUERY,
            doi=args.doi,
            limit=1000 if not args.doi else 1
        )]
    driver.close()

    all_flags = []
    for row in rows:
        flag = assess_paper(
            row["doi"],
            row["title"],
            row["journal_name"],
            row["publisher"],
            row["doaj_indexed"],
        )
        if flag:
            all_flags.append(flag)

    if args.doi:
        if not all_flags:
            print(f"no journal-integrity flags for {args.doi}")
        for f in all_flags:
            print(json.dumps(f, indent=2))
        return

    # Aggregate report
    by_paper = {f["paper_doi"]: [f] for f in all_flags}
    by_journal = {}
    counts = {"high": 0, "medium": 0}

    for f in all_flags:
        by_journal.setdefault(f["journal_name"], []).append(f)
        counts[f["severity"]] += 1

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(all_flags, indent=2))

    print("\n=== journal-integrity-check ===")
    print(f"  papers scanned       : {len(rows)}")
    print(f"  papers with flags    : {len(by_paper)}")
    print(f"  total flag records   : {len(all_flags)}")
    print(f"    high   : {counts['high']}")
    print(f"    medium : {counts['medium']}")
    print(f"\n  report written -> {REPORT_JSON.relative_to(REPO_ROOT)}")

    if by_journal:
        print("\n  flagged journals:")
        for jname, flags in sorted(by_journal.items(), key=lambda kv: -len(kv[1]))[:10]:
            high = len([f for f in flags if f["severity"] == "high"])
            print(f"    [{len(flags)} papers ({high} HIGH)] {jname}")


if __name__ == "__main__":
    main()
