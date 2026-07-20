#!/usr/bin/env python3
"""
flag_evidence_report.py — detailed, human-readable evidence for top-scored papers.

Mirrors tier_a_scoring.py's ranking (sensor flags + explainable graph features)
and expands every contributing signal into named, sourced evidence:
  - retracted_citation / reference_integrity / journal_integrity / ai_text_tell:
    per-sensor flag examples (as before)
  - coauthor_other_misconduct: names the specific probable-person co-authors
    who share a cluster with authors of a misconduct-signal-retracted paper,
    and which paper/reason. NOTE (important, plan.md §0): this uses the BROAD
    misconduct-signal reason set (Paper Mill, Fabrication, Image/Results
    Manipulation, ...) -- the same set retracted_citation_checker.py uses --
    NOT the narrower, formally-adjudicated `on_misconduct_paper` flag (which
    is restricted to official-investigation/ORI findings, see plan.md §2.1b).
    A co-author appearing here means "shares a cluster with someone who wrote
    a paper retracted for a misconduct-signal reason," not "formally
    adjudicated." Keep that distinction in any human-facing copy.
  - journal_retr_rate: the journal's measured retraction rate in this graph.
  - gds_misconduct_prob: the Tier-B GDS node-classification prior, surfaced
    as a labeled, non-scored secondary signal only (see gds_node_classification.py
    for why it is capped/weak/domain-shifted and deliberately excluded from
    the weighted score).

Output: JSON report with full evidence details for human review.
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
from tier_a_scoring import WEIGHTS  # noqa: E402 -- single source of truth, see BUG.md #7

# Must match gds_node_classification.py's MISCONDUCT_REASONS (broad,
# "misconduct-signal" set) -- NOT AuthorInstance.on_misconduct_paper's
# narrower official-investigation/ORI-only definition. See module docstring.
MISCONDUCT_REASONS = [
    "Misconduct - Official Investigation(s) and/or Finding(s)",
    "Investigation by ORI",
    "Paper Mill",
    "Falsification/Fabrication of Data",
    "Falsification/Fabrication of Image",
    "Falsification/Fabrication of Results",
    "Manipulation of Images",
    "Manipulation of Results",
    "Euphemisms for Misconduct",
    "Misconduct by Author",
]

QUERY = """
MATCH (p:Paper {is_retracted:false})-[:PUBLISHED_IN]->(j:Journal)
RETURN p.doi AS doi,
       p.title AS title,
       j.name AS journal,
       p.published_date AS published_date,
       coalesce(p.retracted_citation_flag_count, 0) AS ret_count,
       coalesce(p.external_retracted_citation_flag_count, 0) AS ext_ret_count,
       coalesce(p.reference_integrity_flag_count, 0) AS ref_count,
       coalesce(p.journal_integrity_flag_count, 0) AS journal_count,
       coalesce(p.ai_text_tell_flag_count, 0) AS ai_count,
       coalesce(p.coauthor_other_misconduct, 0) AS coauthor_misconduct,
       coalesce(p.journal_retr_rate, 0.0) AS journal_retr_rate,
       p.gds_misconduct_prob AS gds_prob,
       p.retracted_citation_flags AS ret_flags,
       p.external_retracted_citation_flags AS ext_ret_flags,
       p.reference_integrity_flags AS ref_flags,
       p.journal_integrity_flags AS journal_flags,
       p.ai_text_tell_flags AS ai_flags
"""

COAUTHOR_MISCONDUCT_QUERY = """
MATCH (p:Paper {doi: $doi})<-[:WROTE]-(a:AuthorInstance)
WITH p, collect(DISTINCT a.cluster_id) AS clusters
UNWIND clusters AS cid
MATCH (mate:AuthorInstance {cluster_id: cid})-[:WROTE]->(mp:Paper)-[:RETRACTED_FOR]->(r:Reason)
WHERE mp <> p AND r.code IN $reasons
WITH mate.name AS coauthor_name, mate.cluster_id AS cluster_id,
     collect(DISTINCT mp.doi)[0..2] AS example_dois,
     collect(DISTINCT r.code) AS reasons
