#!/usr/bin/env python3
"""
retracted_citation_checker.py — Phase 4 sensor #1 (plan.md).

Flags a not-yet-retracted paper that cites a paper this graph already knows
is_retracted. Pure graph fact answerable in Cypher alone -- no LLM judgment
needed, since "does this paper cite is_retracted work" is deterministic.

Severity comes from two orthogonal, evidence-based signals (plan §0: a
retraction is not proof of fraud, so these are kept separate rather than
collapsed into one score):
  - timing: did the citing paper publish AFTER the cited paper's retraction
    date (the authors should have known) vs before/unknown (built on it
    while it still looked legitimate)?
  - the cited paper's retraction reason: a misconduct-signal reason (Paper
    Mill, Fabrication, Compromised Peer Review, Image Manipulation, ...) vs a
    non-misconduct reason (Author Error, Journal/Publisher Error, ...).

Output: one flag record PER (citing paper, cited retracted paper) pair, each
independently verifiable with its own source_url -- matches the
{flag, severity, evidence, source_url} shape from plan.md Phase 4.

Usage:
  python sensors/retracted_citation_checker.py                  # all candidates -> report
  python sensors/retracted_citation_checker.py --doi 10.xxx/xxx  # single paper, stdout only
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

REPORT_JSON = REPO_ROOT / "data" / "flags" / "retracted_citation_flags.json"

# Retraction Watch reason codes (measured against this graph's actual Reason
# nodes) that signal deliberate misconduct rather than honest error or
# publisher error -- plan.md §0 principle 1 + the §4 reason-priority list.
# Citing a paper retracted for one of these is a stronger flag than citing
# one retracted for e.g. "Author Unresponsive" or "Error by Journal/Publisher".
MISCONDUCT_REASONS = {
    "Paper Mill",
    "Compromised Peer Review",
    "Falsification/Fabrication of Data",
    "Falsification/Fabrication of Image",
    "Falsification/Fabrication of Results",
    "Manipulation of Images",
    "Manipulation of Results",
    "Misconduct - Official Investigation(s) and/or Finding(s)",
    "Misconduct by Author",
    "Investigation by ORI",
    "Euphemisms for Misconduct",
    "Computer-Aided Content or Computer-Generated Content",
    "Duplication of/in Image",
}

QUERY = """
MATCH (citing:Paper {is_retracted:false})-[:CITES]->(cited:Paper {is_retracted:true})
WHERE $doi IS NULL OR citing.doi = $doi
OPTIONAL MATCH (cited)-[:RETRACTED_FOR]->(reason:Reason)
WITH citing, cited, collect(DISTINCT reason.code) AS reasons
RETURN citing.doi AS citing_doi, citing.title AS citing_title,
       citing.published_date AS citing_date,
       cited.doi AS cited_doi, cited.title AS cited_title,
       cited.retraction_date AS retraction_date,
       cited.retraction_nature AS retraction_nature,
       reasons
ORDER BY citing.doi
"""


def severity(citing_date, retraction_date, reasons: list[str]) -> tuple[str, bool, bool]:
    """Two orthogonal signals -> a 3-tier severity."""
    cited_after = bool(citing_date and retraction_date and citing_date > retraction_date)
    is_misconduct = bool(set(reasons) & MISCONDUCT_REASONS)
    if cited_after and is_misconduct:
        tier = "high"
    elif cited_after or is_misconduct:
        tier = "medium"
    else:
        tier = "low"
    return tier, cited_after, is_misconduct


def build_flag(row: dict) -> dict:
    tier, cited_after, is_misconduct = severity(
        row["citing_date"], row["retraction_date"], row["reasons"])
    if cited_after:
        when = "after"
    elif row["citing_date"] and row["retraction_date"]:
        when = "before"
    else:
        when = "at an unconfirmed time relative to"
    evidence = (
        f'"{row["citing_title"]}" ({row["citing_doi"]}) cites '
        f'"{row["cited_title"]}" ({row["cited_doi"]}), which was retracted '
        f'{row["retraction_date"] or "(date unknown)"} '
        f'({row["retraction_nature"] or "Retraction"}) for: '
        f'{", ".join(row["reasons"]) or "no reason on record"}. '
        f'Citing paper published {when} the retraction.'
    )
    return {
        "flag": "cites_retracted_paper",
        "severity": tier,
        "citing_paper_doi": row["citing_doi"],
        "citing_paper_title": row["citing_title"],
        "cited_retracted_paper_doi": row["cited_doi"],
        "cited_retracted_paper_title": row["cited_title"],
        "retraction_date": str(row["retraction_date"]) if row["retraction_date"] else None,
        "retraction_reasons": row["reasons"],
        "citing_after_retraction": cited_after,
        "cited_for_misconduct_reason": is_misconduct,
        "evidence": evidence,
        "source_url": f'https://doi.org/{row["cited_doi"]}',
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", help="check a single citing paper by DOI "
                                   "(stdout only, no report write)")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(QUERY, doi=args.doi)]
    driver.close()

    flags = [build_flag(r) for r in rows]

    if args.doi:
        if not flags:
            print(f"no retracted-citation flags for {args.doi}")
        for f in flags:
            print(json.dumps(f, indent=2))
        return

    by_paper: dict[str, list[dict]] = {}
    for f in flags:
        by_paper.setdefault(f["citing_paper_doi"], []).append(f)

    counts = {"high": 0, "medium": 0, "low": 0}
    for f in flags:
        counts[f["severity"]] += 1

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(flags, indent=2))

    print("=== retracted-citation-checker ===")
    print(f"  candidate papers flagged : {len(by_paper)}")
    print(f"  total flag records       : {len(flags)}")
    print(f"    high   : {counts['high']}")
    print(f"    medium : {counts['medium']}")
    print(f"    low    : {counts['low']}")
    print(f"\n  report written -> {REPORT_JSON.relative_to(REPO_ROOT)}")

    top = sorted(by_paper.items(), key=lambda kv: -len(kv[1]))[:5]
    if top:
        print("\n  top candidates by flag count:")
        for doi, fl in top:
            print(f"    [{len(fl)}] {fl[0]['citing_paper_title'][:80]}  ({doi})")


if __name__ == "__main__":
    main()
