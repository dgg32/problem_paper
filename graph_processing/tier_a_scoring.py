#!/usr/bin/env python3
"""
tier_a_scoring.py — Tier-A heuristic scoring engine (plan.md §2.2).

Combines Phase 4 sensor outputs into a weighted triage score.

Scoring logic:
  - retracted_citation_flag_count: weight 3.0 (citing known-retracted work is very suspicious;
    graph-internal only -- see external_retracted_citation_flag_count below for the complement)
  - external_retracted_citation_flag_count: weight 2.0 (citing a retracted paper OUTSIDE our
    Retraction-Watch-seeded corpus, confirmed live via OpenAlex's is_retracted field -- a
    confirmed fact, not a heuristic search, so it's safe to score. Lower than the 3.0 sibling
    because it lacks RetractionWatch's timing/misconduct-reason/self-citation context -- see
    sensors/external_retracted_citation_checker.py, added 2026-07-20.)
  - reference_integrity_flag_count: NOT scored (see WEIGHTS comment below — corpus-specific
    false-positive rate, 2026-07-20)
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
  - institution_retr_rate: weight 1.5 (an institution's own measured
    retraction rate in this graph, max across a paper's INVOLVES institutions;
    see graph_processing/institution_retraction_rate.py, added 2026-07-21.)
  - crossref_correction_flag_count: weight 0.3 (tied with pubmed_erratum_flag
    -- found via Crossref's updates:{doi} reverse lookup, i.e. a correction
    notice's own record explicitly links back to this DOI (update-to); a
    real, dated, publisher-deposited fact, but corrections are routinely
    benign (typo/affiliation fixes), so kept low deliberately. See
    graph_processing/refresh_correction_history.py, added 2026-07-21.)
  - publisher_retr_rate: weight 1.5 (a publisher's retraction rate computed
    from the FULL Retraction Watch csv over a Crossref Members API total-
    dois denominator -- unlike institution_retr_rate this is NOT scoped to
    our own retraction-seeded graph, so it is a genuine external base rate,
    not a graph-relative one. See graph_processing/publisher_retraction_rate.py,
    added 2026-07-22.)
  - country_retr_rate: weight 1.0 (same external-rate reasoning, at country
    granularity via OpenAlex's per-country works count; lowest weight of the
    three retr_rate sensors as the broadest, least specific attribution. See
    graph_processing/country_retraction_rate.py, added 2026-07-22.)
  - journal_retr_rate_external: weight 2.0 (same external-rate reasoning as
    publisher_retr_rate/country_retr_rate, at journal granularity via
    Crossref's Journals API total-dois. See
    graph_processing/journal_retraction_rate_external.py, added 2026-07-22.)
  - author_retr_rate_external: weight 100 (a FIRST or LAST author's own
    personal retraction RATE, same weight/cap idiom as publisher_retr_rate/
    journal_retr_rate_external above -- their claimed-DOI list from the
    ORCID Public API as denominator, intersected against the full
    Retraction Watch csv by DOI as numerator, so unlike the venue sensors
    it needs no name matching at all. Restricted to first/last position
    only -- "directly responsible for the outcome" per the user's own
    framing, as opposed to coauthor_other_misconduct's broader "shares a
    cluster with someone who..." scope. History, all 2026-07-22 (same day):
    (1) started as RATE * weight 1.0 (i.e. weight = ceiling at a theoretical
    100% rate); (2) user clarified they actually wanted COUNT * weight 1.0
    ("Jingshan Shi has 4 retracted works... that should add 4 points"),
    which fixed the undercounting problem (a 37.6%-rate/50-retraction author
    scored only +0.376 under (1)) but then let an extreme repeat offender
    (248 retracted works) contribute +248 uncapped, alone dwarfing every
    other candidate; (3) capped the count at 10 -- fixed the dominance but
    is arguably an odd unit (10 raw retracted-works, not comparable to any
    other signal's scale); (4) settled on RATE * 100, capped at 10 (same
    cap key, unchanged) -- same [0.01%-1%-typical/tens-of-percent-tail]
    shape and weight/cap recipe as publisher_retr_rate/
    journal_retr_rate_external, so this signal now reads on the same scale
    as its siblings instead of being a one-off. The raw retracted-works
    COUNT is still shown in the evidence text for context.)
  - journal_retr_rate (graph-internal) was REMOVED from the score entirely
    2026-07-22, on user feedback: it and journal_integrity_flag_count's
    check 3 (journal_integrity_check.py) measured the SAME underlying fact
    -- this journal's retraction rate in our own seeded graph -- one
    continuously, one as a >10% threshold, so a paper could be scored twice
    for one real cause. journal_retr_rate_external above is the real,
    non-redundant replacement. The property itself still exists as a Tier-B
    GDS input feature (gds_node_classification.py) -- only removed from
    Tier-A scoring/display.
  - publisher_retr_rate / country_retr_rate / journal_retr_rate_external are
    CAPPED (added 2026-07-22, see capped_contribution() below), after a real
    problem surfaced the same day: these three rates are extremely
    heavy-tailed (most candidates ~0.01%-1%, but a handful of Hindawi-family
    journals sit at 8%-26%), so NO single linear weight works -- turn it up
    enough to matter for a typical paper and the tail journals/publishers get
    a proportionally huge boost, enough alone to jump to #1 regardless of any
    other evidence about that specific paper; turn it down to tame that and
    it goes back to invisible for everyone else. There is also a conceptual
    problem, not just a numeric one: a journal/publisher/country's aggregate
    rate is evidence about the VENUE, not about THIS paper -- the same
    ecological-not-direct category as coauthor_other_misconduct (plan.md §0:
    same person/place ≠ same responsibility) -- so it should never be able to
    outrank direct per-paper evidence (an ORI finding, this paper's own
    AI-text tells) purely on venue association. Fix: weight is set high
    enough to differentiate the normal range, but contribution = min(rate *
    weight, cap) -- a hard ceiling per signal (currently 2.0 for
    publisher/journal, 1.5 for country) so the extreme tail can never
    dominate no matter how elevated the rate. The raw rate is still shown in
    the evidence text/JSON; only the SCORED contribution is capped.

Score = sum of (flag_count * weight) for each sensor, with the three signals
above additionally capped per-signal (see capped_contribution()).

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

import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

RUNS_DIR = REPO_ROOT / "runs"


def load_paperconan_runs() -> dict:
    """Returns {doi: meta} for each runs/<doi__>/meta.yaml present.

    The filesystem (not the graph) is the source of truth for paperconan
    verdicts -- meta.yaml is what a human/agent hand-edits during
    adjudication (see runs/*/CONCLUSION.md), so reading it directly here
    means a corrected verdict takes effect on the next scoring run with no
    separate "wire it into the graph" step to remember. build_review_page.py
    imports this same function so both scripts agree exactly on what a
    paperconan verdict is."""
    out: dict[str, dict] = {}
    if not RUNS_DIR.exists():
        return out
    for meta_path in RUNS_DIR.glob("*/meta.yaml"):
        try:
            meta = yaml.safe_load(meta_path.read_text()) or {}
        except (yaml.YAMLError, OSError):
            continue
        doi = meta.get("doi")
        if doi:
            meta["_dir"] = meta_path.parent.name
            out[doi] = meta
    return out

# Scoring weights. Two families, both explainable (plan.md §0: every point maps
# to a flag):
#   sensor flags  — per-paper Phase-4 sensor hits
#   graph features — Neo4j-derived, explainable (co-author misconduct proximity,
#                    journal retraction rate)
# gds_misconduct_prob is deliberately NOT weighted in — it is a weak, capped,
# domain-shifted learned prior (see gds_node_classification.py) and rides along
# only as a labeled secondary column for the reviewer.
#
# paperconan_needs_human / paperconan_confirmed (added 2026-07-21): the ONE
# exception to "soft inputs stay unscored" below, and only for these two
# specific ADJUDICATED verdicts (never the raw high/medium/low counts, which
# stay unscored — see load_paperconan_runs() above). Added after a real,
# quantified finding (10.1038/s41586-024-08248-5: an 8-decimal exact
# relationship between 2 of 3 nominal replicate columns that the 3rd doesn't
# share, plus a ~1.2e-6-by-chance measurement duplication) sat unscored while
# an earlier, wrong "false_positive" adjudication of the SAME data had also
# gone unscored — i.e. the score was blind to this signal in both directions,
# which defeated the point of running paperconan at all. Weighted comparably
# to the other strongest fact-based signals here (needs_human ~ pubmed_eoc,
# confirmed ~ ori_finding) because a directly-opened, quantified anomaly in
# the paper's own source data is that strong a signal once a human/agent has
# actually adjudicated it — this is NOT the raw detector count (which stays
# excluded, see below), only the post-adjudication verdict.
#
# DO NOT add image-forensics, PubPeer, GDS, raw paperconan severity counts, or
# reference_integrity_flag_count keys here. Those remain soft / signal-not-
# verdict inputs shown as labelled review context only (plan.md §0); scoring
# them would silently turn a hypothesis into a weighted accusation.
#   reference_integrity_flag_count excluded 2026-07-20: Route 1 (DOI-based
#   lookup against Crossref, now cross-checked against the universal doi.org
#   resolver) is precise, but Route 2 (title/author bibliographic search when a
#   reference has no DOI) flagged 71% of the corpus HIGH -- overwhelmingly
#   real, legitimate microbiology citations that just don't index well for
#   title search (Bergey's Manual taxonomic chapters, pre-DOI species-naming
#   authorities, LPSN, gray literature). Until Route 2 is fixed or split out,
#   its counts are noise, not signal -- see sensors/reference_integrity_checker.py.
# This dict is the fallback/default source of truth — build_review_page.py and
# flag_evidence_report.py import WEIGHTS (see #7 in BUG.md), which is DEFAULT_WEIGHTS
# overlaid with config/weights.yaml if present (see load_weights() below). Keep every
# weight here, not copied.
DEFAULT_WEIGHTS = {
    # sensor flags
    "retracted_citation_flag_count": 3.0,
    "external_retracted_citation_flag_count": 2.0,
    "journal_integrity_flag_count": 1.0,
    "ai_text_tell_flag_count": 2.0,
    "p_value_hacking_flag_count": 0.5,
    "pubmed_eoc_flag": 2.5,
    "pubmed_erratum_flag": 0.3,
    "ori_finding_flag": 4.0,
    # graph features (Neo4j)
    "coauthor_other_misconduct": 1.5,   # per probable-person co-author with a misconduct paper elsewhere
    "institution_retr_rate": 1.5,       # rate in [0,1]; max across a paper's INVOLVES institutions
                                         # (see graph_processing/institution_retraction_rate.py)
    "crossref_correction_flag_count": 0.3,  # Crossref-deposited correction notices on this DOI;
                                             # low weight, corrections are often benign (see refresh_correction_history.py)
    "publisher_retr_rate": 100.0,        # EXTERNAL rate (full RW csv / Crossref total-dois), not graph-scoped
                                          # (see graph_processing/publisher_retraction_rate.py) -- CAPPED, see below
    "publisher_retr_rate_cap": 2.0,       # ceiling on this signal's contribution -- see CAPPED_KEYS/capped_contribution()
    "country_retr_rate": 1000.0,         # EXTERNAL rate (full RW csv / OpenAlex works count), max across countries
                                          # (see graph_processing/country_retraction_rate.py) -- CAPPED, see below
    "country_retr_rate_cap": 1.5,        # ceiling on this signal's contribution -- see CAPPED_KEYS/capped_contribution()
    "journal_retr_rate_external": 100.0, # EXTERNAL rate (full RW csv / Crossref Journals API total-dois), not graph-scoped
                                          # (see graph_processing/journal_retraction_rate_external.py) -- CAPPED, see below
    "journal_retr_rate_external_cap": 2.0,  # ceiling on this signal's contribution -- see CAPPED_KEYS/capped_contribution()
    "author_retr_rate_external": 100.0,  # a first/last author's OWN retraction RATE (ORCID claimed-works
                                          # denominator, RW-by-DOI numerator; see graph_processing/author_
                                          # retraction_rate_external.py) -- same weight/cap idiom as
                                          # publisher_retr_rate/journal_retr_rate_external. CAPPED, see below.
    "author_retr_rate_external_cap": 10.0,  # ceiling on this signal's contribution -- see CAPPED_KEYS/capped_contribution()
    # paperconan (filesystem, not the graph — see load_paperconan_runs() above)
    "paperconan_needs_human": 2.0,      # an opened, quantified anomaly that survived adjudication
    "paperconan_confirmed": 4.0,        # tied with ori_finding_flag as the strongest signal here
}

WEIGHTS_CONFIG_PATH = REPO_ROOT / "config" / "weights.yaml"


def load_weights(path: Path = WEIGHTS_CONFIG_PATH) -> dict:
    """DEFAULT_WEIGHTS overlaid with config/weights.yaml, if present.

    Lets a reviewer retune the scoring formula (config/weights.yaml, under
    git for an audit trail) without touching code. Unknown keys in the YAML
    are ignored with a warning rather than silently scoring an unweighted
    flag; missing keys fall back to the coded default so a partial override
    file still produces a complete, explainable score."""
    weights = dict(DEFAULT_WEIGHTS)
    if not path.exists():
        return weights
    try:
        overrides = yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError) as e:
        print(f"warning: could not read {path} ({e}); using default weights", file=sys.stderr)
        return weights
    for key, value in overrides.items():
        if key not in weights:
            print(f"warning: {path} has unknown weight key {key!r}; ignoring", file=sys.stderr)
            continue
        weights[key] = float(value)
    return weights


