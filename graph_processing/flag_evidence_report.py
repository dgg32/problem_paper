#!/usr/bin/env python3
"""
flag_evidence_report.py — detailed, human-readable evidence for top-scored papers.

Mirrors tier_a_scoring.py's ranking (sensor flags + explainable graph features)
and expands every contributing signal into named, sourced evidence:
  - retracted_citation / reference_integrity / journal_integrity / ai_text_tell:
    per-sensor flag examples (as before)
  - MERGED 2026-07-22 (user request): coauthor_other_misconduct and
    author_retr_rate_external used to be two separate signals -- one
    graph-internal/fuzzy-cluster/any-co-author/misconduct-reason-only, one
    ORCID-strict/first-last-only/any-reason -- now unified into 4 count-based
    minmax buckets split by AUTHOR POSITION (first/last vs middle) x
    SEVERITY (misconduct-reason vs any other reason), in a 1:2:2:4 ratio:
      - fl_any_count / fl_misconduct_count: a first/last author who
        co-authored OTHER retracted work, ORCID-matched against the full
        external Retraction Watch db (author_retraction_rate_external.py).
        NOTE (plan.md §0): this is still evidence about the PERSON's other
        work, not this paper.
      - mid_any_count / mid_misconduct_count: a MIDDLE co-author who shares
        a probable-person cluster with authors of some OTHER retracted paper
        found elsewhere in this graph (coauthor_retraction_severity.py) --
        the same fuzzy-cluster, graph-internal discipline the old
        coauthor_other_misconduct used, restricted now to middle positions
        only (first/last moved to the stricter ORCID path above). Misconduct
        here uses the same BROAD misconduct-signal reason set (Paper Mill,
        Fabrication, Image/Results Manipulation, ...) as everywhere else in
        this pipeline -- NOT the narrower, formally-adjudicated
        `on_misconduct_paper` flag (restricted to official-investigation/ORI
        findings, see plan.md §2.1b). A co-author appearing here means
        "shares a cluster with someone who wrote a paper retracted for a
        misconduct-signal reason," not "formally adjudicated." Keep that
        distinction in any human-facing copy.
  - institution_retr_rate: the (worst) involved institution's measured
    retraction rate in this graph -- unscored context only (2026-07-22:
    replaced in scoring by institution_retr_rate_external below, since this
    graph-internal rate runs inflated, e.g. 85-96% for the top institutions,
    "correct but too high to be intuitive" per user feedback -- same story as
    the old graph-internal journal_retr_rate). See institution_retraction_rate.py.
  - institution_retr_rate_external / publisher_retr_rate / country_retr_rate /
    journal_retr_rate_external: EXTERNAL real-world retraction rates (full
    Retraction Watch csv over a Crossref/OpenAlex/ROR total-works denominator,
    NOT scoped to our own graph) -- see institution_retraction_rate.py /
    publisher_retraction_rate.py / country_retraction_rate.py /
    journal_retraction_rate_external.py.
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
from tier_a_scoring import WEIGHTS, minmax_contribution, compute_corpus_maxes, get_corpus_max  # noqa: E402 -- single source of truth, see BUG.md #7

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
       coalesce(p.mid_any_count, 0) AS mid_any_count,
       coalesce(p.mid_misconduct_count, 0) AS mid_misconduct_count,
       coalesce(p.fl_any_count, 0) AS fl_any_count,
       coalesce(p.fl_misconduct_count, 0) AS fl_misconduct_count,
       coalesce(p.mid_any_volume, 0) AS mid_any_volume,
       coalesce(p.mid_misconduct_volume, 0) AS mid_misconduct_volume,
       coalesce(p.fl_any_volume, 0) AS fl_any_volume,
       coalesce(p.fl_misconduct_volume, 0) AS fl_misconduct_volume,
       coalesce(p.institution_retr_rate, 0.0) AS institution_retr_rate,
       p.institution_retr_rate_name AS institution_retr_rate_name,
       p.institution_retr_rate_n AS institution_retr_rate_n,
       coalesce(p.institution_retr_rate_external, 0.0) AS institution_retr_rate_external,
       p.institution_retr_rate_external_name AS institution_retr_rate_external_name,
       p.institution_retr_rate_external_n AS institution_retr_rate_external_n,
       p.institution_retr_rate_external_total AS institution_retr_rate_external_total,
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
       p.first_author_name AS first_author_name,
       coalesce(p.first_author_retr_n, 0) AS first_author_retr_n,
       coalesce(p.first_author_retr_misconduct_n, 0) AS first_author_retr_misconduct_n,
       p.first_author_retr_rate AS first_author_retr_rate,
       p.first_author_retr_total AS first_author_retr_total,
       p.last_author_name AS last_author_name,
       coalesce(p.last_author_retr_n, 0) AS last_author_retr_n,
       coalesce(p.last_author_retr_misconduct_n, 0) AS last_author_retr_misconduct_n,
       p.last_author_retr_rate AS last_author_retr_rate,
       p.last_author_retr_total AS last_author_retr_total,
       coalesce(p.crossref_correction_count, 0) AS correction_count,
       p.crossref_correction_dois AS correction_dois,
       p.gds_misconduct_prob AS gds_prob,
       p.retracted_citation_flags AS ret_flags,
       p.external_retracted_citation_flags AS ext_ret_flags,
       p.reference_integrity_flags AS ref_flags,
       p.journal_integrity_flags AS journal_flags,
       p.ai_text_tell_flags AS ai_flags
"""

