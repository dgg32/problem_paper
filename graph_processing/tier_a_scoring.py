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
  - pubmed_eoc_flag: weight 10.0 (raised from 2.5, 2026-07-22, user request --
    Expression of Concern is a formal, dated, journal-issued fact, not a
    community opinion; found via PubMed's CommentsCorrectionsList, see
    graph_processing/refresh_editorial_notices.py.
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
  - institution_retr_rate_external: an institution's retraction rate against
    an OpenAlex works_count denominator (via its ROR ID -- no name-search
    ambiguity at all, 100% ROR coverage on this graph's Institution nodes).
    REPLACES the graph-internal institution_retr_rate in scoring (2026-07-22,
    same day as the minmax update below, user feedback: the graph-internal
    rate was "correct but too high to be intuitive" -- same inflation problem
    already fixed for journal_retr_rate). institution_retr_rate and
    institution_global_retraction_count both stay as unscored review-card
    context; see graph_processing/institution_retraction_rate.py.
  - crossref_correction_flag_count: weight 0.3 (tied with pubmed_erratum_flag
    -- found via Crossref's updates:{doi} reverse lookup, i.e. a correction
    notice's own record explicitly links back to this DOI (update-to); a
    real, dated, publisher-deposited fact, but corrections are routinely
    benign (typo/affiliation fixes), so kept low deliberately. See
    graph_processing/refresh_correction_history.py, added 2026-07-21.)
  - publisher_retr_rate: weight 1.5 (a publisher's retraction rate computed
    from the FULL Retraction Watch csv over a Crossref Members API total-
    dois denominator -- NOT scoped to our own retraction-seeded graph (same
    reasoning as institution_retr_rate_external above), so it is a genuine
    external base rate, not a graph-relative one. See
    graph_processing/publisher_retraction_rate.py,
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
    COUNT is still shown in the evidence text for context.
    SUPERSEDED 2026-07-22, same day, user request: MERGED with
    coauthor_other_misconduct into 4 count-based minmax buckets
    (mid_any_count/mid_misconduct_count/fl_any_count/fl_misconduct_count,
    see MINMAX_KEYS below and config/weights.yaml) split by author position
    (first/last vs middle) x severity (misconduct-reason vs any other
    reason) in a 1:2:2:4 ratio. author_retr_rate_external the RATE still
    exists on AuthorInstance/Paper (author_retraction_rate_external.py) for
    display/context, but the SCORED contribution now comes from the count-
    based fl_* buckets, not this rate.)
  - journal_retr_rate (graph-internal) was REMOVED from the score entirely
    2026-07-22, on user feedback: it and journal_integrity_flag_count's
    check 3 (journal_integrity_check.py) measured the SAME underlying fact
    -- this journal's retraction rate in our own seeded graph -- one
    continuously, one as a >10% threshold, so a paper could be scored twice
    for one real cause. journal_retr_rate_external above is the real,
    non-redundant replacement. The property itself still exists as a Tier-B
    GDS input feature (gds_node_classification.py) -- only removed from
    Tier-A scoring/display.
  - institution_retr_rate_external / publisher_retr_rate / country_retr_rate /
    journal_retr_rate_external are all MINMAX-SCORED (see
    minmax_contribution() below), not weighted-and-capped. mid_any_count/
    mid_misconduct_count/fl_any_count/fl_misconduct_count (the merged
    coauthor_other_misconduct + author_retr_rate_external replacement, see
    below) use the same mechanism.
    History: these four rate-based signals are entity-level (not per-paper)
    retraction rates, and
    are extremely heavy-tailed (most candidates ~0.01%-1%, a handful of
    Hindawi-family journals/repeat-offender authors sit at 8%-95%), so no
    single linear weight ever worked -- turn it up enough to matter for a
    typical paper and the tail alone can jump to #1 regardless of any other
    evidence about that specific paper; turn it down to tame that and it goes
    invisible for everyone else. First fix (2026-07-22, same day): contribution
    = min(rate * weight, cap) -- a hard per-signal ceiling (2.0 publisher/
    journal, 1.5 country, 10.0 author) so the tail could never dominate. This
    worked, but the user then proposed something better: since a raw cap
    still has an arbitrary-feeling ceiling number disconnected from the actual
    data, MINMAX-SCALE each signal against its own observed worst-in-corpus
    value instead -- the single worst offender in a category (e.g. an author
    with an 80% personal retraction rate) scores a fixed target (10 by
    default, lowered from an initial 20 -- 2026-07-22, same day, user
    request), and every other entity in that SAME category scores
    proportionally less (a 20%-rate author scores 10 * 20/80 = 2.5). This
    folded institution_retr_rate's plain linear weight into the same minmax
    mechanism too (on request, since it's the same kind of entity-level rate)
    -- then, later the same day, institution_retr_rate itself was REPLACED by
    institution_retr_rate_external (OpenAlex works_count via ROR ID as an
    unambiguous external denominator, no name-search needed) once the user
    flagged the graph-internal rate as "too high to be intuitive" -- same
    inflation problem, same fix, as journal_retr_rate_external before it. See
    minmax_contribution()/compute_corpus_maxes() below and plan.md's
    2026-07-22 minmax-scoring update for the full rationale, including the
    one real tradeoff worth knowing: minmax is inherently MORE sensitive to a
    single new extreme outlier than a fixed cap was -- one future noisy data
    point becomes the anchor for every other entity's score in that category,
    not just its own. Checked live before shipping: all four current maxima
    (this rate-based family; the count-based fl_*/mid_* buckets are a
    separate, later addition -- see their own note above and in
    config/weights.yaml) are backed by reasonably large samples (publisher
    8.4% at n=11,524, journal 31.0% at n=157, country 0.33% at n=2,418;
    institution_retr_rate_external's max is European Society of Cardiology
    at 0.101%, n=1/994 -- small numerator but a large denominator, same
    shape as the other external rates) -- not a fragile single-paper
    artifact.
    There is also a conceptual reason these four are treated specially
    (unrelated to the numeric heavy-tail problem): a journal/publisher/
    country/institution's aggregate rate is evidence about the ENTITY,
    not about THIS paper -- the same ecological-not-direct category as
    the fl_*/mid_* co-author signals above (plan.md §0: same person/place ≠
    same responsibility) -- so it should never be able to outrank direct
    per-paper evidence (an ORI finding, this paper's own AI-text tells)
    purely on venue association. The raw rate is still shown in the
    evidence text/JSON; only the SCORED contribution is minmax-scaled.

Score = sum of (flag_count * weight) for each sensor, with the entity-rate
and co-author-severity signals above all minmax-scaled per-signal instead
(see minmax_contribution()).

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
    "pubmed_eoc_flag": 10.0,
    "pubmed_erratum_flag": 0.3,
    "ori_finding_flag": 4.0,
    # graph features (Neo4j)
    "crossref_correction_flag_count": 0.3,  # Crossref-deposited correction notices on this DOI;
                                             # low weight, corrections are often benign (see refresh_correction_history.py)
    # coauthor_other_misconduct + author_retr_rate_external MERGED 2026-07-22
    # (user request) into 4 count-based minmax buckets, split by author
    # position (first/last vs middle) x severity (misconduct-reason vs any
    # other reason), in a 1:2:2:4 ratio -- see config/weights.yaml's comment
    # for the full reasoning (why minmax instead of literal flat points).
    "mid_any_minmax_target": 2.5,
    "mid_misconduct_minmax_target": 5.0,
    "fl_any_minmax_target": 5.0,
    "fl_misconduct_minmax_target": 10.0,
    # The four entity-level retraction-RATE signals below are all MINMAX-SCALED
    # against their own observed worst-in-corpus value (see minmax_contribution()
    # / MINMAX_KEYS below), not multiplied by a raw weight -- these "_minmax_target"
    # values are each signal's target score for the single worst offender in its
    # category; every other entity in the same category scores proportionally
    # less. Replaces the old weight+cap idiom entirely (2026-07-22, user request).
    "institution_retr_rate_minmax_target": 10.0,       # EXTERNAL rate (graph_processing/institution_retraction_rate.py)
    "publisher_retr_rate_minmax_target": 10.0,         # EXTERNAL rate (graph_processing/publisher_retraction_rate.py)
    "country_retr_rate_minmax_target": 10.0,           # EXTERNAL rate (graph_processing/country_retraction_rate.py)
    "journal_retr_rate_external_minmax_target": 10.0,  # EXTERNAL rate (graph_processing/journal_retraction_rate_external.py)
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

# Which raw-rate keys are minmax-scored, and the WEIGHTS key holding each
# one's target score for the worst-in-corpus entity -- see the "MINMAX-SCORED"
# docstring note above for why. build_review_page.py and flag_evidence_report.py
# both import minmax_contribution()/compute_corpus_maxes() (not just WEIGHTS)
# so the displayed per-row score always matches what's actually summed here.
MINMAX_KEYS = {
    "institution_retr_rate_external": "institution_retr_rate_minmax_target",
    "publisher_retr_rate": "publisher_retr_rate_minmax_target",
    "country_retr_rate": "country_retr_rate_minmax_target",
    "journal_retr_rate_external": "journal_retr_rate_external_minmax_target",
    "mid_any_count": "mid_any_minmax_target",
    "mid_misconduct_count": "mid_misconduct_minmax_target",
    "fl_any_count": "fl_any_minmax_target",
    "fl_misconduct_count": "fl_misconduct_minmax_target",
}

# Populated once per run by compute_corpus_maxes(), BEFORE any row is scored --
# read by minmax_contribution(). Must be computed from the FULL not-yet-retracted
# candidate population, never a --top-sliced subset, or the worst-in-corpus
# anchor would shift depending on an unrelated CLI flag.
CORPUS_MAXES: dict[str, float] = {}


def compute_corpus_maxes(rows: list[dict]) -> dict[str, float]:
    """Observed worst-in-corpus value for each MINMAX_KEYS entry, across every
    row passed in. Call once per script run with the full candidate list,
    before scoring/sorting/slicing any of it."""
    global CORPUS_MAXES
    CORPUS_MAXES = {key: max((row.get(key) or 0.0) for row in rows) for key in MINMAX_KEYS}
    return CORPUS_MAXES


def get_corpus_max(key: str) -> float:
    """Current worst-in-corpus value for a MINMAX_KEYS entry, as of the last
    compute_corpus_maxes() call. Callers in other modules should use this
    (not a direct `from tier_a_scoring import CORPUS_MAXES`) -- a plain import
    binds a snapshot at import time and won't see later compute_corpus_maxes()
    updates, since that reassigns the name rather than mutating it in place."""
    return CORPUS_MAXES.get(key, 0.0)


def minmax_contribution(key: str, rate: float) -> float:
    """(rate / worst-in-corpus-for-this-key) * WEIGHTS[MINMAX_KEYS[key]] --
    the single worst offender in this category scores WEIGHTS[MINMAX_KEYS[key]]
    (10 by default), every other entity in the same category scores
    proportionally less. Requires compute_corpus_maxes(rows) to have already
    been called this run. NOTE: more sensitive to a single new extreme outlier
    than a fixed cap was -- see the module docstring's MINMAX-SCORED note."""
    corpus_max = CORPUS_MAXES.get(key, 0.0)
    if corpus_max <= 0:
        return 0.0
    return (rate / corpus_max) * WEIGHTS[MINMAX_KEYS[key]]


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
       coalesce(p.mid_any_count, 0) AS mid_any_count,
       coalesce(p.mid_misconduct_count, 0) AS mid_misconduct_count,
       coalesce(p.fl_any_count, 0) AS fl_any_count,
       coalesce(p.fl_misconduct_count, 0) AS fl_misconduct_count,
       coalesce(p.institution_retr_rate, 0.0) AS institution_retr_rate,
       p.institution_retr_rate_name AS institution_retr_rate_name,
       p.institution_retr_rate_n AS institution_retr_rate_n,
       coalesce(p.institution_retr_rate_external, 0.0) AS institution_retr_rate_external,
       p.institution_retr_rate_external_name AS institution_retr_rate_external_name,
       p.institution_retr_rate_external_n AS institution_retr_rate_external_n,
       p.institution_retr_rate_external_total AS institution_retr_rate_external_total,
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
       p.last_author_name AS last_author_name,
       coalesce(p.last_author_retr_n, 0) AS last_author_retr_n,
       coalesce(p.last_author_retr_misconduct_n, 0) AS last_author_retr_misconduct_n,
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
        minmax_contribution("mid_any_count", row["mid_any_count"]) +
        minmax_contribution("mid_misconduct_count", row["mid_misconduct_count"]) +
        minmax_contribution("fl_any_count", row["fl_any_count"]) +
        minmax_contribution("fl_misconduct_count", row["fl_misconduct_count"]) +
        minmax_contribution("institution_retr_rate_external", row["institution_retr_rate_external"]) +
        minmax_contribution("publisher_retr_rate", row["publisher_retr_rate"]) +
        minmax_contribution("country_retr_rate", row["country_retr_rate"]) +
        minmax_contribution("journal_retr_rate_external", row["journal_retr_rate_external"]) +
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

    compute_corpus_maxes(rows)
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
            "mid_any_count": row["mid_any_count"],
            "mid_misconduct_count": row["mid_misconduct_count"],
            "fl_any_count": row["fl_any_count"],
            "fl_misconduct_count": row["fl_misconduct_count"],
            "institution_retr_rate": round(row["institution_retr_rate"], 3),
            "institution_retr_rate_name": row["institution_retr_rate_name"] or "",
            "institution_retr_rate_external": round(row["institution_retr_rate_external"], 5),
            "institution_retr_rate_external_name": row["institution_retr_rate_external_name"] or "",
            "publisher_retr_rate": round(row["publisher_retr_rate"], 5),
            "publisher_retr_rate_name": row["publisher_retr_rate_name"] or "",
            "country_retr_rate": round(row["country_retr_rate"], 5),
            "country_retr_rate_name": row["country_retr_rate_name"] or "",
            "journal_retr_rate_external": round(row["journal_retr_rate_external"], 5),
            "first_author_name": row["first_author_name"] or "",
            "first_author_retr_n": row["first_author_retr_n"],
            "first_author_retr_misconduct_n": row["first_author_retr_misconduct_n"],
            "last_author_name": row["last_author_name"] or "",
            "last_author_retr_n": row["last_author_retr_n"],
            "last_author_retr_misconduct_n": row["last_author_retr_misconduct_n"],
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
        "mid_any_count",
        "mid_misconduct_count",
        "fl_any_count",
        "fl_misconduct_count",
        "institution_retr_rate",
        "institution_retr_rate_name",
        "institution_retr_rate_external",
        "institution_retr_rate_external_name",
        "publisher_retr_rate",
        "publisher_retr_rate_name",
        "country_retr_rate",
        "country_retr_rate_name",
        "journal_retr_rate_external",
        "first_author_name",
        "first_author_retr_n",
        "first_author_retr_misconduct_n",
        "last_author_name",
        "last_author_retr_n",
        "last_author_retr_misconduct_n",
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
              f"mid_any{r['mid_any_count']} mid_mc{r['mid_misconduct_count']} "
              f"fl_any{r['fl_any_count']} fl_mc{r['fl_misconduct_count']} "
              f"irr_ext{r['institution_retr_rate_external']} prr{r['publisher_retr_rate']} crr{r['country_retr_rate']} "
              f"jre{r['journal_retr_rate_external']} "
              f"corr{r['correction_count']}{gds}", file=sys.stderr)
        print(f"     {r['doi']} ({r['journal']})", file=sys.stderr)


if __name__ == "__main__":
    main()
