#!/usr/bin/env python3
"""
build_review_page.py — Phase 5 (minimal): render the Tier-A triage queue as a
single self-contained, PRIVATE HTML review page for a human reviewer.

Why a local file, never a published/shared artifact: this page shows
retraction- and misconduct-adjacent flags tied to real, named researchers.
plan.md §7 says keep the review queue private during the POC, and §0 says
every output is a hypothesis for human review, not an accusation. So this
writes review/index.html for the user to open locally; it is never uploaded
anywhere. That path is git-ignored (review/*.html); a tracked review/README.md
documents the privacy intent.

It mirrors tier_a_scoring.py's ranking exactly (same WEIGHTS), and for each
top-N candidate expands every contributing signal into named, sourced
evidence -- including the two signals added 2026-07-19 that the older
flag_evidence_report.py predates:
  - Expression of Concern (pubmed_eoc_*, weight 10.0) with date + notice DOI
  - PubPeer comment count + category breakdown + author-response note
    (from data/flags/pubpeer_comment_categories.json), shown as review
    context but NOT scored (comment count = attention, not guilt).

Copy discipline (plan.md §5, §0): header reads "Papers flagged for human
review"; a standing banner states the facts-not-verdicts framing; the
coauthor-misconduct block carries the "same person ≠ same responsibility"
caveat verbatim in spirit. No cell asserts fraud.

Usage:
  python graph_processing/build_review_page.py [--top 50]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

OUTPUT = REPO_ROOT / "review" / "index.html"
PUBPEER_CATEGORIES = REPO_ROOT / "data" / "flags" / "pubpeer_comment_categories.json"
RUNS_DIR = REPO_ROOT / "runs"

# Filter chips: key (matches data-tags) -> label, in display order. A chip is
# only rendered if at least one card carries that tag (see tag_counts).
TAG_LABELS = [
    ("eoc", "Expression of Concern"),
    ("ori", "ORI Finding"),
    ("fl-misconduct", "1st/last author: misconduct retraction"),
    ("fl-misconduct-volume", "1st/last author: misconduct retraction (volume)"),
    ("fl-any-retr", "1st/last author: other retraction"),
    ("fl-any-retr-volume", "1st/last author: other retraction (volume)"),
    ("mid-misconduct", "Co-author: misconduct retraction"),
    ("mid-misconduct-volume", "Co-author: misconduct retraction (volume)"),
    ("mid-any-retr", "Co-author: other retraction"),
    ("mid-any-retr-volume", "Co-author: other retraction (volume)"),
    ("cites-retracted", "Cites retracted"),
    ("cites-retracted-ext", "Cites retracted (ext.)"),
    ("self-cite", "Cites own retracted"),
    ("correction", "Crossref correction"),
    ("journal", "Journal integrity"),
    ("journal-high-retr", "High retract rate journal"),
    ("publisher-high-retr", "High retract rate publisher"),
    ("institution-high-retr", "High retract rate institution"),
    ("country-high-retr", "High retract rate country"),
    ("journal-hijack", "Journal hijacking target"),
    ("known-miller", "Known miller co-author"),
    ("cabanac-chatgpt", "ChatGPT text tell (Cabanac)"),
    ("ai", "AI-text tells"),
    ("tortured", "Tortured phrase"),
    ("pval", "p-value pattern"),
    ("erratum", "Erratum"),
    ("pubpeer", "PubPeer"),
    ("pubpeer-allegation", "PubPeer allegation"),
    ("suppl", "Suppl data"),
    ("paperconan", "paperconan"),
    ("image", "Image screen"),
]

# ">=" threshold for the "High retract rate journal/publisher" chips below.
# journal_retr_rate_external / publisher_retr_rate are real-world rates on a
# ~0.01%-1%-typical scale (see journal_retraction_rate_external.py /
# publisher_retraction_rate.py) -- 5% sits clearly above that normal range,
# catching only the genuinely elevated, often well-documented cases (e.g.
# Hindawi's 2023-2024 mass-retraction event at 8.4%) without also flagging
# the long tail of ordinary low-single-digit-percent venues.
HIGH_EXTERNAL_RATE_THRESHOLD = 0.05

# Threshold for the "High retract rate country" chip, calibrated separately
# from HIGH_EXTERNAL_RATE_THRESHOLD above -- country_retr_rate lives on a
# TWO-ORDERS-OF-MAGNITUDE smaller scale (max observed ~0.33%, see
# country_retraction_rate.py), so reusing 5% would never fire. Checked
# 2026-07-22 against the full not-yet-retracted candidate population (796
# papers): country_retr_rate is present (>0) for 34% of candidates -- too
# broad to badge as "notable" on its own, unlike author-retr/institution-
# high-retr below where ANY signal at all is already rare. The top-10%-by-
# rank cutoff measured 0.126%; 0.125% is used here as a clean round number
# at that same cut, catching ~80 of 796 candidates (countries from roughly
# Senegal/Kazakhstan's rate upward) instead of a third of the corpus.
# Re-check this cutoff if the candidate population changes meaningfully.
HIGH_COUNTRY_RATE_THRESHOLD = 0.00125

# 🚩 priority gauge: map the continuous weighted score to 1-5 review-priority
# flags for at-a-glance triage. Thresholds are fixed + documented (shown in the
# page legend) so the gauge is transparent, not a black box. The exact numeric
# score is always kept alongside (sort + tooltip) -- the flags are a visual aid,
# never a replacement for the real number. plan.md's §5 vision was literally a
# "🚩 count" ranking; this honours that idiom over the weighted score.
FLAG_BANDS = [(12, 5), (9, 4), (6, 3), (3, 2), (0.0001, 1)]


def flag_gauge(sc: float) -> int:
    for threshold, flags in FLAG_BANDS:
        if sc >= threshold:
            return flags
    return 0

# Single source of truth — imported, not copied, so the two can never drift
# (was three hand-synced copies; see #7 in BUG.md). tier_a_scoring only touches
# the DB inside main(), so importing the module is side-effect-free.
from tier_a_scoring import (  # noqa: E402
    WEIGHTS, load_paperconan_runs, minmax_contribution, compute_corpus_maxes, get_corpus_max, MINMAX_KEYS,
)

# Same broad misconduct-signal set as flag_evidence_report.py / gds.
MISCONDUCT_REASONS = [
    "Misconduct - Official Investigation(s) and/or Finding(s)",
    "Investigation by ORI", "Paper Mill",
    "Falsification/Fabrication of Data", "Falsification/Fabrication of Image",
    "Falsification/Fabrication of Results", "Manipulation of Images",
    "Manipulation of Results", "Euphemisms for Misconduct", "Misconduct by Author",
]

QUERY = """
MATCH (p:Paper {is_retracted:false})-[:PUBLISHED_IN]->(j:Journal)
RETURN p.doi AS doi, p.title AS title, j.name AS journal,
       toString(p.published_date) AS published_date, p.cited_by_count AS cited_by_count,
       coalesce(p.retracted_citation_flag_count, 0) AS ret_count,
       coalesce(p.external_retracted_citation_flag_count, 0) AS ext_ret_count,
       coalesce(p.reference_integrity_flag_count, 0) AS ref_count,
       coalesce(p.journal_integrity_flag_count, 0) AS journal_count,
       coalesce(p.ai_text_tell_flag_count, 0) AS ai_count,
       coalesce(p.tortured_phrase_flag_count, 0) AS tortured_count,
       coalesce(p.p_value_hacking_flag_count, 0) AS pval_count,
       CASE WHEN p.pubmed_eoc_status = "expression_of_concern" THEN 1 ELSE 0 END AS eoc_flag,
       CASE WHEN p.pubmed_eoc_status = "erratum_only" THEN 1 ELSE 0 END AS erratum_flag,
       p.pubmed_eoc_date AS eoc_date, p.pubmed_eoc_source_doi AS eoc_source_doi,
       CASE WHEN p.ori_finding_doc_url IS NOT NULL THEN 1 ELSE 0 END AS ori_flag,
       p.ori_finding_doc_url AS ori_doc_url, p.ori_respondent_name AS ori_respondent,
       p.ori_finding_date AS ori_date,
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
       coalesce(p.pubpeer_comments_total, 0) AS pubpeer_total,
       p.pubpeer_check_url AS pubpeer_url,
       coalesce(p.pubpeer_has_author_response, false) AS pubpeer_author_response,
       coalesce(p.pmc_suppl_status, "unchecked") AS pmc_suppl_status,
       coalesce(p.has_pmc_suppl, false) AS has_pmc_suppl,
       p.pmc_suppl_url AS pmc_suppl_url,
       p.retracted_citation_flags AS ret_flags,
       p.external_retracted_citation_flags AS ext_ret_flags,
       p.reference_integrity_flags AS ref_flags,
       p.journal_integrity_flags AS journal_flags,
       p.ai_text_tell_flags AS ai_flags,
       p.tortured_phrase_flags AS tortured_flags,
       p.p_value_hacking_flags AS pval_flags
