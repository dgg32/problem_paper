#!/usr/bin/env python3
"""
tier_a_scoring.py — Tier-A heuristic scoring engine (plan.md §2.2).

Combines Phase 4 sensor outputs into a weighted triage score.

Scoring logic:
  - retracted_citation_flag_count: weight 3.0 (citing known-retracted work is very suspicious)
  - reference_integrity_flag_count: weight 1.5 (unresolvable references suggest fabrication)
  - journal_integrity_flag_count: weight 1.0 (publishing in compromised journals)
  - ai_text_tell_flag_count: weight 2.0 (obvious AI generation is suspicious)
  - p_value_hacking_flag_count: weight 0.5 (lowest weight -- plan.md explicitly
    flags this sensor as noisy given the small per-paper p-value sample; see
    sensors/p_value_hacking_detector.py docstring)
  - pubmed_eoc_flag: weight 2.5 (Expression of Concern -- a formal, dated,
    journal-issued fact, not a community opinion; found via PubMed's
    CommentsCorrectionsList, see graph_processing/refresh_editorial_notices.py.
    NOTE: 166 of 227 EoC papers found 2026-07-19 are one coordinated mass
    action by a single journal on a single ethics-committee investigation --
    each is still individually, formally EoC'd (a real per-paper fact), but
    expect the top of the ranking to cluster on that journal as a result.)
  - pubmed_erratum_flag: weight 0.3 (weak signal -- most errata are benign
    corrections, not integrity-relevant; kept low deliberately)
  - ori_finding_flag: weight 4.0 (HIGHEST weight in the system -- a federal
    Office of Research Integrity finding naming this exact paper by DOI,
    via Federal Register "Findings of Research Misconduct" notices; see
    graph_processing/refresh_ori_findings.py. The single strongest fact-based
    signal available: not a proxy, not a community opinion, an adjudicated
    government finding. Measured 2026-07-19: only 2 papers in the whole graph
    matched, both already-retracted -- the machinery is in place for future
    re-runs as ORI publishes new findings, but don't expect this to move the
    current ranking much.)

Score = sum of (flag_count * weight) for each sensor.

Output: CSV with top-N papers sorted by score, including:
  - DOI, title, journal
  - Score components (per-sensor counts)
  - Total score
  - Flag details (JSON for human review)

Usage:
  python graph_processing/tier_a_scoring.py [--top N] [--min-score S]

Examples:
  python graph_processing/tier_a_scoring.py --top 50              # top 50 by score
  python graph_processing/tier_a_scoring.py --min-score 5 --top 100  # all with score>=5, up to 100
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

# Scoring weights. Two families, both explainable (plan.md §0: every point maps
# to a flag):
#   sensor flags  — per-paper Phase-4 sensor hits
#   graph features — Neo4j-derived, explainable (co-author misconduct proximity,
#                    journal retraction rate)
# gds_misconduct_prob is deliberately NOT weighted in — it is a weak, capped,
# domain-shifted learned prior (see gds_node_classification.py) and rides along
# only as a labeled secondary column for the reviewer.
WEIGHTS = {
    # sensor flags
    "retracted_citation_flag_count": 3.0,
    "reference_integrity_flag_count": 1.5,
    "journal_integrity_flag_count": 1.0,
    "ai_text_tell_flag_count": 2.0,
    "p_value_hacking_flag_count": 0.5,
    "pubmed_eoc_flag": 2.5,
    "pubmed_erratum_flag": 0.3,
    "ori_finding_flag": 4.0,
    # graph features (Neo4j)
    "coauthor_other_misconduct": 1.5,   # per probable-person co-author with a misconduct paper elsewhere
    "journal_retr_rate": 2.0,           # rate in [0,1]; granular complement to the journal flag
}

# Rank ALL not-yet-retracted candidates. Nearly every one has some graph signal
# (59% have a misconduct-history co-author), so this is a full triage ordering,
# not just the sensor-flagged subset.
QUERY = """
MATCH (p:Paper {is_retracted:false})-[:PUBLISHED_IN]->(j:Journal)
RETURN p.doi AS doi,
       p.title AS title,
       j.name AS journal,
       p.published_date AS published_date,
       p.cited_by_count AS cited_by_count,
       coalesce(p.retracted_citation_flag_count, 0) AS ret_count,
       coalesce(p.reference_integrity_flag_count, 0) AS ref_count,
       coalesce(p.journal_integrity_flag_count, 0) AS journal_count,
       coalesce(p.ai_text_tell_flag_count, 0) AS ai_count,
       coalesce(p.p_value_hacking_flag_count, 0) AS pval_count,
       CASE WHEN p.pubmed_eoc_status = "expression_of_concern" THEN 1 ELSE 0 END AS eoc_flag,
       CASE WHEN p.pubmed_eoc_status = "erratum_only" THEN 1 ELSE 0 END AS erratum_flag,
       p.pubmed_eoc_date AS eoc_date,
       p.pubmed_eoc_source_doi AS eoc_source_doi,
       CASE WHEN p.ori_finding_doc_url IS NOT NULL THEN 1 ELSE 0 END AS ori_flag,
       p.ori_finding_doc_url AS ori_doc_url,
       p.ori_respondent_name AS ori_respondent,
       coalesce(p.coauthor_other_misconduct, 0) AS coauthor_misconduct,
       coalesce(p.journal_retr_rate, 0.0) AS journal_retr_rate,
       p.gds_misconduct_prob AS gds_prob,
       p.retracted_citation_flags AS ret_flags,
       p.reference_integrity_flags AS ref_flags,
       p.journal_integrity_flags AS journal_flags,
       p.ai_text_tell_flags AS ai_flags,
       p.p_value_hacking_flags AS pval_flags