WEIGHTS = load_weights()

# Which WEIGHTS keys are capped, and the WEIGHTS key holding their ceiling --
# see the "publisher_retr_rate / country_retr_rate / journal_retr_rate_external
# are CAPPED" docstring note above for why. build_review_page.py and
# flag_evidence_report.py both import capped_contribution() (not just WEIGHTS)
# so the displayed per-row score always matches what's actually summed here.
CAPPED_KEYS = {
    "publisher_retr_rate": "publisher_retr_rate_cap",
    "country_retr_rate": "country_retr_rate_cap",
    "journal_retr_rate_external": "journal_retr_rate_external_cap",
    "author_retr_rate_external": "author_retr_rate_external_cap",
}


def capped_contribution(key: str, rate: float) -> float:
    """rate * WEIGHTS[key], ceilinged at WEIGHTS[CAPPED_KEYS[key]] for the
    four heavy-tailed rate signals in CAPPED_KEYS (publisher/country/journal/
    author retraction rates)."""
    contrib = rate * WEIGHTS[key]
    cap_key = CAPPED_KEYS.get(key)
    if cap_key:
        return min(contrib, WEIGHTS[cap_key])
    return contrib


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
       coalesce(p.external_retracted_citation_flag_count, 0) AS ext_ret_count,
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
       coalesce(p.institution_retr_rate, 0.0) AS institution_retr_rate,
       p.institution_retr_rate_name AS institution_retr_rate_name,
       p.institution_retr_rate_n AS institution_retr_rate_n,
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
       p.ai_text_tell_flags AS ai_flags,
       p.p_value_hacking_flags AS pval_flags