RETURN coauthor_name, cluster_id, example_dois, reasons
ORDER BY coauthor_name
LIMIT 6
"""


def calculate_score(row: dict) -> float:
    return (
        row["ret_count"] * WEIGHTS["retracted_citation_flag_count"] +
        row["ext_ret_count"] * WEIGHTS["external_retracted_citation_flag_count"] +
        # reference_integrity_flag_count deliberately excluded -- see tier_a_scoring.py WEIGHTS comment
        row["journal_count"] * WEIGHTS["journal_integrity_flag_count"] +
        row["ai_count"] * WEIGHTS["ai_text_tell_flag_count"] +
        row["coauthor_misconduct"] * WEIGHTS["coauthor_other_misconduct"] +
        row["journal_retr_rate"] * WEIGHTS["journal_retr_rate"]
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=50, help="top N papers")
    ap.add_argument("--output", "-o", default="data/flag_evidence_report_top.json")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(QUERY)]

        scored = [(calculate_score(r), r) for r in rows]
        scored.sort(key=lambda t: t[0], reverse=True)
        top_rows = scored[:args.top]

        report = []
        for rank, (score, row) in enumerate(top_rows, 1):
            flags_summary = []

            if row["ret_count"] > 0:
                ret_flags = json.loads(row["ret_flags"] or "[]")
                flags_summary.append({
                    "type": "retracted_citation",
                    "count": row["ret_count"],
                    "weight": WEIGHTS["retracted_citation_flag_count"],
                    "contribution": row["ret_count"] * WEIGHTS["retracted_citation_flag_count"],
                    "examples": [
                        {
                            "cited_doi": f.get("cited_retracted_paper_doi"),
                            "cited_title": (f.get("cited_retracted_paper_title") or "")[:80],
                            "reason": ", ".join(f.get("retraction_reasons", [])) or "unknown",
                            "citing_after_retraction": f.get("citing_after_retraction"),
                        }
                        for f in ret_flags[:3]
                    ],
                })

            if row["ext_ret_count"] > 0:
                ext_ret_flags = json.loads(row["ext_ret_flags"] or "[]")
                flags_summary.append({
                    "type": "external_retracted_citation",
                    "count": row["ext_ret_count"],
                    "weight": WEIGHTS["external_retracted_citation_flag_count"],
                    "contribution": row["ext_ret_count"] * WEIGHTS["external_retracted_citation_flag_count"],
                    "note": ("Cites a retracted paper OUTSIDE our Retraction-Watch-seeded corpus, "
                             "confirmed live via OpenAlex's is_retracted field. No RetractionWatch "
                             "reason/date/self-citation context available -- see "
                             "sensors/external_retracted_citation_checker.py."),
                    "examples": [
                        {
                            "cited_doi": f.get("cited_retracted_paper_doi"),
                            "cited_title": (f.get("cited_retracted_paper_title") or "")[:80],
                        }
                        for f in ext_ret_flags[:3]
                    ],
                })

            if row["ref_count"] > 0:
                ref_flags = json.loads(row["ref_flags"] or "[]")
                flags_summary.append({
                    "type": "reference_integrity",
                    "count": row["ref_count"],
                    "scored": False,
                    "note": ("NOT part of the score (2026-07-20): the no-DOI bibliographic-search "
                             "route flags ~71% of the corpus, overwhelmingly real citations that "
                             "just don't index well for title search (taxonomic monographs, "
                             "pre-DOI authorities, gray literature) -- see "
                             "sensors/reference_integrity_checker.py."),
                    "examples": [
                        {
                            "reference": (f.get("reference_title") or "")[:100],
                            "issue": f.get("reason"),
                            "severity": f.get("severity"),
                        }
                        for f in ref_flags[:3]
                    ],
                })

            if row["journal_count"] > 0:
                journal_flags = json.loads(row["journal_flags"] or "[]")
                flags_summary.append({
                    "type": "journal_integrity",
                    "count": row["journal_count"],
                    "weight": WEIGHTS["journal_integrity_flag_count"],
                    "contribution": row["journal_count"] * WEIGHTS["journal_integrity_flag_count"],
                    "reason": journal_flags[0].get("reason") if journal_flags else "unknown",
                })

            if row["ai_count"] > 0:
                ai_flags = json.loads(row["ai_flags"] or "[]")
                flags_summary.append({
                    "type": "ai_text_tell",
                    "count": row["ai_count"],
                    "weight": WEIGHTS["ai_text_tell_flag_count"],
                    "contribution": row["ai_count"] * WEIGHTS["ai_text_tell_flag_count"],
                    "examples": [f.get("pattern") for f in ai_flags[:3]],
                })

            if row["coauthor_misconduct"] > 0:
                coauthors = s.run(
                    COAUTHOR_MISCONDUCT_QUERY, doi=row["doi"], reasons=MISCONDUCT_REASONS
                ).data()
                flags_summary.append({
                    "type": "coauthor_other_misconduct",
                    "count": row["coauthor_misconduct"],
                    "weight": WEIGHTS["coauthor_other_misconduct"],
                    "contribution": row["coauthor_misconduct"] * WEIGHTS["coauthor_other_misconduct"],
                    "note": ("Shares a probable-person cluster with co-author(s) who wrote a "
                             "paper retracted for a misconduct-signal reason. This is NOT the "
                             "narrower formally-adjudicated flag (official investigation/ORI) -- "
                             "see plan.md §2.1b. Same person ≠ same responsibility (§0)."),
                    "examples": [
                        {
                            "coauthor_name": c["coauthor_name"],
                            "misconduct_paper_dois": c["example_dois"],
                            "misconduct_reasons": c["reasons"],
                        }
                        for c in coauthors
                    ],
                })

            if row["journal_retr_rate"] > 0:
                flags_summary.append({
                    "type": "journal_retr_rate",
                    "value": round(row["journal_retr_rate"], 3),
                    "weight": WEIGHTS["journal_retr_rate"],
                    "contribution": round(row["journal_retr_rate"] * WEIGHTS["journal_retr_rate"], 2),
                    "note": f"{row['journal']} has a {row['journal_retr_rate']:.1%} retraction rate in this graph.",
                })

            report.append({
                "rank": rank,
                "doi": row["doi"],
                "title": row["title"],
                "journal": row["journal"],
                "published_date": str(row["published_date"]) if row["published_date"] else None,
                "score": round(score, 2),
                "flags": flags_summary,
                # secondary, labeled, NOT part of score -- see gds_node_classification.py
                "gds_misconduct_prob": round(row["gds_prob"], 3) if row["gds_prob"] is not None else None,
                "gds_note": "Weak/capped/domain-shifted Tier-B learned prior; not part of the score." if row["gds_prob"] is not None else None,
                "doi_url": f"https://doi.org/{row['doi']}",
            })
    driver.close()

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, indent=2))

    print(f"Wrote {len(report)} papers with evidence to {args.output}", file=sys.stderr)

    print("\n=== Flag Evidence Report ===", file=sys.stderr)
    for paper in report[:10]:
        print(f"\nRank {paper['rank']}: {paper['title'][:70]}...", file=sys.stderr)
        print(f"  DOI: {paper['doi']}", file=sys.stderr)
        print(f"  Score: {paper['score']:.1f}" + (
            f"  | GDS prior: {paper['gds_misconduct_prob']}" if paper["gds_misconduct_prob"] is not None else ""
        ), file=sys.stderr)
        for flag in paper["flags"]:
            label = flag["type"]
            contrib = flag.get("contribution", 0)
            print(f"    • {label}: +{contrib:.2f} score", file=sys.stderr)
            if label == "coauthor_other_misconduct":
                for ex in flag["examples"]:
                    print(f"        - {ex['coauthor_name']}  (see {ex['misconduct_paper_dois']})", file=sys.stderr)


if __name__ == "__main__":
    main()