"""


def calculate_score(row: dict) -> float:
    """Weighted explainable score (sensor flags + graph features)."""
    return (
        row["ret_count"] * WEIGHTS["retracted_citation_flag_count"] +
        row["ref_count"] * WEIGHTS["reference_integrity_flag_count"] +
        row["journal_count"] * WEIGHTS["journal_integrity_flag_count"] +
        row["ai_count"] * WEIGHTS["ai_text_tell_flag_count"] +
        row["pval_count"] * WEIGHTS["p_value_hacking_flag_count"] +
        row["eoc_flag"] * WEIGHTS["pubmed_eoc_flag"] +
        row["erratum_flag"] * WEIGHTS["pubmed_erratum_flag"] +
        row["ori_flag"] * WEIGHTS["ori_finding_flag"] +
        row["coauthor_misconduct"] * WEIGHTS["coauthor_other_misconduct"] +
        row["journal_retr_rate"] * WEIGHTS["journal_retr_rate"]
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=50, help="return top N papers (default: 50)")
    ap.add_argument("--min-score", type=float, default=0, help="minimum score threshold (default: 0)")
    ap.add_argument("--output", "-o", help="CSV output file (default: stdout)")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(QUERY)]
    driver.close()

    # Calculate scores
    results = []
    for row in rows:
        score = calculate_score(row)

        if score < args.min_score:
            continue

        gds_prob = row["gds_prob"]
        results.append({
            "doi": row["doi"],
            "title": row["title"],
            "journal": row["journal"],
            "published_date": row["published_date"],
            "cited_by_count": row["cited_by_count"],
            "score": round(score, 2),
            "retracted_citation_count": row["ret_count"],
            "reference_integrity_count": row["ref_count"],
            "journal_integrity_count": row["journal_count"],
            "ai_text_tell_count": row["ai_count"],
            "p_value_hacking_count": row["pval_count"],
            "expression_of_concern": "Y" if row["eoc_flag"] else "",
            "eoc_date": row["eoc_date"] or "",
            "eoc_source_doi": row["eoc_source_doi"] or "",
            "erratum_only": "Y" if row["erratum_flag"] else "",
            "ori_finding": "Y" if row["ori_flag"] else "",
            "ori_doc_url": row["ori_doc_url"] or "",
            "ori_respondent": row["ori_respondent"] or "",
            "coauthor_misconduct": row["coauthor_misconduct"],
            "journal_retr_rate": round(row["journal_retr_rate"], 3),
            # secondary, labeled, NOT in score:
            "gds_misconduct_prob": round(gds_prob, 3) if gds_prob is not None else "",
            "gds_flagged": "Y" if (gds_prob is not None and gds_prob >= 0.5) else "",
        })

    # Sort by score descending
    results.sort(key=lambda r: r["score"], reverse=True)
    results = results[:args.top]

    # Write output
    if not results:
        print("No papers meet the score threshold.", file=sys.stderr)
        return

    output_file = Path(args.output) if args.output else None
    fieldnames = [
        "rank",
        "score",
        "doi",
        "title",
        "journal",
        "published_date",
        "cited_by_count",
        # sensor flags
        "retracted_citation_count",
        "reference_integrity_count",
        "journal_integrity_count",
        "ai_text_tell_count",
        "p_value_hacking_count",
        "expression_of_concern",
        "eoc_date",
        "eoc_source_doi",
        "erratum_only",
        "ori_finding",
        "ori_doc_url",
        "ori_respondent",
        # graph features (in score)
        "coauthor_misconduct",
        "journal_retr_rate",
        # secondary learned prior (NOT in score)
        "gds_misconduct_prob",
        "gds_flagged",
    ]

    writer = None
    try:
        if output_file:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            f = output_file.open("w", newline="")
        else:
            f = sys.stdout

        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for rank, result in enumerate(results, 1):
            row_dict = {"rank": rank, **result}
            writer.writerow(row_dict)

        if output_file:
            print(f"Wrote {len(results)} papers to {output_file}", file=sys.stderr)
        else:
            print(f"\n[{len(results)} papers scored and ranked]", file=sys.stderr)

    finally:
        if output_file and writer:
            f.close()

    # Summary
    gds_flagged = sum(1 for r in results if r["gds_flagged"] == "Y")
    print(f"\n=== Tier-A Scoring Summary ===", file=sys.stderr)
    print(f"Candidates ranked   : {len(results)}", file=sys.stderr)
    print(f"Score range         : {results[-1]['score']:.1f} — {results[0]['score']:.1f}", file=sys.stderr)
    print(f"Average score       : {sum(r['score'] for r in results) / len(results):.1f}", file=sys.stderr)
    print(f"GDS-flagged in view : {gds_flagged} (secondary learned prior, not in score)", file=sys.stderr)
    print(f"\nWeights (explainable score):", file=sys.stderr)
    for sensor, weight in WEIGHTS.items():
        print(f"  {sensor}: {weight}", file=sys.stderr)

    # Top 5 by score
    print(f"\nTop 5 by score:", file=sys.stderr)
    for i, r in enumerate(results[:5], 1):
        gds = f" | GDS {r['gds_misconduct_prob']}{'⚑' if r['gds_flagged']=='Y' else ''}" if r['gds_misconduct_prob'] != "" else ""
        print(f"  {i}. [{r['score']:.1f}] {r['title'][:70]}...", file=sys.stderr)
        print(f"     ret{r['retracted_citation_count']} ref{r['reference_integrity_count']} "
              f"jrnl{r['journal_integrity_count']} ai{r['ai_text_tell_count']} "
              f"coauthor-misconduct{r['coauthor_misconduct']} jrr{r['journal_retr_rate']}{gds}", file=sys.stderr)
        print(f"     {r['doi']} ({r['journal']})", file=sys.stderr)


if __name__ == "__main__":
    main()