# Restricted to MIDDLE position (2026-07-22) -- first/last co-authors moved
# to the ORCID-strict author_retraction_rate_external.py path; see this
# file's module docstring for the merge. A cluster with BOTH a misconduct-
# reason and a non-misconduct-reason retracted paper elsewhere counts ONLY
# as misconduct (mirrors coauthor_retraction_severity.py's mutually-
# exclusive mid_any_count/mid_misconduct_count split) -- $misconduct=true
# returns clusters with >=1 misconduct-reason paper; $misconduct=false
# returns clusters with ZERO misconduct-reason papers among their retracted-
# elsewhere work.
COAUTHOR_SEVERITY_QUERY = """
MATCH (p:Paper {doi: $doi})<-[:WROTE {author_position: 'middle'}]-(a:AuthorInstance)
WITH p, collect(DISTINCT a.cluster_id) AS clusters
UNWIND clusters AS cid
MATCH (mate:AuthorInstance {cluster_id: cid})-[:WROTE]->(mp:Paper)-[:RETRACTED_FOR]->(r:Reason)
WHERE mp <> p
WITH mate.name AS coauthor_name, cid AS cluster_id, mp, collect(DISTINCT r.code) AS codes
WITH coauthor_name, cluster_id, mp, any(c IN codes WHERE c IN $reasons) AS mp_is_misconduct
WITH coauthor_name, cluster_id,
     collect(DISTINCT mp.doi) AS example_dois,
     any(f IN collect(mp_is_misconduct) WHERE f) AS has_misconduct
WHERE has_misconduct = $misconduct
RETURN coauthor_name, cluster_id, example_dois[0..2] AS example_dois
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
        minmax_contribution("mid_any_count", row["mid_any_count"]) +
        minmax_contribution("mid_misconduct_count", row["mid_misconduct_count"]) +
        minmax_contribution("fl_any_count", row["fl_any_count"]) +
        minmax_contribution("fl_misconduct_count", row["fl_misconduct_count"]) +
        minmax_contribution("mid_any_volume", row["mid_any_volume"]) +
        minmax_contribution("mid_misconduct_volume", row["mid_misconduct_volume"]) +
        minmax_contribution("fl_any_volume", row["fl_any_volume"]) +
        minmax_contribution("fl_misconduct_volume", row["fl_misconduct_volume"]) +
        minmax_contribution("institution_retr_rate_external", row["institution_retr_rate_external"]) +
        minmax_contribution("publisher_retr_rate", row["publisher_retr_rate"]) +
        minmax_contribution("country_retr_rate", row["country_retr_rate"]) +
        minmax_contribution("journal_retr_rate_external", row["journal_retr_rate_external"]) +
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

        compute_corpus_maxes(rows)
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

            if row["mid_misconduct_count"] > 0:
                coauthors = s.run(
                    COAUTHOR_SEVERITY_QUERY, doi=row["doi"], reasons=MISCONDUCT_REASONS, misconduct=True
                ).data()
                corpus_max = get_corpus_max("mid_misconduct_count")
                flags_summary.append({
                    "type": "mid_misconduct_count",
                    "count": row["mid_misconduct_count"],
                    "corpus_max": corpus_max,
                    "target_max_score": WEIGHTS["mid_misconduct_minmax_target"],
                    "contribution": round(minmax_contribution("mid_misconduct_count", row["mid_misconduct_count"]), 2),
                    "note": ("Shares a probable-person cluster (middle author, fuzzy-matched, NOT strict "
                             "ORCID) with co-author(s) who wrote a paper retracted for a misconduct-signal "
                             "reason. This is NOT the narrower formally-adjudicated flag (official "
                             "investigation/ORI) -- see plan.md §2.1b. Same person ≠ same responsibility (§0). "
                             "Minmax-scaled against the worst-in-corpus middle-co-author count "
                             f"({corpus_max:g}), which scores {WEIGHTS['mid_misconduct_minmax_target']}."),
                    "examples": [
                        {"coauthor_name": c["coauthor_name"], "misconduct_paper_dois": c["example_dois"]}
                        for c in coauthors
                    ],
                })
                # VOLUME sibling (2026-08-16, user request): mid_misconduct_count
                # above only answers "does at least one cluster qualify" (0/N
                # clusters) -- a co-author with 1 other misconduct-coded
                # retraction and one with 26 (this corpus's max) contribute
                # identically there. This entry scores the SAME underlying
                # count a second time, minmax-scaled against its own
                # corpus-worst, as a SEPARATE additive entry (not folded into
                # the entry above) so the JSON's per-signal contributions stay
                # individually auditable -- see tier_a_scoring.py's
                # DEFAULT_WEIGHTS "VOLUME" note for the full rationale.
                mc_vol_max = get_corpus_max("mid_misconduct_volume")
                flags_summary.append({
                    "type": "mid_misconduct_volume",
                    "count": row["mid_misconduct_volume"],
                    "corpus_max": mc_vol_max,
                    "target_max_score": WEIGHTS["mid_misconduct_volume_minmax_target"],
                    "contribution": round(minmax_contribution("mid_misconduct_volume", row["mid_misconduct_volume"]), 2),
                    "note": ("Total OTHER misconduct-coded retracted papers across the co-author(s) above "
                             "(not just how many co-authors qualify -- one prolific co-author can carry most "
                             "of this total). Additive to mid_misconduct_count, at half its target weight. "
                             f"Minmax-scaled against the worst-in-corpus total ({mc_vol_max:g}), which scores "
                             f"{WEIGHTS['mid_misconduct_volume_minmax_target']}."),
                })

            if row["mid_any_count"] > 0:
                coauthors = s.run(
                    COAUTHOR_SEVERITY_QUERY, doi=row["doi"], reasons=MISCONDUCT_REASONS, misconduct=False
                ).data()
                corpus_max = get_corpus_max("mid_any_count")
                flags_summary.append({
                    "type": "mid_any_count",
                    "count": row["mid_any_count"],
                    "corpus_max": corpus_max,
                    "target_max_score": WEIGHTS["mid_any_minmax_target"],
                    "contribution": round(minmax_contribution("mid_any_count", row["mid_any_count"]), 2),
                    "note": ("Shares a probable-person cluster (middle author, fuzzy-matched) with "
                             "co-author(s) who wrote a paper retracted for a NON-misconduct reason "
                             "(honest error, duplication, etc.) -- half the weight of mid_misconduct_count. "
                             "Minmax-scaled against the worst-in-corpus middle-co-author count "
                             f"({corpus_max:g}), which scores {WEIGHTS['mid_any_minmax_target']}."),
                    "examples": [
                        {"coauthor_name": c["coauthor_name"], "paper_dois": c["example_dois"]}
                        for c in coauthors
                    ],
                })
                # VOLUME sibling -- see the mid_misconduct_volume note above.
                any_vol_max = get_corpus_max("mid_any_volume")
                flags_summary.append({
                    "type": "mid_any_volume",
                    "count": row["mid_any_volume"],
                    "corpus_max": any_vol_max,
                    "target_max_score": WEIGHTS["mid_any_volume_minmax_target"],
                    "contribution": round(minmax_contribution("mid_any_volume", row["mid_any_volume"]), 2),
                    "note": ("Total OTHER non-misconduct retracted papers across the co-author(s) above. "
                             "Additive to mid_any_count, at half its target weight. Minmax-scaled against "
                             f"the worst-in-corpus total ({any_vol_max:g}), which scores "
                             f"{WEIGHTS['mid_any_volume_minmax_target']}."),
                })

            if row["fl_misconduct_count"] > 0 or row["fl_any_count"] > 0:
                for position, name_key, n_key, mc_key in (
                    ("first", "first_author_name", "first_author_retr_n", "first_author_retr_misconduct_n"),
                    ("last", "last_author_name", "last_author_retr_n", "last_author_retr_misconduct_n"),
                ):
                    if row[n_key] == 0:
                        continue
                    is_misconduct = row[mc_key] > 0
                    key = "fl_misconduct_count" if is_misconduct else "fl_any_count"
                    target_key = "fl_misconduct_minmax_target" if is_misconduct else "fl_any_minmax_target"
                    corpus_max = get_corpus_max(key)
                    flags_summary.append({
                        "type": f"{key}_{position}",
                        "position": position,
                        "count": 1,
                        "corpus_max": corpus_max,
                        "target_max_score": WEIGHTS[target_key],
                        "contribution": round(minmax_contribution(key, 1), 2),
                        "note": (f"{row[name_key]} ({position} author): {row[n_key]} of their ORCID-claimed "
                                 f"works were retracted per Retraction Watch ({row[mc_key]} misconduct-coded), "
                                 "matched by DOI against their own ORCID record, not by name. Same person ≠ "
                                 "same responsibility (§0): this is the author's track record across ALL their "
                                 f"claimed work, not a finding about this paper. Minmax-scaled: the worst-in-"
                                 f"corpus count of qualifying first/last positions ({corpus_max:g}) scores "
                                 f"{WEIGHTS[target_key]}; this position contributes 1 unit toward that."),
                    })
                    # VOLUME sibling (2026-08-16): this position's OWN retracted-
                    # works count (not the flat "1 unit" above) minmax-scaled
                    # against its own corpus-worst -- see mid_misconduct_volume
                    # note above for the full rationale. Already naturally
                    # position-specific, so no per-position splitting needed:
                    # the misconduct-only subset for misconduct positions,
                    # the full (all-non-misconduct) count otherwise.
                    volume_key = "fl_misconduct_volume" if is_misconduct else "fl_any_volume"
                    volume_target_key = f"{volume_key}_minmax_target"
                    volume_n = row[mc_key] if is_misconduct else row[n_key]
                    volume_corpus_max = get_corpus_max(volume_key)
                    flags_summary.append({
                        "type": f"{volume_key}_{position}",
                        "position": position,
                        "count": volume_n,
                        "corpus_max": volume_corpus_max,
                        "target_max_score": WEIGHTS[volume_target_key],
                        "contribution": round(minmax_contribution(volume_key, volume_n), 2),
                        "note": (f"{row[name_key]} ({position} author): {volume_n} qualifying retracted work(s) "
                                 "at this position, minmax-scaled directly (not flattened to 1 unit like the "
                                 "entry above) -- additive, at half that entry's target weight. Minmax-scaled "
                                 f"against the worst-in-corpus total for this bucket ({volume_corpus_max:g}), "
                                 f"which scores {WEIGHTS[volume_target_key]}."),
                    })

            if row["institution_retr_rate"] > 0:
                flags_summary.append({
                    "type": "institution_retr_rate",
                    "scored": False,
                    "value": round(row["institution_retr_rate"], 3),
                    "note": (f"{row['institution_retr_rate_name']} has a {row['institution_retr_rate']:.1%} "
                             f"retraction rate in this graph (n={row['institution_retr_rate_n']} papers). "
                             "Context only, never scored (2026-07-22): this graph-internal rate runs inflated "
                             "-- institution_retr_rate_external below is the real, scored replacement."),
                })

            if row["institution_retr_rate_external"] > 0:
                corpus_max = get_corpus_max("institution_retr_rate_external")
                flags_summary.append({
                    "type": "institution_retr_rate_external",
                    "value": round(row["institution_retr_rate_external"], 5),
                    "corpus_max": round(corpus_max, 5),
                    "target_max_score": WEIGHTS["institution_retr_rate_minmax_target"],
                    "contribution": round(minmax_contribution("institution_retr_rate_external", row["institution_retr_rate_external"]), 2),
                    "note": (f"{row['institution_retr_rate_external_name']} has an external (Retraction Watch / "
                             f"OpenAlex via ROR) retraction rate of {row['institution_retr_rate_external']:.3%} "
                             f"(n={row['institution_retr_rate_external_n']}/{row['institution_retr_rate_external_total']}) "
                             "-- NOT scoped to our own graph. Minmax-scaled against the worst institution in this "
                             f"corpus ({corpus_max:.3%}), which scores {WEIGHTS['institution_retr_rate_minmax_target']}."),
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
                corpus_max = get_corpus_max("publisher_retr_rate")
                flags_summary.append({
                    "type": "publisher_retr_rate",
                    "value": round(row["publisher_retr_rate"], 5),
                    "corpus_max": round(corpus_max, 5),
                    "target_max_score": WEIGHTS["publisher_retr_rate_minmax_target"],
                    "contribution": round(minmax_contribution("publisher_retr_rate", row["publisher_retr_rate"]), 4),
                    "note": (f"{row['publisher_retr_rate_name']} has an external (Retraction Watch / Crossref) "
                             f"retraction rate of {row['publisher_retr_rate']:.3%} "
                             f"(n={row['publisher_retr_rate_n']} RW-recorded retractions) -- NOT scoped to our own graph. "
                             f"Minmax-scaled against the worst publisher in this corpus ({corpus_max:.3%}), which "
                             f"scores {WEIGHTS['publisher_retr_rate_minmax_target']}."),
                })

            if row["country_retr_rate"] > 0:
                corpus_max = get_corpus_max("country_retr_rate")
                flags_summary.append({
                    "type": "country_retr_rate",
                    "value": round(row["country_retr_rate"], 5),
                    "corpus_max": round(corpus_max, 5),
                    "target_max_score": WEIGHTS["country_retr_rate_minmax_target"],
                    "contribution": round(minmax_contribution("country_retr_rate", row["country_retr_rate"]), 4),
                    "note": (f"{row['country_retr_rate_name']} has an external (Retraction Watch / OpenAlex) "
                             f"retraction rate of {row['country_retr_rate']:.3%} "
                             f"(n={row['country_retr_rate_n']} RW-recorded retractions) -- NOT scoped to our own graph. "
                             f"Minmax-scaled against the worst country in this corpus ({corpus_max:.3%}), which "
                             f"scores {WEIGHTS['country_retr_rate_minmax_target']}."),
                })

            if row["journal_retr_rate_external"] > 0:
                corpus_max = get_corpus_max("journal_retr_rate_external")
                flags_summary.append({
                    "type": "journal_retr_rate_external",
                    "value": round(row["journal_retr_rate_external"], 5),
                    "corpus_max": round(corpus_max, 5),
                    "target_max_score": WEIGHTS["journal_retr_rate_external_minmax_target"],
                    "contribution": round(minmax_contribution("journal_retr_rate_external", row["journal_retr_rate_external"]), 4),
                    "note": (f"{row['journal']} has an external (Retraction Watch / Crossref Journals API) "
                             f"retraction rate of {row['journal_retr_rate_external']:.3%} "
                             f"(n={row['journal_retr_rate_external_n']} RW-recorded retractions) -- NOT scoped to our own graph. "
                             f"Minmax-scaled against the worst journal in this corpus ({corpus_max:.3%}), which "
                             f"scores {WEIGHTS['journal_retr_rate_external_minmax_target']}."),
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
            if label in ("mid_misconduct_count", "mid_any_count"):
                for ex in flag["examples"]:
                    dois = ex.get("misconduct_paper_dois") or ex.get("paper_dois")
                    print(f"        - {ex['coauthor_name']}  (see {dois})", file=sys.stderr)


if __name__ == "__main__":
    main()
