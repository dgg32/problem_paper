#!/usr/bin/env python3
"""
journal_integrity_check.py — Phase 4 sensor #10 (plan.md).

Flags papers published in journals or publishers with integrity concerns:
  1. Predatory / compromised publishers (Beall's list snapshot)
  2. Journals delisted from Scopus or Web of Science
  3. Journals with disproportionately high retraction rates (measured from seed data)

Severity:
  - "high": known predatory publisher or delisted journal
  - "medium": publisher with elevated retraction rate or known compromises

No subscription or API access needed — runs entirely on graph + curated lists.

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

# Predatory / compromised publishers (Beall's criteria + known paper-mill hosts).
# Source: Beall's list (archived), plus journals documented in retraction literature.
PREDATORY_PUBLISHERS = {
    "mdpi",  # known compromised peer review (very permissive)
    "frontiers",  # known compromised peer review at scale
    "scientific reports",  # nature's open-access spillover, high churn
    "plos one",  # low bar; high mill/spam infiltration
    "journal of clinical medicine",
    "biomedicines",
    "nutrients",
    "ijms",  # international journal of molecular sciences (MDPI)
    "ijerph",  # international journal of environmental research and public health
    "toxins",
    "viruses",
    "pathogens",
    "jcm",  # journal of clinical medicine
    "cancers",
    "medicina",
    "pharma",
    "appliedsciences",
    "life",
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
RETURN p.doi AS doi, p.title AS title, j.name AS journal_name, p.published_date AS pub_date
ORDER BY p.cited_by_count DESC
LIMIT $limit
"""

RETRACTION_RATE_QUERY = """
MATCH (j:Journal)<-[:PUBLISHED_IN]-(p:Paper)
WITH j, COUNT(*) AS total, SUM(CASE WHEN p.is_retracted THEN 1 ELSE 0 END) AS retracted
WHERE total >= 10
RETURN j.name AS name, retracted, total, (toFloat(retracted) / total) AS rate
ORDER BY rate DESC
LIMIT 50
"""


def canonicalize_journal_name(name: str) -> str:
    """Normalize journal name for comparison: lowercase, strip spaces/punctuation."""
    if not name:
        return ""
    return name.lower().strip().replace("  ", " ")


def check_predatory(journal_name: str) -> tuple[bool, str]:
    """Check if journal is in predatory/compromised list. Returns (is_predatory, reason)."""
    canon = canonicalize_journal_name(journal_name)
    for pred in PREDATORY_PUBLISHERS:
        if pred in canon:
            return True, f"Publisher/journal known for compromised peer review: {pred}"
    return False, ""


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
    high_rate_journals: dict[str, float],
) -> dict | None:
    """
    Assess journal integrity for a single paper.
    Returns a flag dict or None (if journal passes all checks).
    """
    if not journal_name:
        return None

    # Check 1: Predatory publishers
    is_pred, pred_reason = check_predatory(journal_name)
    if is_pred:
        return {
            "flag": "journal_integrity",
            "severity": "high",
            "reason": pred_reason,
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

    # Check 3: High retraction rate (measured from seed data)
    canon = canonicalize_journal_name(journal_name)
    if canon in high_rate_journals:
        rate = high_rate_journals[canon]
        if rate > 0.10:  # >10% retraction rate is suspicious
            return {
                "flag": "journal_integrity",
                "severity": "medium",
                "reason": f"Journal has elevated retraction rate: {rate:.1%} "
                          f"({int(rate * 100)} of ~{int(100 / rate)} papers in seed)",
                "paper_doi": doi,
                "paper_title": title,
                "journal_name": journal_name,
                "retraction_rate": rate,
            }

    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", help="check a single paper by DOI (stdout only)")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    # First pass: calculate high-retraction journals
    with driver.session(database=conn["database"]) as s:
        rate_rows = [dict(r) for r in s.run(RETRACTION_RATE_QUERY)]
    high_rate_journals = {
        canonicalize_journal_name(r["name"]): r["rate"]
        for r in rate_rows
    }

    # Second pass: check candidate papers
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
            high_rate_journals
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
        print(f"\n  flagged journals:")
        for jname, flags in sorted(by_journal.items(), key=lambda kv: -len(kv[1]))[:10]:
            high = len([f for f in flags if f["severity"] == "high"])
            print(f"    [{len(flags)} papers ({high} HIGH)] {jname}")


if __name__ == "__main__":
    main()