"""

# Restricted to MIDDLE position (2026-07-22) -- first/last co-authors moved
# to the ORCID-strict author_retraction_rate_external.py path (see the
# fl-misconduct/fl-any-retr rows in render_evidence()). A cluster with BOTH
# a misconduct-reason and a non-misconduct-reason retracted paper elsewhere
# counts ONLY as misconduct (mirrors coauthor_retraction_severity.py's
# mutually-exclusive mid_any_count/mid_misconduct_count split) --
# $misconduct=true returns clusters with >=1 misconduct-reason paper;
# $misconduct=false returns clusters with ZERO misconduct-reason papers.
COAUTHOR_QUERY = """
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
RETURN coauthor_name, example_dois[0..2] AS example_dois
ORDER BY coauthor_name
LIMIT 6
"""


# All minmax-scored signals summed in one place -- broken out as its own
# function so each individual scoring chip's live re-rank (toggleScoreChip())
# can subtract just its own term via chip_contributions() below, while
# score() still adds up to the same total. The four entity-rate signals
# (institution/publisher/country/journal) and the four co-author-severity
# buckets (mid_any/mid_misconduct/fl_any/fl_misconduct, merged 2026-07-22
# from coauthor_other_misconduct + author_retr_rate_external -- user request)
# are all MINMAX-SCALED against their own worst-in-corpus value; see
# MINMAX_KEYS in tier_a_scoring.py. The four *_volume siblings (added
# 2026-08-16, user request) score the SAME underlying retraction count a
# second time, minmax-scaled against ITS OWN corpus-worst -- see
# tier_a_scoring.py's DEFAULT_WEIGHTS "VOLUME" note for why this is additive
# rather than a replacement (an author with 1 retraction and one with 17
# score identically in fl_any_count by design; fl_any_volume is what tells
# them apart, at half fl_any_count's weight). Given their own chips/rows
# (2026-08-16, user request, revising the initial fold-together design) so a
# reader can see and independently toggle exactly how much of a paper's
# score came from "tainted at all" vs "how much" -- see chip_contributions().
def minmax_total(r: dict) -> float:
    return (
        minmax_contribution("institution_retr_rate_external", r["institution_retr_rate_external"])
        + minmax_contribution("publisher_retr_rate", r["publisher_retr_rate"])
        + minmax_contribution("country_retr_rate", r["country_retr_rate"])
        + minmax_contribution("journal_retr_rate_external", r["journal_retr_rate_external"])
        + minmax_contribution("mid_any_count", r["mid_any_count"])
        + minmax_contribution("mid_misconduct_count", r["mid_misconduct_count"])
        + minmax_contribution("fl_any_count", r["fl_any_count"])
        + minmax_contribution("fl_misconduct_count", r["fl_misconduct_count"])
        + minmax_contribution("mid_any_volume", r["mid_any_volume"])
        + minmax_contribution("mid_misconduct_volume", r["mid_misconduct_volume"])
        + minmax_contribution("fl_any_volume", r["fl_any_volume"])
        + minmax_contribution("fl_misconduct_volume", r["fl_misconduct_volume"])
    )


def score(r: dict) -> float:
    adj = r.get("paperconan_adjudication")
    return (
        r["ret_count"] * WEIGHTS["retracted_citation_flag_count"]
        + r["ext_ret_count"] * WEIGHTS["external_retracted_citation_flag_count"]
        # reference_integrity_flag_count deliberately excluded -- see tier_a_scoring.py WEIGHTS comment
        + r["journal_count"] * WEIGHTS["journal_integrity_flag_count"]
        + r["ai_count"] * WEIGHTS["ai_text_tell_flag_count"]
        + r["tortured_count"] * WEIGHTS["tortured_phrase_flag_count"]
        + r["pval_count"] * WEIGHTS["p_value_hacking_flag_count"]
        + r["eoc_flag"] * WEIGHTS["pubmed_eoc_flag"]
        + r["erratum_flag"] * WEIGHTS["pubmed_erratum_flag"]
        + r["ori_flag"] * WEIGHTS["ori_finding_flag"]
        + minmax_total(r)
        + r["correction_count"] * WEIGHTS["crossref_correction_flag_count"]
        + (WEIGHTS["paperconan_needs_human"] if adj == "needs_human" else 0.0)
        + (WEIGHTS["paperconan_confirmed"] if adj == "confirmed" else 0.0)
    )


# Chips whose tag corresponds to an actual scored weight (as opposed to
# unscored/context-only tags like pubpeer, journal-hijack, suppl, etc.) --
# 2026-07-22, user request: these chips stop being show/hide filters and
# become live score-inclusion toggles instead (click = zero that signal's
# contribution across every paper and re-rank, nothing gets hidden). Every
# other TAG_LABELS entry keeps the old filter behaviour unchanged, since
# there's nothing for them to zero out.
SCORED_TAGS = {
    "eoc", "ori", "fl-misconduct", "fl-misconduct-volume", "fl-any-retr", "fl-any-retr-volume",
    "mid-misconduct", "mid-misconduct-volume", "mid-any-retr", "mid-any-retr-volume",
    "cites-retracted", "cites-retracted-ext",
    "correction", "journal", "journal-high-retr", "publisher-high-retr",
    "institution-high-retr", "country-high-retr",
    "ai", "tortured", "pval", "erratum", "paperconan",
}


def chip_contributions(r: dict) -> dict[str, float]:
    """Per-signal score contribution for each SCORED_TAGS chip -- all 5
    entity-rate minmax signals plus the 8 co-author-severity buckets
    (fl-misconduct/fl-any-retr/mid-misconduct/mid-any-retr, merged
    2026-07-22 from coauthor_other_misconduct + author_retr_rate_external --
    user request; each split 2026-08-16, user request, into a presence chip
    and its own separate *-volume chip) have their own chip. Embedded as
    JSON on each card (data-contribs) so the browser can subtract any subset
    live without a page reload -- mirrors minmax_total()'s terms exactly,
    just broken out signal-by-signal. Each value here is the PAPER-LEVEL
    total for that tag (matching minmax_total()'s terms exactly, so the chip
    toggle zeroes the right amount); render_evidence()'s individual
    first/last-author rows show a smaller PER-POSITION share of the presence
    total (see its own note) so two qualifying positions on one paper don't
    each claim the whole paper-level contribution -- the volume rows are
    already naturally position-specific and need no such splitting.

    Presence and volume were originally folded into one combined chip; split
    apart on user request so a reader can see, and independently toggle,
    exactly how much of a paper's score came from "tainted at all" (fixed
    per-position unit, deliberately blind to HOW tainted) vs "how much"
    (minmax-scaled against the corpus-worst actual count) -- see
    tier_a_scoring.py's DEFAULT_WEIGHTS "VOLUME" note for the full
    rationale, and plan.md's 2026-08-16 entries for the worked example that
    motivated it (10.1016/j.envres.2024.119440)."""
    adj = r.get("paperconan_adjudication")
    return {
        "eoc": r["eoc_flag"] * WEIGHTS["pubmed_eoc_flag"],
        "ori": r["ori_flag"] * WEIGHTS["ori_finding_flag"],
        "mid-misconduct": minmax_contribution("mid_misconduct_count", r["mid_misconduct_count"]),
        "mid-misconduct-volume": minmax_contribution("mid_misconduct_volume", r["mid_misconduct_volume"]),
        "mid-any-retr": minmax_contribution("mid_any_count", r["mid_any_count"]),
        "mid-any-retr-volume": minmax_contribution("mid_any_volume", r["mid_any_volume"]),
        "fl-misconduct": minmax_contribution("fl_misconduct_count", r["fl_misconduct_count"]),
        "fl-misconduct-volume": minmax_contribution("fl_misconduct_volume", r["fl_misconduct_volume"]),
        "fl-any-retr": minmax_contribution("fl_any_count", r["fl_any_count"]),
        "fl-any-retr-volume": minmax_contribution("fl_any_volume", r["fl_any_volume"]),
        "cites-retracted": r["ret_count"] * WEIGHTS["retracted_citation_flag_count"],
        "cites-retracted-ext": r["ext_ret_count"] * WEIGHTS["external_retracted_citation_flag_count"],
        "correction": r["correction_count"] * WEIGHTS["crossref_correction_flag_count"],
        "journal": r["journal_count"] * WEIGHTS["journal_integrity_flag_count"],
        "journal-high-retr": minmax_contribution("journal_retr_rate_external", r["journal_retr_rate_external"]),
        "publisher-high-retr": minmax_contribution("publisher_retr_rate", r["publisher_retr_rate"]),
        "ai": r["ai_count"] * WEIGHTS["ai_text_tell_flag_count"],
        "tortured": r["tortured_count"] * WEIGHTS["tortured_phrase_flag_count"],
        "pval": r["pval_count"] * WEIGHTS["p_value_hacking_flag_count"],
        "erratum": r["erratum_flag"] * WEIGHTS["pubmed_erratum_flag"],
        "paperconan": (
            WEIGHTS["paperconan_confirmed"] if adj == "confirmed"
            else WEIGHTS["paperconan_needs_human"] if adj == "needs_human" else 0.0
        ),
        "institution-high-retr": minmax_contribution(
            "institution_retr_rate_external", r["institution_retr_rate_external"]),
        "country-high-retr": minmax_contribution("country_retr_rate", r["country_retr_rate"]),
    }


_TAG_RE = re.compile(r"<[^>]+>")


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _count_volume_suffix(count: int, volume: int, unit: str) -> str:
    """Badge text shows the raw retraction-VOLUME behind a count whenever it
    exceeds the count (2026-08-16) -- otherwise a co-author with 1 other
    retracted paper and one with 17 look identical even at a glance, exactly
    the flattening tier_a_scoring.py's fl_any_volume/mid_any_volume now score
    but the collapsed card would still visually hide."""
    return f'{count}' if volume <= count else f'{count}, {volume} {unit} total'


def esc_title(x) -> str:
    """Titles from OpenAlex carry inline formatting tags (<i>, <sub>, <sup>).
    Strip them to plain text, THEN escape — otherwise html.escape turns the
    source tags into visible literal <i>...</i> text."""
    return html.escape(_TAG_RE.sub("", str(x if x is not None else "")))


def load_pubpeer_categories() -> dict:
    """Returns {doi: {category: count}} for non-author-response comments."""
    if not PUBPEER_CATEGORIES.exists():
        return {}
    out: dict[str, dict[str, int]] = {}
    for rec in json.loads(PUBPEER_CATEGORIES.read_text()):
        if rec.get("author_response"):
            continue
        out.setdefault(rec["paper_doi"], {}).setdefault(rec["category"], 0)
        out[rec["paper_doi"]][rec["category"]] += 1
    return out


# Categories from categorize_pubpeer_comments.py that are an actual claim
# ABOUT the paper, not just administrative/procedural chatter -- used for the
# "PubPeer allegation" chip below, a stricter filter than the plain "PubPeer"
# chip (any comment at all, including a methodology question or the author's
# own rebuttal). Deliberately excludes "methodology_question" (a clarification
# request, not a concern) and "author_response"/"uncategorized" (no claim, or
# no rule matched at all) -- see that module's docstring for the full
# category definitions. Still a comment's NATURE, never a verdict (plan.md §0).
PUBPEER_ALLEGATION_CATEGORIES = {
    "official_editorial_action",
    "external_investigation_reference",
    "conflict_of_interest",
    "reference_integrity",
    "image_integrity",
}


# load_paperconan_runs() now lives in tier_a_scoring.py (imported above) --
# paperconan's `needs_human`/`confirmed` verdicts are scored since 2026-07-21
# (see that module's WEIGHTS comment); the raw high/medium/low counts and
# every other verdict stay unscored, same discipline as PubPeer / GDS.

# Once a run is adjudicated (meta.yaml gains an `adjudicated:` verdict and drops
# needs_adjudication), the badge reflects the HUMAN verdict, not the raw count —
# so a benign "26 high" reads calmly, and a real concern reads loud.
ADJ_BADGE = {
    "false_positive": ("badge-pc-ok", "paperconan: reviewed — false positive"),
    "benign":         ("badge-pc-ok", "paperconan: reviewed — benign"),
    "inconclusive":   ("badge-pc",    "paperconan: reviewed — inconclusive"),
    "needs_data":     ("badge-pc",    "paperconan: reviewed — needs data"),
    "needs_human":    ("badge-pc-hi", "paperconan: unresolved anomaly — needs human review"),
    "confirmed":      ("badge-pc-hi", "paperconan: confirmed concern"),
}


def paperconan_badge(pc: dict) -> str:
    """Folded-card badge summarising a paperconan run.

    Priority: non-scan outcome > adjudicated verdict > raw severity (draft).
    A non-scan `outcome` must NOT read as 'clean' (never scanned); once
    adjudicated, the human verdict supersedes the raw count."""
    outcome = pc.get("outcome")
    if outcome == "no_data_files_available":
        return '<span class="badge badge-pc">paperconan: no data</span>'
    if outcome == "no_tabular_data":
        return '<span class="badge badge-pc">paperconan: figures only</span>'
    if outcome:
        return '<span class="badge badge-pc">paperconan: not scanned</span>'
    adj = pc.get("adjudicated")
    if adj:
        cls, label = ADJ_BADGE.get(adj, ("badge-pc", f"paperconan: reviewed — {adj}"))
        return f'<span class="badge {cls}">{esc(label)}</span>'
    # Unadjudicated: raw severity, marked as a draft so it isn't mistaken for a verdict.
    f = pc.get("findings") or {}
    hi, med = f.get("high", 0), f.get("medium", 0)
    draft = " (draft)" if pc.get("needs_adjudication") else ""
    if hi:
        return f'<span class="badge badge-pc-hi">paperconan: {hi} high{draft}</span>'
    if med:
        return f'<span class="badge badge-pc">paperconan: {med} medium{draft}</span>'
    return f'<span class="badge badge-pc">paperconan: clean{draft}</span>'


def image_badge(img: dict) -> str:
    """Folded-card badge for the vendored image-reuse screen (not scored)."""
    n = img.get("n_findings", 0)
    if n:
        return f'<span class="badge badge-pc-hi">🖼 images: {n} reuse</span>'
    return '<span class="badge badge-pc-ok">🖼 images: no reuse</span>'


def render_evidence(r: dict, mid_coauthors_misconduct: list[dict], mid_coauthors_any: list[dict],
                     pp_cats: dict, pc_runs: dict) -> str:
    """Build the expandable per-paper evidence HTML."""
    parts: list[str] = []

    def row(label, contrib, body, help=None, tag=None):
        # publisher_retr_rate/country_retr_rate are real-world-scale rates
        # (typically 0.0001-0.08, see config/weights.yaml) -- at 2 decimals
        # their contribution rounds to "+0.00", which reads as "no score"
        # even though it's a genuine nonzero contributor. Show more decimals
        # whenever 2 would hide a real nonzero value.
        contrib_str = f"+{contrib:.2f}" if contrib == 0 or contrib >= 0.005 else f"+{contrib:.4f}"
        # Optional `help`: supplementary context, not part of the always-
        # visible evidence -- rendered as a small "!" next to the TITLE with
        # a floating popover, pure-CSS hover/focus (no JS, no layout shift --
        # 2026-07-23, user feedback: expand-in-place read as an "expander,"
        # not a help bubble, and pushed the rest of the card down). Neutral
        # accent color, not warn-colored -- it's info, not an alert.
        help_html = ""
        if help:
            help_html = (
                '<span class="help-wrap">'
                '<button class="help-btn" type="button" aria-label="More context">!</button>'
                f'<span class="help-pop">{help}</span>'
                "</span>"
            )
        # Optional `tag`: this row's SCORED_TAGS chip key, if it has one.
        # 2026-07-22, user-noticed inconsistency: toggling a scoring chip
        # already re-ranked the card-level score, but this row (baked in at
        # build time) kept showing its old "+X.XX" regardless -- looked like
        # the chip hadn't done anything if you had a card expanded. JS reads
        # data-tag to strike the row through (and swap in "+0.00") whenever
        # its chip is currently toggled off; data-full preserves the real
        # value so it can be restored exactly, no re-render needed.
        tag_attr = f' data-tag="{tag}"' if tag else ""
        return (
            f'<div class="ev"{tag_attr}><div class="ev-h"><span class="ev-tg"><span class="ev-t">{esc(label)}</span>{help_html}</span>'
            f'<span class="ev-c" data-full="{contrib_str}">{contrib_str}</span></div>'
            f'<div class="ev-b">{body}</div></div>'
        )

    if r["ori_flag"]:
        parts.append(row(
            "ORI finding of research misconduct (federal)",
            WEIGHTS["ori_finding_flag"],
            f'Respondent: 👤 {esc(r["ori_respondent"]) or "unnamed"} &middot; date: {esc(r["ori_date"]) or "unknown"} &middot; '
            f'<a href="{esc(r["ori_doc_url"])}" target="_blank" rel="noopener">Federal Register notice</a>. '
            'An adjudicated federal finding naming this paper by DOI — the strongest fact-based signal here.',
            tag="ori",
        ))

    if r["eoc_flag"]:
        notice = r["eoc_source_doi"]
        link = f' &middot; notice: <a href="https://doi.org/{esc(notice)}" target="_blank" rel="noopener">{esc(notice)}</a>' if notice else ""
        parts.append(row(
            "Expression of Concern (formal journal notice)",
            WEIGHTS["pubmed_eoc_flag"],
            f'Date: {esc(r["eoc_date"]) or "unknown"}{link}. '
            'A formal, dated editorial fact — stronger than community commentary, weaker than a retraction.',
            tag="eoc",
        ))

    def _coauthor_names(clist: list[dict]) -> str:
        return "".join(
            f'<li>👤 <strong>{esc(c["coauthor_name"])}</strong> — '
            + ", ".join(f'<a href="https://doi.org/{esc(d)}" target="_blank" rel="noopener">{esc(d)}</a>' for d in c["example_dois"])
            + '</li>'
            for c in clist
        )

    if r["ret_count"] > 0:
        flags = json.loads(r["ret_flags"] or "[]")

        def _ret_item(f: dict) -> str:
            tail = ", cited AFTER retraction" if f.get("citing_after_retraction") else ""
            self_note = ""
            if f.get("self_citation"):
                authors = ", ".join(f.get("self_citation_authors", [])[:3])
                self_note = f' <span class="self-cite">👤 {esc(authors) if authors else "self-citation"}</span>'
            return (
                f'<li>📃 <a href="https://doi.org/{esc(f.get("cited_retracted_paper_doi"))}" target="_blank" rel="noopener">'
                f'{esc((f.get("cited_retracted_paper_title") or "")[:80])}</a> '
                f'<span class="muted">({esc(", ".join(f.get("retraction_reasons", [])) or "reason unknown")}{tail})</span>'
                f'{self_note}</li>'
            )

        # self-citations first — they're the stronger signal
        flags = sorted(flags, key=lambda f: not f.get("self_citation"))
        items = "".join(_ret_item(f) for f in flags[:4])
        if len(flags) > 4:
            items += f'<li class="muted">…and {len(flags) - 4} more</li>'
        n_self = sum(1 for f in flags if f.get("self_citation"))
        self_help = None
        if n_self:
            self_help = (
                f'{n_self} self-citation(s): the citing paper shares an author (same probable-person cluster) '
                'with the retracted work it cites — the authors are citing their own now-retracted results, a '
                "materially stronger integrity signal than citing a stranger's."
            )
        parts.append(row(
            f'♺ Cites retracted work ({r["ret_count"]})',
            r["ret_count"] * WEIGHTS["retracted_citation_flag_count"],
            f'<ul>{items}</ul>',
            help=self_help,
            tag="cites-retracted",
        ))

    if r["ext_ret_count"] > 0:
        ext_flags = json.loads(r["ext_ret_flags"] or "[]")
        items = "".join(
            f'<li>📃 <a href="https://doi.org/{esc(f.get("cited_retracted_paper_doi"))}" target="_blank" rel="noopener">'
            f'{esc((f.get("cited_retracted_paper_title") or "")[:80])}</a></li>'
            for f in ext_flags[:4]
        )
        if len(ext_flags) > 4:
            items += f'<li class="muted">…and {len(ext_flags) - 4} more</li>'
        parts.append(row(
            f'♺ Cites retracted work — external ({r["ext_ret_count"]})',
            r["ext_ret_count"] * WEIGHTS["external_retracted_citation_flag_count"],
            f'<ul>{items}</ul>',
            help='Retracted per OpenAlex, but outside our own Retraction-Watch-seeded '
                 'corpus — no reason/date/self-citation context available, confirmed retraction status only.',
            tag="cites-retracted-ext",
        ))

    # Journal/publisher-level signals grouped together. The graph-internal
    # `journal_retr_rate` (removed 2026-07-22, plan.md) used to sit here too,
    # but it and journal_integrity_flag_count's check 3 measured the SAME
    # underlying fact (this journal's retraction rate in our own seeded
    # graph) two different ways -- one continuous, one a >10% threshold --
    # so a paper could be scored twice for one real cause. Kept only the
    # flag (journal_integrity_flag_count); journal_retr_rate_external below
    # is the real, non-redundant replacement (a different, external-scale fact).
    if r["journal_count"] > 0:
        flags = json.loads(r["journal_flags"] or "[]")
        reason = flags[0].get("reason") if flags else "flagged"
        parts.append(row(
            "📔 Journal integrity flag",
            r["journal_count"] * WEIGHTS["journal_integrity_flag_count"],
            f'<p>The specific reason for THIS paper: {esc(reason)}</p>',
            help="This flag fires for any ONE of three different checks -- an OA-only "
                 "publisher's journal missing from DOAJ, explicit delisting from Scopus/Web of Science, or "
                 "this journal crossing a >10% retraction-rate threshold measured in OUR OWN graph (the same "
                 "underlying fact as the removed \"Journal retraction rate\" row, just thresholded instead of "
                 "continuous) -- so the same +1.00 score can mean quite different things paper to paper.",
            tag="journal",
        ))

    def minmax_note(key: str) -> str:
        """Help-popover text explaining the minmax scaling for one of the
        entity-level retraction-RATE rows or co-author-severity COUNT/VOLUME
        rows -- see plan.md's 2026-07-22 minmax-scoring update
        (minmax_contribution() in tier_a_scoring.py): the worst offender in
        this category anchors the top of the scale (its target score),
        everyone else scores proportionally less. Count- and volume-based
        keys (fl_*/mid_* co-author buckets, _count added 2026-07-22, _volume
        added 2026-08-16) show the worst-in-corpus value as a plain integer;
        rate-based keys (institution/publisher/country/journal) show it as
        a percentage -- a bare "2" would be misleading formatted as "200%"."""
        target = WEIGHTS[MINMAX_KEYS[key]]
        corpus_max = get_corpus_max(key)
        corpus_max_str = (f'{corpus_max:g}' if key.endswith("_count") or key.endswith("_volume")
                           else f'{corpus_max:.3%}')
        return (f'Minmax-scaled: the worst-in-corpus value for this signal ({corpus_max_str}) scores '
                f'{target:g}; this row scores proportionally less.')

    if r["journal_retr_rate_external"] > 0:
        parts.append(row(
            "📔 Journal retraction rate (external)",
            minmax_contribution("journal_retr_rate_external", r["journal_retr_rate_external"]),
            f'{esc(r["journal"])} has a real-world {r["journal_retr_rate_external"]:.3%} retraction rate '
            f'(Retraction Watch / Crossref Journals API, n={r["journal_retr_rate_external_n"]}).',
            help=minmax_note("journal_retr_rate_external"),
            tag="journal-high-retr",
        ))

    if r["publisher_retr_rate"] > 0:
        parts.append(row(
            "📇 Publisher retraction rate (external)",
            minmax_contribution("publisher_retr_rate", r["publisher_retr_rate"]),
            f'{esc(r["publisher_retr_rate_name"])} has a real-world {r["publisher_retr_rate"]:.3%} retraction rate '
            f'(Retraction Watch / Crossref, n={r["publisher_retr_rate_n"]}).',
            help=minmax_note("publisher_retr_rate"),
            tag="publisher-high-retr",
        ))

    if r["institution_retr_rate_external"] > 0:
        parts.append(row(
            "🏛️ Institution retraction rate (external)",
            minmax_contribution("institution_retr_rate_external", r["institution_retr_rate_external"]),
            f'{esc(r["institution_retr_rate_external_name"])} has a real-world '
            f'{r["institution_retr_rate_external"]:.3%} retraction rate '
            f'(Retraction Watch / OpenAlex via ROR, '
            f'n={r["institution_retr_rate_external_n"]}/{r["institution_retr_rate_external_total"]}).',
            help=minmax_note("institution_retr_rate_external"),
            tag="institution-high-retr",
        ))

    if r["country_retr_rate"] > 0:
        parts.append(row(
            "🏳️‍🌈 Country retraction rate (external)",
            minmax_contribution("country_retr_rate", r["country_retr_rate"]),
            f'{esc(r["country_retr_rate_name"])} has a real-world {r["country_retr_rate"]:.3%} retraction rate '
            f'(Retraction Watch / OpenAlex, n={r["country_retr_rate_n"]}).',
            help=minmax_note("country_retr_rate"),
            tag="country-high-retr",
        ))

    if r["correction_count"] > 0:
        correction_dois = json.loads(r["correction_dois"] or "[]")
        items = "".join(
            f'<li><a href="https://doi.org/{esc(d)}" target="_blank" rel="noopener">{esc(d)}</a></li>'
            for d in correction_dois[:4]
        )
        parts.append(row(
            f'Crossref correction notice ({r["correction_count"]})',
            r["correction_count"] * WEIGHTS["crossref_correction_flag_count"],
            '<p class="caveat">Corrections are often benign (typo/affiliation fixes) -- kept low-weight.</p>'
            f'<ul>{items}</ul>',
            tag="correction",
        ))

    if r["ai_count"] > 0:
        flags = json.loads(r["ai_flags"] or "[]")
        pats = ", ".join(esc(f.get("pattern")) for f in flags[:4])
        if len(flags) > 4:
            pats += f' <span class="muted">…and {len(flags) - 4} more</span>'
        parts.append(row(f'AI-text tells ({r["ai_count"]})', r["ai_count"] * WEIGHTS["ai_text_tell_flag_count"], pats, tag="ai"))

    if r["tortured_count"] > 0:
        tflags = json.loads(r["tortured_flags"] or "[]")
        phrases = ", ".join(f'"{esc(f.get("phrase"))}"' for f in tflags[:4])
        if len(tflags) > 4:
            phrases += f' <span class="muted">…and {len(tflags) - 4} more</span>'
        parts.append(row(
            f'Tortured phrase ({r["tortured_count"]})',
            r["tortured_count"] * WEIGHTS["tortured_phrase_flag_count"],
            f'{phrases} <span class="muted">-- exact match against Cabanac\'s PPS "Favourite '
            f'Tortured Phrases" list, a garbled paraphrase of a standard scientific term '
            f'(e.g. "bosom peril" for breast cancer); near-zero false positive.</span>',
            tag="tortured",
        ))

    if r["pval_count"] > 0:
        flags = json.loads(r["pval_flags"] or "[]")
        note = flags[0].get("reason") if flags else "p-value clustering"
        parts.append(row(f'p-value pattern ({r["pval_count"]})', r["pval_count"] * WEIGHTS["p_value_hacking_flag_count"], esc(note), tag="pval"))

    if r["erratum_flag"]:
        parts.append(row("Erratum on record", WEIGHTS["pubmed_erratum_flag"], "A published erratum exists (weak signal — most errata are benign corrections).", tag="erratum"))

    pc = pc_runs.get(r["doi"])
    pc_adj = pc.get("adjudicated") if pc else None
    if pc_adj in ("needs_human", "confirmed"):
        weight = WEIGHTS["paperconan_confirmed"] if pc_adj == "confirmed" else WEIGHTS["paperconan_needs_human"]
        label = "paperconan: confirmed concern" if pc_adj == "confirmed" else "paperconan: unresolved anomaly"
        parts.append(row(
            label, weight,
            f'{esc(pc.get("top_finding") or "")}. {esc(pc.get("conclusion") or "")} '
            f'<span class="muted">Full run: <code>runs/{esc(pc.get("_dir") or "")}/</code></span>',
            tag="paperconan",
        ))

    # --- author history block, grouped together at the bottom of the scored
    # rows (2026-07-24, user request: "look at one block and know about the
    # author history" -- these 4 rows used to be split apart, mid_misconduct/
    # mid_any near the top and first/last further down). Order: first author,
    # last author, THEN both co-author rows together (misconduct, then
    # non-misconduct last) -- 2026-07-24 fix: mid_misconduct used to sit
    # BEFORE first/last while mid_any sat AFTER, so the two co-author rows
    # never appeared next to each other and the block looked inconsistently
    # ordered card to card depending on which rows were present. Now both
    # co-author rows are adjacent, always after first/last.
    for position, name_key, n_key, mc_key, rate_key, total_key in (
        ("first", "first_author_name", "first_author_retr_n", "first_author_retr_misconduct_n",
         "first_author_retr_rate", "first_author_retr_total"),
        ("last", "last_author_name", "last_author_retr_n", "last_author_retr_misconduct_n",
         "last_author_retr_rate", "last_author_retr_total"),
    ):
        if r[n_key] == 0:
            continue
        is_misconduct = r[mc_key] > 0
        key = "fl_misconduct_count" if is_misconduct else "fl_any_count"
        tag = "fl-misconduct" if is_misconduct else "fl-any-retr"
        # Volume (2026-08-16, split into its own row on user request for
        # transparency): this position's OWN retracted-works count, minmax-
        # scaled against its own corpus-worst -- unlike the presence share
        # below (a fixed per-position unit via minmax_contribution(key, 1)),
        # volume is naturally position-specific already, so no splitting
        # trick is needed: just feed this position's own count straight in.
        # misconduct positions use their misconduct-only subset (r[mc_key]),
        # matching how fl_misconduct_volume itself is computed (see
        # author_retraction_rate_external.py).
        volume_key = "fl_misconduct_volume" if is_misconduct else "fl_any_volume"
        volume_tag = "fl-misconduct-volume" if is_misconduct else "fl-any-retr-volume"
        volume_n = r[mc_key] if is_misconduct else r[n_key]
        misconduct_clause = (
            ', a MISCONDUCT-coded reason among them' if is_misconduct else
            ' (none of this person\'s retractions are misconduct-coded -- half the weight of a misconduct one)'
        )
        author_help = (
            'Retracted per Retraction Watch, matched by DOI against this specific person\'s own ORCID '
            'record -- no name-matching involved. Restricted to first/last authors only (the ones '
            'conventionally responsible for the work). This is their track record across ALL their claimed '
            'work, not a finding about this paper specifically. '
            'Different from the co-author row(s) below: this is restricted to the first/last '
            'author specifically, matched by strict ORCID (not a fuzzy cluster), and counts against the '
            'full external Retraction Watch database' + misconduct_clause + '. '
            'This row is the PRESENCE half: a flat per-position share for having ANY qualifying retraction '
            'at all, deliberately blind to how many -- see the row below for the volume half. '
            + minmax_note(key)
        )
        parts.append(row(
            f'👤 {position.capitalize()} author retraction rate (external)'
            + (' — misconduct-coded' if is_misconduct else ' — non-misconduct'),
            minmax_contribution(key, 1),
            f'{r[n_key]} out of {esc(r[name_key])}\'s {r[total_key]} ORCID-claimed works were retracted '
            f'({r[rate_key]:.1%}), {r[mc_key]} of them misconduct-coded.',
            help=author_help,
            tag=tag,
        ))
        volume_help = (
            'The VOLUME half of the row above, split out (2026-08-16, user request) so it can be seen and '
            'toggled independently: the presence row above scores "does this person have ANY qualifying '
            'retraction" the same whether the true count is 1 or 100; this row scores the actual count' + (
                f' ({volume_n} misconduct-coded work(s))' if is_misconduct else f' ({volume_n} work(s))'
            ) + ', minmax-scaled against the worst-in-corpus value for this same bucket, so a genuinely '
            'extreme case (checked live: this corpus\'s worst offender) scores the full volume target and '
            'everyone else scores proportionally less -- same anti-domination mechanism as the entity-rate '
            'rows elsewhere on this card. ' + minmax_note(volume_key)
        )
        parts.append(row(
            f'👤 {position.capitalize()} author retraction rate — volume'
            + (' (misconduct-coded)' if is_misconduct else ' (non-misconduct)'),
            minmax_contribution(volume_key, volume_n),
            f'{volume_n} of {esc(r[name_key])}\'s ORCID-claimed works retracted'
            + (' for a misconduct-coded reason' if is_misconduct else '') + '.',
            help=volume_help,
            tag=volume_tag,
        ))

    if r["mid_misconduct_count"] > 0:
        parts.append(row(
            f'Co-authors in misconduct work ({r["mid_misconduct_count"]} co-author(s))',
            minmax_contribution("mid_misconduct_count", r["mid_misconduct_count"]),
            f'<ul>{_coauthor_names(mid_coauthors_misconduct)}</ul>',
            help='Same person ≠ same responsibility. This means a MIDDLE co-author shares a cluster with '
                 'someone who wrote a paper retracted for a misconduct-signal reason — not a formal finding about '
                 'this paper or this person. Matched via a fuzzier probable-person cluster (not strict ORCID), '
                 'and only counts misconduct-REASON retractions found elsewhere IN THIS GRAPH — not an external, '
                 'all-cause, ORCID-verified rate. Different from the 1st/last author rows above: those use '
                 'strict ORCID matching against the full external Retraction Watch database. This row is the '
                 'PRESENCE half: a flat share for having ANY qualifying co-author at all, deliberately blind to '
                 'how many retracted papers are behind them -- see the row below for the volume half. '
                 + minmax_note("mid_misconduct_count"),
            tag="mid-misconduct",
        ))
        parts.append(row(
            'Co-authors in misconduct work — volume',
            minmax_contribution("mid_misconduct_volume", r["mid_misconduct_volume"]),
            f'{r["mid_misconduct_volume"]} other misconduct-coded retracted paper(s) total across the '
            f'{r["mid_misconduct_count"]} co-author(s) above (one prolific co-author can carry most of this '
            'total -- it need not split evenly).',
            help='The VOLUME half of the row above, split out (2026-08-16, user request) so it can be seen '
                 'and toggled independently: the presence row scores "does at least one co-author qualify" the '
                 'same whether the total behind them is 1 retracted paper or 100; this row scores the actual '
                 'total, minmax-scaled against the worst-in-corpus cluster total for this same bucket. '
                 + minmax_note("mid_misconduct_volume"),
            tag="mid-misconduct-volume",
        ))

    if r["mid_any_count"] > 0:
        parts.append(row(
            f'Co-authors in other retracted work, non-misconduct ({r["mid_any_count"]} co-author(s))',
            minmax_contribution("mid_any_count", r["mid_any_count"]),
            f'<ul>{_coauthor_names(mid_coauthors_any)}</ul>',
            help='Same as the misconduct row above, but for a MIDDLE co-author whose other retracted '
                 'work was NOT retracted for a misconduct-signal reason (honest error, duplication, etc.) -- '
                 'scored at half the weight. This row is the PRESENCE half; see the row below for the volume '
                 'half. ' + minmax_note("mid_any_count"),
            tag="mid-any-retr",
        ))
        parts.append(row(
            'Co-authors in other retracted work — volume',
            minmax_contribution("mid_any_volume", r["mid_any_volume"]),
            f'{r["mid_any_volume"]} other non-misconduct retracted paper(s) total across the '
            f'{r["mid_any_count"]} co-author(s) above.',
            help='The VOLUME half of the row above, same idiom as the misconduct-volume row -- minmax-scaled '
                 'against the worst-in-corpus cluster total for this bucket. ' + minmax_note("mid_any_volume"),
            tag="mid-any-retr-volume",
        ))

    # --- non-scored review context ---
    # reference_integrity is deliberately NOT shown here (2026-07-21): the sensor
    # itself is held out of the routine pipeline (see tier_a_scoring.py WEIGHTS
    # comment -- Route 2's ~71%-of-corpus false-positive rate), so any data in
    # r["ref_count"]/ref_flags is a stale snapshot from before that decision,
    # not something a routine re-run keeps current. Surfacing stale, known-noisy
    # counts on the frontend would be misleading regardless of the "not scored"
    # label. The QUERY field is left in place (harmless, unused) rather than
    # touching the Cypher for a pure UI change.
    ctx = []
    if r["pubpeer_total"] > 0:
        cats = pp_cats.get(r["doi"], {})
        cat_str = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])) or "uncategorised"
        ar = ' &middot; <strong>author has responded</strong>' if r["pubpeer_author_response"] else ""
        url = r["pubpeer_url"] or f"https://pubpeer.com/search?q={esc(r['doi'])}"
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">PubPeer</span> '
            f'<a href="{esc(url)}" target="_blank" rel="noopener">{r["pubpeer_total"]} comment(s)</a>{ar}<br>'
            f'<span class="muted">categorized by comment type — {esc(cat_str)}. '
            'Community attention, NOT a guilt signal, and not part of the score.</span></div>'
        )
    if r["gds_prob"] is not None:
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">GDS prior</span> {r["gds_prob"]:.3f} '
            '<span class="muted">— weak/capped/domain-shifted learned prior, labelled only, never scored.</span></div>'
        )
    if r["institution_retr_rate"] > 0:
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">Institution retraction rate (in-graph)</span> '
            f'{esc(r["institution_retr_rate_name"])}: {r["institution_retr_rate"]:.1%} '
            f'<span class="muted">(n={r["institution_retr_rate_n"]} papers in this graph only -- runs inflated '
            'since this graph is itself retraction-seeded; institution_retr_rate_external above uses OpenAlex\'s '
            'full works_count and is the scored signal. Context only, never scored.)</span></div>'
        )
    if r["institution_global_retraction_count"]:
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">Institution — global retraction count</span> '
            f'{esc(r["institution_global_retraction_count_name"])}: '
            f'{r["institution_global_retraction_count"]} <span class="muted">'
            '(exact-match count against the FULL Retraction Watch csv, all subjects. '
            'A raw count, not a rate; coverage is deliberately partial (exact-string match only, no fuzzy '
            'matching at institution scale) — see institution_retraction_rate.py. Context only, never scored.)</span></div>'
        )
    if r["journal_hijack_flag"]:
        orig_url = r["journal_hijack_original_url"]
        orig_link = (
            f'<a href="{esc(orig_url)}" target="_blank" rel="noopener">{esc(orig_url)}</a>' if orig_url else "unknown"
        )
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">⚠ Journal hijacking target</span> '
            f'<span class="muted">{esc(r["journal"])}\'s name/ISSN is documented in the Retraction Watch / '
            'Anna Abalkina Hijacked Journal Checker — a scam site clones it to solicit fraudulent '
            f'"publications". Real site: {orig_link} &middot; clone site: '
            f'<a href="{esc(r["journal_hijack_hijacked_url"])}" target="_blank" rel="noopener">'
            f'{esc(r["journal_hijack_hijacked_url"])}</a>. '
            'This does NOT mean this specific paper came from the clone — verify which site it actually '
            'appeared on. Context only, never scored.</span></div>'
        )
    if r["known_miller_coauthor"]:
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">⚠️ Known miller co-author</span> '
            f'<a href="{esc(r["known_miller_source_url"])}" target="_blank" rel="noopener">'
            f'👤 {esc(r["known_miller_coauthor_name"])}</a><br>'
            '<span class="muted">named in investigative reporting as a paper-mill participant. '
            'Guilt by co-authorship with a documented bad actor is associative, not a finding about '
            'this paper\'s own conduct. Context only, never scored.</span></div>'
        )
    if r["cabanac_chatgpt_flag"]:
        pp_url = r["cabanac_chatgpt_pubpeer_url"]
        pp_link = (
            f'<a href="{esc(pp_url)}" target="_blank" rel="noopener">PubPeer thread</a>' if pp_url
            else '<span class="muted">no PubPeer thread yet</span>'
        )
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">🤖 ChatGPT text tell (Cabanac PPS)</span> '
            f'{pp_link}<br>'
            f'<span class="muted">matched fingerprint(s): "{esc(r["cabanac_chatgpt_fingerprint"])}". '
            'Confirmed by Guillaume Cabanac\'s Problematic Paper Screener, independent of our own '
            'ai_text_tell_flag_count sensor. Context only, calibrates that sensor rather than adding a '
            'second scored signal for the same phenomenon.</span></div>'
        )
    suppl = r["pmc_suppl_status"]
    if suppl and suppl != "unchecked":
        if suppl == "pmc_suppl" and r["pmc_suppl_url"]:
            body = (f'<a href="{esc(r["pmc_suppl_url"])}" target="_blank" rel="noopener">download files (ZIP)</a> '
                    '<span class="muted">— fetchable from Europe PMC (ZIP verified); the forensic sensors (paperconan) can run on this.</span>')
        elif suppl == "suppl_not_downloadable":
            body = ('<span class="muted">exists per the PMC record but is not downloadable from Europe PMC '
                    '(outside its open-access subset) — would need the publisher\'s page.</span>')
        elif suppl == "no_pmc_suppl":
            body = '<span class="muted">none found in PMC for this article.</span>'
        else:  # unknown
            body = '<span class="muted">unknown — article not in PMC, so availability can\'t be determined here (not a confirmed "no").</span>'
        ctx.append(f'<div class="ctx"><span class="ctx-t">📎 Supplementary data</span> {body}</div>')

    if pc:
        f = pc.get("findings") or {}
        # run_paperconan.py writes a literal "TODO -- adjudicate..." placeholder
        # into conclusion until a human edits meta.yaml -- that's an instruction
        # for whoever runs the pipeline, not something a review-page reader can
        # act on, so treat it as unset here rather than showing it verbatim.
        conclusion = pc.get("conclusion") or ""
        if conclusion.startswith("TODO"):
            conclusion = ""
        if pc.get("outcome"):
            # Non-scan outcome (no data / figures-only): show the recorded reason,
            # never a findings count (there was no numeric scan).
            detail = f'{esc(conclusion) or "not scanned"} '
        else:
            counts = f'{f.get("high", 0)} high &middot; {f.get("medium", 0)} medium &middot; {f.get("low", 0)} low'
            draft = ' <strong>(draft — not yet adjudicated)</strong>' if pc.get("needs_adjudication") else ""
            top = f'Top signal: {esc(pc["top_finding"])}. ' if pc.get("top_finding") else ""
            detail = f'{counts}.{draft} {esc(conclusion)}. {top}' if conclusion else f'{counts}.{draft} {top}'
        scored_note = (
            "The needs_human/confirmed verdict above is scored; the raw detector counts here are not. "
            if pc_adj in ("needs_human", "confirmed") else
            "A separate forensic input — signal, not verdict, and NOT part of the score. "
        )
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">paperconan</span> '
            f'<span class="muted">(numeric-forensics on the data tables, v{esc(pc.get("tool_version") or "?")})</span><br>'
            f'<span class="muted">{detail}{scored_note}'
            f'Full run: <code>runs/{esc(pc.get("_dir") or "")}/</code></span></div>'
        )
    img = pc.get("image_screen") if pc else None
    if img:
        n = img.get("n_findings", 0)
        n_img = img.get("n_images", "?")
        if n:
            idetail = (f'<strong>{n}</strong> potential whole-image reuse pair(s) flagged across {n_img} figures — '
                       'inspect the pairs before trusting (aHash can flag legitimately-similar images).')
        else:
            idetail = (f'no whole-image reuse detected across {n_img} figures '
                       '(coarse aHash screen — misses cropped/partial-panel reuse).')
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">🖼 Image-reuse screen</span> '
            f'<span class="muted">{idetail} A screen, not a verdict; NOT part of the score.</span></div>'
        )
    if ctx:
        parts.append('<div class="ctx-wrap"><div class="ctx-head">Review context (not scored)</div>' + "".join(ctx) + "</div>")

    return "".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()

    pp_cats = load_pubpeer_categories()
    pc_runs = load_paperconan_runs()
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(QUERY)]
        compute_corpus_maxes(rows)
        for r in rows:
            pc = pc_runs.get(r["doi"])
            r["paperconan_adjudication"] = pc.get("adjudicated") if pc else None
        ranked = sorted(((score(r), r) for r in rows), key=lambda t: t[0], reverse=True)[: args.top]
        cards = []
        tag_counts: dict[str, int] = {}
        for rank, (sc, r) in enumerate(ranked, 1):
            mid_coauthors_misconduct = (
                s.run(COAUTHOR_QUERY, doi=r["doi"], reasons=MISCONDUCT_REASONS, misconduct=True).data()
                if r["mid_misconduct_count"] else []
            )
            mid_coauthors_any = (
                s.run(COAUTHOR_QUERY, doi=r["doi"], reasons=MISCONDUCT_REASONS, misconduct=False).data()
                if r["mid_any_count"] else []
            )
            badges, tags = [], []
            if r["ori_flag"]:
                badges.append('<span class="badge badge-ori">ORI Finding</span>'); tags.append("ori")
            if r["eoc_flag"]:
                badges.append('<span class="badge badge-eoc">Expression of Concern</span>'); tags.append("eoc")
            # Each badge is tagged with BOTH its presence tag and its volume
            # tag (2026-08-16) -- the badge text itself stays one combined
            # line (still the clearest at-a-glance summary), but both scoring
            # chips need this card counted in tag_counts, and both need this
            # card's data-contribs entry populated, or the toolbar's chip-n
            # count and the chip's live re-rank would silently under-report.
            if r["mid_misconduct_count"] > 0:
                badges.append(f'<span class="badge badge-flag">Co-author of misconduct work '
                              f'({_count_volume_suffix(r["mid_misconduct_count"], r["mid_misconduct_volume"], "retracted papers")})</span>')
                tags += ["mid-misconduct", "mid-misconduct-volume"]
            if r["mid_any_count"] > 0:
                badges.append(f'<span class="badge badge-flag">Co-author of other retracted work '
                              f'({_count_volume_suffix(r["mid_any_count"], r["mid_any_volume"], "retracted papers")})</span>')
                tags += ["mid-any-retr", "mid-any-retr-volume"]
            if r["fl_misconduct_count"] > 0:
                badges.append(f'<span class="badge badge-flag">👤 1st/last author, misconduct retraction '
                              f'({_count_volume_suffix(r["fl_misconduct_count"], r["fl_misconduct_volume"], "retracted works")})</span>')
                tags += ["fl-misconduct", "fl-misconduct-volume"]
            if r["fl_any_count"] > 0:
                badges.append(f'<span class="badge badge-flag">👤 1st/last author, other retraction '
                              f'({_count_volume_suffix(r["fl_any_count"], r["fl_any_volume"], "retracted works")})</span>')
                tags += ["fl-any-retr", "fl-any-retr-volume"]
            if r["ret_count"] > 0:
                ret_flags = json.loads(r["ret_flags"] or "[]")
                n_self = sum(1 for f in ret_flags if f.get("self_citation"))
                badges.append(f'<span class="badge badge-flag">Cites retracted work ({r["ret_count"]})</span>'); tags.append("cites-retracted")
                if n_self:
                    badges.append(f'<span class="badge badge-selfcite" title="Cites the authors\' OWN retracted work">👤 Cites own retracted ({n_self})</span>'); tags.append("self-cite")
            if r["ext_ret_count"] > 0:
                badges.append(f'<span class="badge badge-flag" title="Retracted per OpenAlex, outside our Retraction-Watch corpus">Cites retracted (ext.) ({r["ext_ret_count"]})</span>'); tags.append("cites-retracted-ext")
            if r["correction_count"] > 0:
                badges.append(f'<span class="badge badge-flag" title="Corrections are often benign -- kept low-weight">Crossref correction ({r["correction_count"]})</span>'); tags.append("correction")
            if r["journal_count"] > 0:
                badges.append('<span class="badge badge-flag">Journal integrity flag</span>'); tags.append("journal")
            if r["journal_retr_rate_external"] >= HIGH_EXTERNAL_RATE_THRESHOLD:
                badges.append(f'<span class="badge badge-flag" title="{r["journal_retr_rate_external"]:.1%} real-world retraction rate">📔 High retract rate journal</span>'); tags.append("journal-high-retr")
            if r["publisher_retr_rate"] >= HIGH_EXTERNAL_RATE_THRESHOLD:
                badges.append(f'<span class="badge badge-flag" title="{r["publisher_retr_rate"]:.1%} real-world retraction rate">📇 High retract rate publisher</span>'); tags.append("publisher-high-retr")
            if r["institution_retr_rate_external"] > 0:
                # Presence-only, not a "high" cutoff -- see HIGH_COUNTRY_RATE_THRESHOLD's
                # comment: only 4% of candidates have ANY external institution-level
                # retraction record at all, so that alone is already a rare, notable
                # signal (same reasoning as author-retr's n>0 condition above).
                badges.append(f'<span class="badge badge-flag" title="{r["institution_retr_rate_external"]:.4%} real-world retraction rate">🏛️ High retract rate institution</span>'); tags.append("institution-high-retr")
            if r["country_retr_rate"] >= HIGH_COUNTRY_RATE_THRESHOLD:
                badges.append(f'<span class="badge badge-flag" title="{r["country_retr_rate"]:.3%} real-world retraction rate">🌍 High retract rate country ({r["country_retr_rate_name"]})</span>'); tags.append("country-high-retr")
            if r["journal_hijack_flag"]:
                badges.append('<span class="badge badge-flag" title="This journal name/ISSN is a documented hijacking target -- see review context below">⚠ Journal hijacking target</span>'); tags.append("journal-hijack")
            if r["known_miller_coauthor"]:
                badges.append(f'<span class="badge badge-flag" title="Co-authored with {esc(r["known_miller_coauthor_name"])}, named in investigative reporting as a paper-mill participant">⚠️ Known miller co-author</span>'); tags.append("known-miller")
            if r["cabanac_chatgpt_flag"]:
                badges.append('<span class="badge badge-flag" title="Confirmed by Guillaume Cabanac\'s Problematic Paper Screener -- see review context below">🤖 ChatGPT text tell (Cabanac)</span>'); tags.append("cabanac-chatgpt")
            if r["ai_count"] > 0:
                badges.append(f'<span class="badge badge-flag">AI-text tells ({r["ai_count"]})</span>'); tags.append("ai")
            if r["tortured_count"] > 0:
                badges.append(f'<span class="badge badge-flag" title="Exact match against Cabanac\'s Problematic Paper Screener \'Favourite Tortured Phrases\' list">Tortured phrase ({r["tortured_count"]})</span>'); tags.append("tortured")
            if r["pval_count"] > 0:
                badges.append(f'<span class="badge badge-flag">p-value pattern ({r["pval_count"]})</span>'); tags.append("pval")
            if r["erratum_flag"]:
                badges.append('<span class="badge badge-flag">Erratum on record</span>'); tags.append("erratum")
            if r["pubpeer_total"] > 0:
                badges.append(f'<span class="badge badge-pp">{r["pubpeer_total"]} PubPeer</span>'); tags.append("pubpeer")
                allegation_cats = [c for c in pp_cats.get(r["doi"], {}) if c in PUBPEER_ALLEGATION_CATEGORIES]
                if allegation_cats:
                    badges.append('<span class="badge badge-pp" title="At least one PubPeer comment makes a substantive claim (image/reference/COI/investigation), not just a methodology question or the author\'s own reply">⚠ PubPeer allegation</span>'); tags.append("pubpeer-allegation")
            if r["has_pmc_suppl"] and r["pmc_suppl_url"]:
                badges.append(
                    f'<a class="badge badge-suppl" href="{esc(r["pmc_suppl_url"])}" target="_blank" '
                    f'rel="noopener" onclick="event.stopPropagation()" '
                    f'title="Download supplementary files (ZIP) from Europe PMC">📎 Suppl data</a>'
                ); tags.append("suppl")
            if r["doi"] in pc_runs:
                badges.append(paperconan_badge(pc_runs[r["doi"]])); tags.append("paperconan")
                if pc_runs[r["doi"]].get("image_screen"):
                    badges.append(image_badge(pc_runs[r["doi"]]["image_screen"])); tags.append("image")
            if r["country_retr_rate"] > 0:
                # Not a chip (an open-ended set of ISO2 codes would clutter the
                # fixed TAG_LABELS chip row) -- filterable instead via the
                # dedicated country <select> built from country_counts below,
                # using the exact same data-tags/activeTags mechanism as chips.
                tags.append(f"country-{r['country_retr_rate_name'].lower()}")
            for t in tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1
            contribs = chip_contributions(r)
            cards.append(f'''
    <article class="card" data-score="{sc:.2f}" data-contribs='{json.dumps(contribs)}' data-rank="{rank}" data-doi="{esc(r['doi'])}" data-tags="{' '.join(tags)}">
      <div class="card-h" onclick="this.parentElement.classList.toggle('open')">
        <div class="rank">#{rank}</div>
        <div class="score" title="{flag_gauge(sc)} of 5 review-priority flags · weighted Tier-A score {sc:.1f}">
          <div class="gauge" aria-label="{flag_gauge(sc)} of 5 priority flags">{'🚩' * flag_gauge(sc)}</div>
          <div class="score-num">{sc:.1f}</div>
        </div>
        <div class="meta">
          <div class="title">{esc_title(r['title'])}</div>
          <div class="sub"><a href="https://doi.org/{esc(r['doi'])}" target="_blank" rel="noopener" onclick="event.stopPropagation()">{esc(r['doi'])}</a>
            &middot; {esc(r['journal'])}{' &middot; ' + esc(r['published_date'][:10]) if r['published_date'] and r['published_date'] != 'null' else ''}</div>
          <div class="badges">{''.join(badges)}</div>
        </div>
        <div class="chev">▾</div>
      </div>
      <div class="card-b">
        {render_evidence(r, mid_coauthors_misconduct, mid_coauthors_any, pp_cats, pc_runs)}
      </div>
    </article>''')
    driver.close()

    n_eoc = sum(1 for _, r in ranked if r["eoc_flag"])
    n_pp = sum(1 for _, r in ranked if r["pubpeer_total"] > 0)
    generated = date.today().isoformat()

    score_chips = "".join(
        f'<button class="chip active" data-tag="{k}" data-mode="score" onclick="toggleScoreChip(this)" '
        f'title="Included in score -- click to zero this signal and re-rank live (papers stay visible)">'
        f'{esc(label)}<span class="chip-n">{tag_counts[k]}</span></button>'
        for k, label in TAG_LABELS if k in SCORED_TAGS and tag_counts.get(k)
    )
    filter_chips = "".join(
        f'<button class="chip" data-tag="{k}" data-mode="filter" onclick="toggleTag(this)" '
        f'title="Click to filter to only papers carrying this tag">'
        f'{esc(label)}<span class="chip-n">{tag_counts[k]}</span></button>'
        for k, label in TAG_LABELS if k not in SCORED_TAGS and tag_counts.get(k)
    )
    country_tags = sorted(
        (k for k in tag_counts if k.startswith("country-")),
        key=lambda k: k[len("country-"):],
    )
    country_options = "".join(
        f'<option value="{k}">{esc(k[len("country-"):].upper())} ({tag_counts[k]})</option>'
        for k in country_tags
    )
    page = PAGE_TEMPLATE.format(
        n=len(ranked), n_eoc=n_eoc, n_pp=n_pp, generated=generated,
        cards="".join(cards), score_chips=score_chips, filter_chips=filter_chips,
        country_options=country_options,
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(page)
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}  ({len(ranked)} papers, {n_eoc} with EoC, {n_pp} with PubPeer comments)")
    print(f"  open it locally: file://{OUTPUT}")


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Papers flagged for human review</title>
<style>
  :root {{ --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#e3e3e6; --card:#fafafa; --card-detail:#fff;
           --accent:#7a5cff; --eoc:#c2410c; --pp:#0369a1; --warn:#92400e; --warnbg:#fef3c7; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#16161a; --fg:#e8e8ea; --muted:#9a9aa2; --line:#2c2c33; --card:#1d1d22; --card-detail:#28282f;
             --accent:#9d86ff; --eoc:#fb923c; --pp:#38bdf8; --warn:#fcd34d; --warnbg:#3a2f10; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  header {{ padding:28px 20px 16px; max-width:960px; margin:0 auto; }}
  h1 {{ font-size:1.55rem; margin:0 0 4px; }}
  .gen {{ color:var(--muted); font-size:.85rem; }}
  .banner {{ background:var(--warnbg); color:var(--warn); border:1px solid color-mix(in srgb,var(--warn) 40%,transparent);
             border-radius:10px; padding:12px 15px; margin:14px 0; font-size:.9rem; }}
  .banner strong {{ color:var(--warn); }}
  .stats {{ display:flex; gap:20px; flex-wrap:wrap; color:var(--muted); font-size:.9rem; margin:10px 0 4px; }}
  .toolbar {{ max-width:960px; margin:0 auto; padding:0 20px; display:flex; gap:10px; align-items:center; }}
  .search {{ flex:1; position:relative; display:flex; align-items:center; }}
  .search-ico {{ position:absolute; left:13px; font-size:.9rem; opacity:.6; pointer-events:none; }}
  .toolbar input {{ flex:1; padding:9px 12px 9px 38px; border:1px solid var(--line); border-radius:22px; background:var(--bg); color:var(--fg); }}
  .toolbar input:focus {{ outline:none; border-color:var(--accent); }}
  .toolbar select {{ padding:9px 12px; border:1px solid var(--line); border-radius:22px; background:var(--bg); color:var(--fg); font-size:.9rem; flex-shrink:0; }}
  .toolbar select:focus {{ outline:none; border-color:var(--accent); }}
  main {{ max-width:960px; margin:0 auto; padding:10px 20px 60px; }}
  .card {{ border:1px solid var(--line); border-radius:11px; margin:10px 0; background:var(--card); }}
  .card-h {{ display:grid; grid-template-columns:auto auto 1fr auto; gap:14px; align-items:center; padding:13px 16px; cursor:pointer; }}
  .rank {{ color:var(--muted); font-variant-numeric:tabular-nums; font-size:.85rem; }}
  .score {{ text-align:center; min-width:5.2ch; }}
  .gauge {{ font-size:.82rem; line-height:1.1; letter-spacing:1px; white-space:nowrap; }}
  .score-num {{ font-weight:700; font-size:.8rem; color:var(--accent); font-variant-numeric:tabular-nums; margin-top:2px; }}
  .title {{ font-weight:600; }}
  .sub {{ color:var(--muted); font-size:.85rem; margin-top:2px; }}
  .sub a {{ color:var(--pp); text-decoration:none; }}
  .badges {{ margin-top:6px; display:flex; gap:6px; flex-wrap:wrap; }}
  .badge {{ font-size:.72rem; padding:2px 8px; border-radius:20px; font-weight:600; }}
  .badge-ori {{ background:color-mix(in srgb,#dc2626 18%,transparent); color:#dc2626; }}
  .badge-eoc {{ background:color-mix(in srgb,var(--eoc) 18%,transparent); color:var(--eoc); }}
  .badge-pp {{ background:color-mix(in srgb,var(--pp) 18%,transparent); color:var(--pp); }}
  .badge-flag {{ background:color-mix(in srgb,var(--fg) 12%,transparent); color:var(--fg); }}
  .badge-selfcite {{ background:color-mix(in srgb,#dc2626 22%,transparent); color:#dc2626; }}
  .badge-pc {{ background:color-mix(in srgb,#0d9488 20%,transparent); color:#0d9488; }}
  .badge-pc-hi {{ background:color-mix(in srgb,#dc2626 20%,transparent); color:#dc2626; }}
  .badge-pc-ok {{ background:color-mix(in srgb,var(--muted) 22%,transparent); color:var(--muted); }}
  .badge-suppl {{ background:color-mix(in srgb,var(--pp) 16%,transparent); color:var(--pp); text-decoration:none; }}
  .badge-suppl:hover {{ background:color-mix(in srgb,var(--pp) 28%,transparent); }}
  .ctx code {{ font-size:.82em; background:color-mix(in srgb,var(--fg) 8%,transparent); padding:1px 5px; border-radius:4px; }}
  .chev {{ color:var(--muted); transition:transform .15s; }}
  .card.open .chev {{ transform:rotate(180deg); }}
  .card-b {{ display:none; padding:4px 16px 16px; border-top:1px solid var(--line); background:var(--card-detail);
    border-radius:0 0 10px 10px; }}
  .card.open .card-b {{ display:block; }}
  .ev {{ margin:12px 0; }}
  .ev-h {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; }}
  .ev-tg {{ display:inline-flex; align-items:center; gap:5px; }}
  .ev-t {{ font-weight:600; font-size:.92rem; }}
  .ev-c {{ color:var(--accent); font-weight:700; font-variant-numeric:tabular-nums; font-size:.85rem; }}
  .ev.ev-zeroed .ev-t, .ev.ev-zeroed .ev-c {{ opacity:.45; text-decoration:line-through; }}
  .ev-b {{ font-size:.88rem; color:var(--fg); margin-top:4px; }}
  .ev-b ul {{ margin:5px 0; padding-left:0; list-style:none; }}
  .ev-b li {{ margin:3px 0; }}
  .ev-b a {{ color:var(--pp); }}
  .muted {{ color:var(--muted); }}
  .caveat {{ background:var(--warnbg); color:var(--warn); border-radius:7px; padding:7px 10px; font-size:.85rem; margin:4px 0 7px; }}
  .help-wrap {{ position:relative; display:inline-flex; align-items:center; }}
  .help-btn {{ display:inline-flex; align-items:center; justify-content:center; flex-shrink:0;
    width:16px; height:16px; border-radius:50%; border:none; background:var(--accent); color:var(--bg);
    font-weight:700; font-size:.72rem; line-height:1; cursor:pointer; padding:0; }}
  .help-btn:hover {{ filter:brightness(1.15); }}
  .help-pop {{ position:absolute; top:22px; left:0; z-index:20; width:260px; max-width:min(260px, 60vw);
    background:var(--card); color:var(--fg); border:1px solid var(--line); border-radius:8px;
    padding:9px 11px; font-size:.82rem; font-weight:400; line-height:1.45;
    box-shadow:0 6px 20px rgba(0,0,0,.18);
    opacity:0; visibility:hidden; transform:translateY(-4px); pointer-events:none;
    transition:opacity .12s ease, transform .12s ease; }}
  .help-wrap:hover .help-pop, .help-wrap:focus-within .help-pop {{
    opacity:1; visibility:visible; transform:translateY(0); pointer-events:auto; }}
  .self-cite-caveat {{ background:color-mix(in srgb,#dc2626 12%,transparent); color:#dc2626; }}
  .self-cite {{ color:#dc2626; font-weight:600; }}
  .ctx-wrap {{ margin-top:16px; border-top:1px dashed var(--line); padding-top:10px; }}
  .ctx-head {{ font-size:.78rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin-bottom:6px; }}
  .ctx {{ font-size:.87rem; margin:6px 0; }}
  .ctx-t {{ font-weight:600; }}
  .ctx a {{ color:var(--pp); }}
  .chip-group {{ max-width:960px; margin:10px auto 0; padding:0 20px; }}
  .chip-group-label {{ font-size:.74rem; color:var(--muted); text-transform:uppercase; letter-spacing:.03em; margin-bottom:4px; }}
  .chip-group-action {{ background:none; border:none; color:var(--pp); cursor:pointer; font-size:.74rem;
    text-transform:none; letter-spacing:normal; padding:0; margin-left:4px; }}
  .chip-group-action:hover {{ text-decoration:underline; }}
  .chips {{ display:flex; gap:7px; flex-wrap:wrap; align-items:center; }}
  .chip {{ font-size:.82rem; padding:4px 11px; border:1px solid var(--line); border-radius:20px; background:var(--card); color:var(--fg); cursor:pointer; display:inline-flex; gap:6px; align-items:center; }}
  .chip:hover {{ border-color:var(--accent); }}
  .chip.active {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
  .chip-n {{ font-size:.72rem; opacity:.7; font-variant-numeric:tabular-nums; }}
  .chip.active .chip-n {{ opacity:.85; }}
  .filterbar {{ max-width:960px; margin:6px auto 0; padding:0 20px; font-size:.82rem; color:var(--muted); display:flex; gap:12px; align-items:center; }}
  #clear {{ background:none; border:none; color:var(--pp); cursor:pointer; font-size:.82rem; padding:0; display:none; }}
  .pager {{ max-width:960px; margin:16px auto; padding:0 20px; display:flex; gap:10px; align-items:center; justify-content:center; font-size:.86rem; color:var(--muted); }}
  .pager button {{ padding:6px 14px; border:1px solid var(--line); border-radius:7px; background:var(--card); color:var(--fg); cursor:pointer; font-size:.85rem; }}
  .pager button:disabled {{ opacity:.4; cursor:default; }}
  .pager button:not(:disabled):hover {{ border-color:var(--accent); }}
  .pager-info {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .pager-info input.page-jump {{ width:44px; padding:3px 5px; margin:0 2px; border:1px solid var(--line);
    border-radius:6px; background:var(--card); color:var(--fg); font-size:.85rem; text-align:center;
    font-variant-numeric:tabular-nums; }}
  .pager-info input.page-jump:focus {{ border-color:var(--accent); outline:none; }}
</style>
</head>
<body>
<header>
  <h1>Papers flagged for human review</h1>
  <div class="gen">Generated {generated} &middot; private local file — not shared</div>
  <div class="banner">
    <strong>These are hypotheses for review, not accusations.</strong>
    <span class="help-wrap">
      <button class="help-btn" type="button" aria-label="More context">!</button>
      <span class="help-pop">A retraction or a flag is not proof of fraud, and nothing here asserts misconduct by any
        named person — author role and responsibility vary paper to paper. 🚩 shows review priority from the weighted
        score (🚩🚩🚩🚩🚩 ≥12 &middot; 🚩🚩🚩🚩 ≥9 &middot; 🚩🚩🚩 ≥6 &middot; 🚩🚩 ≥3 &middot; 🚩 &gt;0); the exact score
        sits under each gauge and drives the sort. Every point maps to a named, sourced flag you can inspect
        below.</span>
    </span>
  </div>
  <div class="stats">
    <span><strong>{n}</strong> papers ranked</span>
    <span><strong>{n_eoc}</strong> with a formal Expression of Concern</span>
    <span><strong>{n_pp}</strong> with PubPeer comments</span>
  </div>
</header>
<div class="toolbar">
  <div class="search">
    <span class="search-ico" aria-hidden="true">🔎</span>
    <input id="filter" type="search" placeholder="Filter by title, DOI, or journal…" oninput="doFilter(this.value)">
  </div>
  <select id="countrySelect" title="🏳️‍🌈 Filter by country retraction rate" onchange="setCountry(this.value)">
    <option value="">🏳️‍🌈 All countries</option>
    {country_options}
  </select>
</div>
<div class="chip-group">
  <div class="chip-group-label">Scoring signals — highlighted = included; click to zero and re-rank live (nothing hidden)
    <button class="chip-group-action" onclick="clearAllScoreChips()">Clear all</button>
  </div>
  <div class="chips">{score_chips}</div>
</div>
<div class="chip-group">
  <div class="chip-group-label">Filters — click to show only papers carrying every selected tag (AND)</div>
  <div class="chips">{filter_chips}</div>
</div>
<div class="filterbar">
  <span id="shown"></span>
  <button id="clear" onclick="clearTags()">reset chips ✕</button>
</div>
<div class="pager" id="pagerTop">
  <button id="prevTop" onclick="gotoPage(page-1)">← prev</button>
  <span class="pager-info">
    <span id="pageInfoNormalTop">page
      <input type="number" class="page-jump" id="jumpTop" min="1" value="1"
        onkeydown="if(event.key==='Enter'){{doJump(this.value);this.blur();}}"
        onchange="doJump(this.value)">
      of <span id="totalTop"></span> (<span id="rangeTop"></span>)</span>
    <span id="pageInfoEmptyTop" style="display:none;">no papers match</span>
  </span>
  <button id="nextTop" onclick="gotoPage(page+1)">next →</button>
</div>
<main id="list">
{cards}
</main>
<div class="pager" id="pagerBottom">
  <button id="prevBottom" onclick="gotoPage(page-1)">← prev</button>
  <span class="pager-info">
    <span id="pageInfoNormalBottom">page
      <input type="number" class="page-jump" id="jumpBottom" min="1" value="1"
        onkeydown="if(event.key==='Enter'){{doJump(this.value);this.blur();}}"
        onchange="doJump(this.value)">
      of <span id="totalBottom"></span> (<span id="rangeBottom"></span>)</span>
    <span id="pageInfoEmptyBottom" style="display:none;">no papers match</span>
  </span>
  <button id="nextBottom" onclick="gotoPage(page+1)">next →</button>
</div>
<script>
  // Combined filtering: free-text query AND the set of active tag chips.
  // Pagination applies to the FILTERED set, not the raw list -- "page 1 of
  // filtered results," matching how the toolbar's "shown X of Y" already works.
  const PAGE_SIZE = 50;
  let query = '';
  let page = 1;
  let filtered = [];
  const activeTags = new Set();
  const cards = Array.from(document.querySelectorAll('.card'));

  function applyFilters() {{
    filtered = cards.filter(c => {{
      const textOk = c.textContent.toLowerCase().includes(query);
      const tags = (c.dataset.tags || '').split(' ');
      const tagOk = [...activeTags].every(t => tags.includes(t));
      return textOk && tagOk;
    }});
    page = 1;
    renderPage();
  }}

  function renderPage() {{
    const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
    page = Math.min(Math.max(1, page), totalPages);
    const start = (page - 1) * PAGE_SIZE;
    const end = start + PAGE_SIZE;
    const onPage = new Set(filtered.slice(start, end));
    cards.forEach(c => {{ c.style.display = onPage.has(c) ? '' : 'none'; }});

    const s = document.getElementById('shown');
    s.textContent = (query || activeTags.size) ? `showing ${{filtered.length}} of ${{cards.length}}` : '';

    ['Top', 'Bottom'].forEach(suf => {{
      document.getElementById('pageInfoNormal' + suf).style.display = filtered.length ? '' : 'none';
      document.getElementById('pageInfoEmpty' + suf).style.display = filtered.length ? 'none' : '';
      if (filtered.length) {{
        const jump = document.getElementById('jump' + suf);
        jump.max = totalPages;
        jump.value = page;
        document.getElementById('total' + suf).textContent = totalPages;
        document.getElementById('range' + suf).textContent =
          `${{start + 1}}–${{Math.min(end, filtered.length)}} of ${{filtered.length}}`;
      }}
    }});
    ['prevTop', 'prevBottom'].forEach(id => document.getElementById(id).disabled = page <= 1);
    ['nextTop', 'nextBottom'].forEach(id => document.getElementById(id).disabled = page >= totalPages);
  }}

  function gotoPage(n) {{
    page = n;
    renderPage();
    document.getElementById('list').scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  }}

  function doJump(v) {{
    const n = parseInt(v, 10);
    if (!isNaN(n)) gotoPage(n);
  }}

  function doFilter(v) {{ query = v.toLowerCase(); applyFilters(); }}
  function toggleTag(btn) {{
    const t = btn.dataset.tag;
    if (activeTags.has(t)) {{ activeTags.delete(t); btn.classList.remove('active'); }}
    else {{ activeTags.add(t); btn.classList.add('active'); }}
    document.getElementById('clear').style.display = (activeTags.size || inactiveScoreTags.size) ? '' : 'none';
    applyFilters();
  }}
  function setCountry(tag) {{
    // Single-select (a paper has one max-country), unlike the chips above --
    // drop any previously-selected country- tag before adding the new one.
    [...activeTags].filter(t => t.startsWith('country-')).forEach(t => activeTags.delete(t));
    if (tag) activeTags.add(tag);
    document.getElementById('clear').style.display = (activeTags.size || inactiveScoreTags.size) ? '' : 'none';
    applyFilters();
  }}
  function clearTags() {{
    activeTags.clear();
    document.querySelectorAll('.chip[data-mode="filter"].active').forEach(b => b.classList.remove('active'));
    document.getElementById('countrySelect').value = '';
    inactiveScoreTags.clear();
    document.querySelectorAll('.chip[data-mode="score"]').forEach(b => b.classList.add('active'));
    document.getElementById('clear').style.display = 'none';
    rerank();
    applyFilters();
  }}

  // Score chips (SCORED_TAGS in build_review_page.py): all 5 entity-rate
  // minmax signals now have their own chip (institution-high-retr,
  // country-high-retr added 2026-07-22, retiring the old bundled 5-signal
  // switch). Toggling one off zeros that signal's per-card contribution
  // (from data-contribs, a JSON {{tag: value}} blob baked in at build time)
  // and re-ranks live -- unlike the filter chips above, nothing is hidden.
  const listContainer = document.getElementById('list');
  const inactiveScoreTags = new Set();

  function activeScore(c) {{
    const contribs = JSON.parse(c.dataset.contribs || '{{}}');
    let sc = parseFloat(c.dataset.score);
    inactiveScoreTags.forEach(t => {{ if (contribs[t] !== undefined) sc -= contribs[t]; }});
    return sc;
  }}
  function gaugeFlags(sc) {{
    if (sc >= 12) return 5;
    if (sc >= 9) return 4;
    if (sc >= 6) return 3;
    if (sc >= 3) return 2;
    if (sc > 0.0001) return 1;
    return 0;
  }}
  function updateCardDisplays() {{
    cards.forEach((c, i) => {{
      const sc = activeScore(c);
      const rank = i + 1;
      const flags = gaugeFlags(sc);
      c.dataset.rank = rank;
      c.querySelector('.rank').textContent = '#' + rank;
      c.querySelector('.gauge').textContent = '🚩'.repeat(flags);
      c.querySelector('.gauge').setAttribute('aria-label', flags + ' of 5 priority flags');
      c.querySelector('.score-num').textContent = sc.toFixed(1);
      c.querySelector('.score').title = flags + ' of 5 review-priority flags · weighted Tier-A score ' + sc.toFixed(1);
    }});
  }}
  // Evidence rows carry data-tag (set in build_review_page.py's row(), only
  // for rows tied to a SCORED_TAGS chip) + data-full (the real "+X.XX",
  // preserved so it can be restored exactly). Strikes a row through and
  // shows +0.00 while its chip is off -- keeps the always-visible per-signal
  // breakdown honest with whatever the card-level score currently reflects,
  // instead of silently showing stale numbers for an excluded signal.
  function updateEvidenceRows() {{
    document.querySelectorAll('.ev[data-tag]').forEach(el => {{
      const zeroed = inactiveScoreTags.has(el.dataset.tag);
      el.classList.toggle('ev-zeroed', zeroed);
      const c = el.querySelector('.ev-c');
      c.textContent = zeroed ? '+0.00' : c.dataset.full;
    }});
  }}
  function rerank() {{
    cards.sort((a, b) => activeScore(b) - activeScore(a));
    listContainer.append(...cards);
    updateCardDisplays();
    updateEvidenceRows();
  }}
  function toggleScoreChip(btn) {{
    const t = btn.dataset.tag;
    if (inactiveScoreTags.has(t)) {{ inactiveScoreTags.delete(t); btn.classList.add('active'); }}
    else {{ inactiveScoreTags.add(t); btn.classList.remove('active'); }}
    document.getElementById('clear').style.display = (activeTags.size || inactiveScoreTags.size) ? '' : 'none';
    rerank();
    applyFilters();
  }}
  // Distinct from "reset chips" (which restores the full default view --
  // every scoring chip back on, all filters cleared): this only zeroes every
  // scoring signal at once, leaving filters untouched, for the opposite
  // question -- "what's left if I strip out every scored signal?"
  function clearAllScoreChips() {{
    document.querySelectorAll('.chip[data-mode="score"]').forEach(btn => {{
      inactiveScoreTags.add(btn.dataset.tag);
      btn.classList.remove('active');
    }});
    document.getElementById('clear').style.display = (activeTags.size || inactiveScoreTags.size) ? '' : 'none';
    rerank();
    applyFilters();
  }}
  applyFilters();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
