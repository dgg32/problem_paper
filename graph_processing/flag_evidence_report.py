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
  - institution_retr_rate: the (worst) involved institution's measured
    retraction rate in this graph -- see institution_retraction_rate.py.
  - publisher_retr_rate / country_retr_rate / journal_retr_rate_external:
    EXTERNAL real-world retraction rates (full Retraction Watch csv over a
    Crossref/OpenAlex total-works denominator, NOT scoped to our own graph)
    -- see publisher_retraction_rate.py / country_retraction_rate.py /
    journal_retraction_rate_external.py.
  - author_retr_rate_external: a FIRST or LAST author's OWN retraction RATE
    (see tier_a_scoring.py's WEIGHTS comment for the full 2026-07-22 retuning
    history) -- their ORCID-claimed works as denominator, Retraction Watch
    matched by DOI (not name) as numerator -- see
    author_retraction_rate_external.py.
  - NOTE (removed 2026-07-22): the graph-internal `journal_retr_rate` used to
    be scored here too, but it and journal_integrity_flag_count's check 3
    (journal_integrity_check.py) measure the SAME underlying fact -- this
    journal's retraction rate in our own seeded graph -- one continuously,
    one as a >10% threshold, so a paper could be scored twice for one real
    cause. Removed from the score entirely; journal_retr_rate_external above
    is the real, non-redundant replacement. journal_retr_rate itself still
    exists as a Tier-B GDS input feature (gds_node_classification.py), just
    no longer surfaced/scored here.
  - crossref_correction_flag_count: Crossref-deposited correction notice(s)
    on this DOI -- see refresh_correction_history.py.
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
from tier_a_scoring import WEIGHTS, capped_contribution  # noqa: E402 -- single source of truth, see BUG.md #7

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
       coalesce(p.institution_retr_rate, 0.0) AS institution_retr_rate,
       p.institution_retr_rate_name AS institution_retr_rate_name,
       p.institution_retr_rate_n AS institution_retr_rate_n,
       p.institution_global_retraction_count AS institution_global_retraction_count,
       p.institution_global_retraction_count_name AS institution_global_retraction_count_name,
       coalesce(p.journal_hijack_flag, false) AS journal_hijack_flag,
       p.journal_hijack_original_url AS journal_hijack_original_url,
       p.journal_hijack_hijacked_url AS journal_hijack_hijacked_url,
       coalesce(p.known_miller_coauthor, false) AS known_miller_coauthor,
       p.known_miller_coauthor_name AS known_miller_coauthor_name,
       p.known_miller_source_url AS known_miller_source_url,
       coalesce(p.cabanac_chatgpt_flag, false) AS cabanac_chatgpt_flag,
       p.cabanac_chatgpt_fingerprint AS cabanac_chatgpt_fingerprint,
       p.cabanac_chatgpt_pubpeer_url AS cabanac_chatgpt_pubpeer_url,
       coalesce(p.publisher_retr_rate, 0.0) AS publisher_retr_rate,
       p.publisher_retr_rate_name AS publisher_retr_rate_name,
       p.publisher_retr_rate_n AS publisher_retr_rate_n,
       coalesce(p.country_retr_rate, 0.0) AS country_retr_rate,
       p.country_retr_rate_name AS country_retr_rate_name,
       p.country_retr_rate_n AS country_retr_rate_n,
       coalesce(p.journal_retr_rate_external, 0.0) AS journal_retr_rate_external,
       p.journal_retr_rate_external_n AS journal_retr_rate_external_n,
       coalesce(p.author_retr_rate_external, 0.0) AS author_retr_rate_external,
       p.author_retr_rate_external_name AS author_retr_rate_external_name,
       p.author_retr_rate_external_position AS author_retr_rate_external_position,
       coalesce(p.author_retr_rate_external_n, 0) AS author_retr_rate_external_n,
       p.author_retr_rate_external_total AS author_retr_rate_external_total,
       coalesce(p.crossref_correction_count, 0) AS correction_count,
       p.crossref_correction_dois AS correction_dois,
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
        row["institution_retr_rate"] * WEIGHTS["institution_retr_rate"] +
        capped_contribution("publisher_retr_rate", row["publisher_retr_rate"]) +
        capped_contribution("country_retr_rate", row["country_retr_rate"]) +
        capped_contribution("journal_retr_rate_external", row["journal_retr_rate_external"]) +
        capped_contribution("author_retr_rate_external", row["author_retr_rate_external"]) +
        row["correction_count"] * WEIGHTS["crossref_correction_flag_count"]
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
                    "note": ("This flag fires for any ONE of three different checks (see "
                             "sensors/journal_integrity_check.py): an OA-only publisher's journal missing "
                             "from DOAJ, explicit Scopus/Web-of-Science delisting, or this journal crossing "
                             "a >10% retraction-rate threshold measured in our own graph -- the same weight "
                             "can mean different things paper to paper. The specific reason for THIS paper:"),
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

            if row["institution_retr_rate"] > 0:
                flags_summary.append({
                    "type": "institution_retr_rate",
                    "value": round(row["institution_retr_rate"], 3),
                    "weight": WEIGHTS["institution_retr_rate"],
                    "contribution": round(row["institution_retr_rate"] * WEIGHTS["institution_retr_rate"], 2),
                    "note": (f"{row['institution_retr_rate_name']} has a {row['institution_retr_rate']:.1%} "
                             f"retraction rate in this graph (n={row['institution_retr_rate_n']} papers)."),
                })

            if row["institution_global_retraction_count"]:
                flags_summary.append({
                    "type": "institution_global_retraction_count",
                    "count": row["institution_global_retraction_count"],
                    "scored": False,
                    "note": (f"{row['institution_global_retraction_count_name']} has "
                             f"{row['institution_global_retraction_count']} retraction(s) exact-name-matched in the "
                             f"FULL Retraction Watch csv (all subjects) -- NOT scoped to this graph. Raw count, not "
                             f"a rate; coverage is deliberately partial (exact match only). Context only, never scored."),
                })

            if row["journal_hijack_flag"]:
                flags_summary.append({
                    "type": "journal_hijack_flag",
                    "scored": False,
                    "hijacked_url": row["journal_hijack_hijacked_url"],
                    "original_url": row["journal_hijack_original_url"],
                    "note": (f"{row['journal']}'s name/ISSN is documented in the Retraction Watch / Anna Abalkina "
                             "Hijacked Journal Checker: a scam site clones it to solicit fraudulent 'publications'. "
                             f"Real site: {row['journal_hijack_original_url'] or 'unknown'} -- clone site: "
                             f"{row['journal_hijack_hijacked_url']}. This does NOT mean this specific paper came "
                             "from the clone -- verify which site it actually appeared on. Context only, never scored."),
                })

            if row["known_miller_coauthor"]:
                flags_summary.append({
                    "type": "known_miller_coauthor",
                    "scored": False,
                    "miller_name": row["known_miller_coauthor_name"],
                    "source_url": row["known_miller_source_url"],
                    "note": (f"👤 {row['known_miller_coauthor_name']} -- named in investigative reporting as a "
                             f"paper-mill participant ({row['known_miller_source_url']}) -- co-authored this paper. "
                             "Guilt by co-authorship with a documented bad actor is associative, not a finding "
                             "about this paper's own conduct (§0). Context only, never scored."),
                })

            if row["cabanac_chatgpt_flag"]:
                flags_summary.append({
                    "type": "cabanac_chatgpt_flag",
                    "scored": False,
                    "fingerprint": row["cabanac_chatgpt_fingerprint"],
                    "pubpeer_url": row["cabanac_chatgpt_pubpeer_url"],
                    "note": (f"Matched fingerprint(s): \"{row['cabanac_chatgpt_fingerprint']}\". Confirmed by "
                             "Guillaume Cabanac's Problematic Paper Screener, independent of our own "
                             "ai_text_tell_flag_count sensor. Context only -- calibrates that sensor rather than "
                             "adding a second scored signal for the same phenomenon."),
                })

            if row["publisher_retr_rate"] > 0:
                contrib = capped_contribution("publisher_retr_rate", row["publisher_retr_rate"])
                capped = contrib < row["publisher_retr_rate"] * WEIGHTS["publisher_retr_rate"]
                flags_summary.append({
                    "type": "publisher_retr_rate",
                    "value": round(row["publisher_retr_rate"], 5),
                    "weight": WEIGHTS["publisher_retr_rate"],
                    "contribution": round(contrib, 4),
                    "capped": capped,
                    "note": (f"{row['publisher_retr_rate_name']} has an external (Retraction Watch / Crossref) "
                             f"retraction rate of {row['publisher_retr_rate']:.3%} "
                             f"(n={row['publisher_retr_rate_n']} RW-recorded retractions) -- NOT scoped to our own graph."
                             + (f" Contribution capped at {WEIGHTS['publisher_retr_rate_cap']} (see plan.md 2026-07-22 "
                                "caps update) so a single infamous publisher can't outrank direct per-paper evidence."
                                if capped else "")),
                })

            if row["country_retr_rate"] > 0:
                contrib = capped_contribution("country_retr_rate", row["country_retr_rate"])
                capped = contrib < row["country_retr_rate"] * WEIGHTS["country_retr_rate"]
                flags_summary.append({
                    "type": "country_retr_rate",
                    "value": round(row["country_retr_rate"], 5),
                    "weight": WEIGHTS["country_retr_rate"],
                    "contribution": round(contrib, 4),
                    "capped": capped,
                    "note": (f"{row['country_retr_rate_name']} has an external (Retraction Watch / OpenAlex) "
                             f"retraction rate of {row['country_retr_rate']:.3%} "
                             f"(n={row['country_retr_rate_n']} RW-recorded retractions) -- NOT scoped to our own graph."
                             + (f" Contribution capped at {WEIGHTS['country_retr_rate_cap']}." if capped else "")),
                })

            if row["journal_retr_rate_external"] > 0:
                contrib = capped_contribution("journal_retr_rate_external", row["journal_retr_rate_external"])
                capped = contrib < row["journal_retr_rate_external"] * WEIGHTS["journal_retr_rate_external"]
                flags_summary.append({
                    "type": "journal_retr_rate_external",
                    "value": round(row["journal_retr_rate_external"], 5),
                    "weight": WEIGHTS["journal_retr_rate_external"],
                    "contribution": round(contrib, 4),
                    "capped": capped,
                    "note": (f"{row['journal']} has an external (Retraction Watch / Crossref Journals API) "
                             f"retraction rate of {row['journal_retr_rate_external']:.3%} "
                             f"(n={row['journal_retr_rate_external_n']} RW-recorded retractions) -- NOT scoped to our own graph."
                             + (f" Contribution capped at {WEIGHTS['journal_retr_rate_external_cap']}." if capped else "")),
                })

            if row["author_retr_rate_external_n"] > 0:
                contrib = capped_contribution("author_retr_rate_external", row["author_retr_rate_external"])
                capped = contrib < row["author_retr_rate_external"] * WEIGHTS["author_retr_rate_external"]
                flags_summary.append({
                    "type": "author_retr_rate_external",
                    "count": row["author_retr_rate_external_n"],
                    "value": round(row["author_retr_rate_external"], 5),
                    "weight": WEIGHTS["author_retr_rate_external"],
                    "contribution": round(contrib, 2),
                    "capped": capped,
                    "note": (f"{row['author_retr_rate_external_n']} out of {row['author_retr_rate_external_name']}'s "
                             f"({row['author_retr_rate_external_position']} author) "
                             f"{row['author_retr_rate_external_total']} ORCID-claimed works were retracted "
                             f"({row['author_retr_rate_external']:.1%}), per Retraction Watch -- matched by DOI "
                             "against their own ORCID record, not by name. Same person ≠ same responsibility (§0): "
                             "this is the author's track record across ALL their claimed work, not a finding about "
                             "this paper."
                             + (f" Contribution capped at {WEIGHTS['author_retr_rate_external_cap']} so one "
                                "prolific repeat-offender author can't dwarf every other candidate's entire score."
                                if capped else "")),
                })

            if row["correction_count"] > 0:
                correction_dois = json.loads(row["correction_dois"] or "[]")
                flags_summary.append({
                    "type": "crossref_correction_flag_count",
                    "count": row["correction_count"],
                    "weight": WEIGHTS["crossref_correction_flag_count"],
                    "contribution": round(row["correction_count"] * WEIGHTS["crossref_correction_flag_count"], 2),
                    "note": ("A correction notice's own Crossref record links back to this DOI "
                             "(update-to) -- corrections are often benign, kept low-weight; "
                             "see graph_processing/refresh_correction_history.py."),
                    "examples": correction_dois[:3],
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