"""


def calculate_score(row: dict) -> float:
    """Weighted explainable score (sensor flags + graph features + paperconan verdict)."""
    adj = row.get("paperconan_adjudication")
    return (
        row["ret_count"] * WEIGHTS["retracted_citation_flag_count"] +
        row["ext_ret_count"] * WEIGHTS["external_retracted_citation_flag_count"] +
        # reference_integrity_flag_count deliberately excluded -- see WEIGHTS comment above
        row["journal_count"] * WEIGHTS["journal_integrity_flag_count"] +
        row["ai_count"] * WEIGHTS["ai_text_tell_flag_count"] +
        row["pval_count"] * WEIGHTS["p_value_hacking_flag_count"] +
        row["eoc_flag"] * WEIGHTS["pubmed_eoc_flag"] +
        row["erratum_flag"] * WEIGHTS["pubmed_erratum_flag"] +
        row["ori_flag"] * WEIGHTS["ori_finding_flag"] +
        row["coauthor_misconduct"] * WEIGHTS["coauthor_other_misconduct"] +
        row["institution_retr_rate"] * WEIGHTS["institution_retr_rate"] +
        capped_contribution("publisher_retr_rate", row["publisher_retr_rate"]) +
        capped_contribution("country_retr_rate", row["country_retr_rate"]) +
        capped_contribution("journal_retr_rate_external", row["journal_retr_rate_external"]) +
        capped_contribution("author_retr_rate_external", row["author_retr_rate_external"]) +
        row["correction_count"] * WEIGHTS["crossref_correction_flag_count"] +
        (WEIGHTS["paperconan_needs_human"] if adj == "needs_human" else 0.0) +
        (WEIGHTS["paperconan_confirmed"] if adj == "confirmed" else 0.0)
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

    paperconan_runs = load_paperconan_runs()

    # Calculate scores
    results = []
    for row in rows:
        pc = paperconan_runs.get(row["doi"])
        row["paperconan_adjudication"] = pc.get("adjudicated") if pc else None
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
            "external_retracted_citation_count": row["ext_ret_count"],
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
            "institution_retr_rate": round(row["institution_retr_rate"], 3),
            "institution_retr_rate_name": row["institution_retr_rate_name"] or "",
            "publisher_retr_rate": round(row["publisher_retr_rate"], 5),
            "publisher_retr_rate_name": row["publisher_retr_rate_name"] or "",
            "country_retr_rate": round(row["country_retr_rate"], 5),
            "country_retr_rate_name": row["country_retr_rate_name"] or "",
            "journal_retr_rate_external": round(row["journal_retr_rate_external"], 5),
            "author_retr_rate_external": round(row["author_retr_rate_external"], 5),
            "author_retr_rate_external_n": row["author_retr_rate_external_n"],
            "author_retr_rate_external_name": row["author_retr_rate_external_name"] or "",
            "author_retr_rate_external_position": row["author_retr_rate_external_position"] or "",
            "correction_count": row["correction_count"],
            "paperconan_adjudication": row["paperconan_adjudication"] or "",
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
        "external_retracted_citation_count",
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
        "institution_retr_rate",
        "institution_retr_rate_name",
        "publisher_retr_rate",
        "publisher_retr_rate_name",
        "country_retr_rate",
        "country_retr_rate_name",
        "journal_retr_rate_external",
        "author_retr_rate_external",
        "author_retr_rate_external_n",
        "author_retr_rate_external_name",
        "author_retr_rate_external_position",
        "correction_count",
        "paperconan_adjudication",
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
    weights_source = WEIGHTS_CONFIG_PATH if WEIGHTS_CONFIG_PATH.exists() else "built-in defaults"
    print(f"\nWeights (explainable score, source: {weights_source}):", file=sys.stderr)
    for sensor, weight in WEIGHTS.items():
        print(f"  {sensor}: {weight}", file=sys.stderr)

    # Top 5 by score
    print(f"\nTop 5 by score:", file=sys.stderr)
    for i, r in enumerate(results[:5], 1):
        gds = f" | GDS {r['gds_misconduct_prob']}{'⚑' if r['gds_flagged']=='Y' else ''}" if r['gds_misconduct_prob'] != "" else ""
        print(f"  {i}. [{r['score']:.1f}] {r['title'][:70]}...", file=sys.stderr)
        print(f"     ret{r['retracted_citation_count']} extret{r['external_retracted_citation_count']} "
              f"ref{r['reference_integrity_count']} "
              f"jrnl{r['journal_integrity_count']} ai{r['ai_text_tell_count']} "
              f"coauthor-misconduct{r['coauthor_misconduct']} "
              f"irr{r['institution_retr_rate']} prr{r['publisher_retr_rate']} crr{r['country_retr_rate']} "
              f"jre{r['journal_retr_rate_external']} are_n{r['author_retr_rate_external_n']} "
              f"corr{r['correction_count']}{gds}", file=sys.stderr)
        print(f"     {r['doi']} ({r['journal']})", file=sys.stderr)


if __name__ == "__main__":
    main()
