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
  - Expression of Concern (pubmed_eoc_*, weight 2.5) with date + notice DOI
  - PubPeer comment count + category breakdown + author-response note
    (from data/flags/pubpeer_comment_categories.json), shown as review
    context but NOT scored (comment count = attention, not guilt).

Copy discipline (plan.md §5, §0): header reads "Papers flagged for human
review"; a standing banner states the facts-not-verdicts framing; the
coauthor-misconduct block carries the "same person ≠ same responsibility"
caveat verbatim in spirit. No cell asserts fraud.

Reviewer-decision buttons persist to the browser's localStorage only -- this
is a read-first preview; durable, shared decisions need the FastAPI backend
(the next Phase-5 increment). The buttons are clearly labelled as local-only
so a reviewer isn't misled into thinking a click is saved server-side.

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

import yaml
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
    ("coauthor", "Co-author of misconduct"),
    ("cites-retracted", "Cites retracted"),
    ("journal", "Journal integrity"),
    ("reference", "Reference integrity"),
    ("ai", "AI-text tells"),
    ("pval", "p-value pattern"),
    ("erratum", "Erratum"),
    ("pubpeer", "PubPeer"),
    ("suppl", "Suppl data"),
    ("paperconan", "paperconan"),
    ("image", "Image screen"),
]

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

# Must match tier_a_scoring.py WEIGHTS exactly.
WEIGHTS = {
    "retracted_citation_flag_count": 3.0,
    "reference_integrity_flag_count": 1.5,
    "journal_integrity_flag_count": 1.0,
    "ai_text_tell_flag_count": 2.0,
    "p_value_hacking_flag_count": 0.5,
    "pubmed_eoc_flag": 2.5,
    "pubmed_erratum_flag": 0.3,
    "ori_finding_flag": 4.0,
    "coauthor_other_misconduct": 1.5,
    "journal_retr_rate": 2.0,
}

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
       coalesce(p.reference_integrity_flag_count, 0) AS ref_count,
       coalesce(p.journal_integrity_flag_count, 0) AS journal_count,
       coalesce(p.ai_text_tell_flag_count, 0) AS ai_count,
       coalesce(p.p_value_hacking_flag_count, 0) AS pval_count,
       CASE WHEN p.pubmed_eoc_status = "expression_of_concern" THEN 1 ELSE 0 END AS eoc_flag,
       CASE WHEN p.pubmed_eoc_status = "erratum_only" THEN 1 ELSE 0 END AS erratum_flag,
       p.pubmed_eoc_date AS eoc_date, p.pubmed_eoc_source_doi AS eoc_source_doi,
       CASE WHEN p.ori_finding_doc_url IS NOT NULL THEN 1 ELSE 0 END AS ori_flag,
       p.ori_finding_doc_url AS ori_doc_url, p.ori_respondent_name AS ori_respondent,
       p.ori_finding_date AS ori_date,
       coalesce(p.coauthor_other_misconduct, 0) AS coauthor_misconduct,
       coalesce(p.journal_retr_rate, 0.0) AS journal_retr_rate,
       p.gds_misconduct_prob AS gds_prob,
       coalesce(p.pubpeer_comments_total, 0) AS pubpeer_total,
       p.pubpeer_check_url AS pubpeer_url,
       coalesce(p.pubpeer_has_author_response, false) AS pubpeer_author_response,
       coalesce(p.pmc_suppl_status, "unchecked") AS pmc_suppl_status,
       coalesce(p.has_pmc_suppl, false) AS has_pmc_suppl,
       p.pmc_suppl_url AS pmc_suppl_url,
       p.retracted_citation_flags AS ret_flags,
       p.reference_integrity_flags AS ref_flags,
       p.journal_integrity_flags AS journal_flags,
       p.ai_text_tell_flags AS ai_flags,
       p.p_value_hacking_flags AS pval_flags
"""

COAUTHOR_QUERY = """
MATCH (p:Paper {doi: $doi})<-[:WROTE]-(a:AuthorInstance)
WITH p, collect(DISTINCT a.cluster_id) AS clusters
UNWIND clusters AS cid
MATCH (mate:AuthorInstance {cluster_id: cid})-[:WROTE]->(mp:Paper)-[:RETRACTED_FOR]->(r:Reason)
WHERE mp <> p AND r.code IN $reasons
WITH mate.name AS coauthor_name, collect(DISTINCT mp.doi)[0..2] AS example_dois,
     collect(DISTINCT r.code) AS reasons
RETURN coauthor_name, example_dois, reasons ORDER BY coauthor_name LIMIT 6
"""


def score(r: dict) -> float:
    return (
        r["ret_count"] * WEIGHTS["retracted_citation_flag_count"]
        + r["ref_count"] * WEIGHTS["reference_integrity_flag_count"]
        + r["journal_count"] * WEIGHTS["journal_integrity_flag_count"]
        + r["ai_count"] * WEIGHTS["ai_text_tell_flag_count"]
        + r["pval_count"] * WEIGHTS["p_value_hacking_flag_count"]
        + r["eoc_flag"] * WEIGHTS["pubmed_eoc_flag"]
        + r["erratum_flag"] * WEIGHTS["pubmed_erratum_flag"]
        + r["ori_flag"] * WEIGHTS["ori_finding_flag"]
        + r["coauthor_misconduct"] * WEIGHTS["coauthor_other_misconduct"]
        + r["journal_retr_rate"] * WEIGHTS["journal_retr_rate"]
    )


_TAG_RE = re.compile(r"<[^>]+>")


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


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


def load_paperconan_runs() -> dict:
    """Returns {doi: meta} for each runs/<doi__>/meta.yaml present.

    paperconan is numeric-forensics on the data tables (plan.md Phase 3) — a
    separate, signal-not-verdict input. Its result is shown as review context,
    never folded into the weighted score (same discipline as PubPeer / GDS)."""
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


# Once a run is adjudicated (meta.yaml gains an `adjudicated:` verdict and drops
# needs_adjudication), the badge reflects the HUMAN verdict, not the raw count —
# so a benign "26 high" reads calmly, and a real concern reads loud.
ADJ_BADGE = {
    "false_positive": ("badge-pc-ok", "paperconan: reviewed — false positive"),
    "benign":         ("badge-pc-ok", "paperconan: reviewed — benign"),
    "inconclusive":   ("badge-pc",    "paperconan: reviewed — inconclusive"),
    "needs_data":     ("badge-pc",    "paperconan: reviewed — needs data"),
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


def render_evidence(r: dict, coauthors: list[dict], pp_cats: dict, pc_runs: dict) -> str:
    """Build the expandable per-paper evidence HTML."""
    parts: list[str] = []

    def row(label, contrib, body):
        return (
            f'<div class="ev"><div class="ev-h"><span class="ev-t">{esc(label)}</span>'
            f'<span class="ev-c">+{contrib:.2f}</span></div>'
            f'<div class="ev-b">{body}</div></div>'
        )

    if r["ori_flag"]:
        parts.append(row(
            "ORI finding of research misconduct (federal)",
            WEIGHTS["ori_finding_flag"],
            f'Respondent: 👤 {esc(r["ori_respondent"]) or "unnamed"} &middot; date: {esc(r["ori_date"]) or "unknown"} &middot; '
            f'<a href="{esc(r["ori_doc_url"])}" target="_blank" rel="noopener">Federal Register notice</a>. '
            'An adjudicated federal finding naming this paper by DOI — the strongest fact-based signal here.',
        ))

    if r["eoc_flag"]:
        notice = r["eoc_source_doi"]
        link = f' &middot; notice: <a href="https://doi.org/{esc(notice)}" target="_blank" rel="noopener">{esc(notice)}</a>' if notice else ""
        parts.append(row(
            "Expression of Concern (formal journal notice)",
            WEIGHTS["pubmed_eoc_flag"],
            f'Date: {esc(r["eoc_date"]) or "unknown"}{link}. '
            'A formal, dated editorial fact — stronger than community commentary, weaker than a retraction.',
        ))

    if r["coauthor_misconduct"] > 0:
        names = "".join(
            f'<li>👤 <strong>{esc(c["coauthor_name"])}</strong> — '
            + ", ".join(f'<a href="https://doi.org/{esc(d)}" target="_blank" rel="noopener">{esc(d)}</a>' for d in c["example_dois"])
            + f' <span class="muted">({esc(", ".join(c["reasons"]))})</span></li>'
            for c in coauthors
        )
        parts.append(row(
            f'Co-author of misconduct work ({r["coauthor_misconduct"]} co-author(s))',
            r["coauthor_misconduct"] * WEIGHTS["coauthor_other_misconduct"],
            '<p class="caveat">⚠ Same person ≠ same responsibility (§0). This means a co-author shares a cluster with '
            'someone who wrote a paper retracted for a misconduct-signal reason — <em>not</em> a formal finding about '
            'this paper or this person. Author role varies paper to paper.</p>'
            f'<ul>{names}</ul>',
        ))

    if r["ret_count"] > 0:
        flags = json.loads(r["ret_flags"] or "[]")
        items = "".join(
            f'<li>📃 <a href="https://doi.org/{esc(f.get("cited_retracted_paper_doi"))}" target="_blank" rel="noopener">'
            f'{esc((f.get("cited_retracted_paper_title") or "")[:80])}</a> '
            f'<span class="muted">({esc(", ".join(f.get("retraction_reasons", [])) or "reason unknown")}'
            + (", cited AFTER retraction" if f.get("citing_after_retraction") else "") + ')</span></li>'
            for f in flags[:4]
        )
        parts.append(row(
            f'Cites retracted work ({r["ret_count"]})',
            r["ret_count"] * WEIGHTS["retracted_citation_flag_count"],
            f'<ul>{items}</ul>',
        ))

    if r["journal_retr_rate"] > 0:
        parts.append(row(
            "Journal retraction rate",
            r["journal_retr_rate"] * WEIGHTS["journal_retr_rate"],
            f'{esc(r["journal"])} has a measured {r["journal_retr_rate"]:.1%} retraction rate in this graph.',
        ))

    if r["journal_count"] > 0:
        flags = json.loads(r["journal_flags"] or "[]")
        parts.append(row(
            "Journal integrity flag",
            r["journal_count"] * WEIGHTS["journal_integrity_flag_count"],
            esc(flags[0].get("reason") if flags else "flagged"),
        ))

    if r["ref_count"] > 0:
        flags = json.loads(r["ref_flags"] or "[]")
        items = "".join(f'<li>{esc((f.get("reference_title") or "")[:100])} — <span class="muted">{esc(f.get("reason"))}</span></li>' for f in flags[:4])
        parts.append(row(
            f'Reference integrity ({r["ref_count"]})',
            r["ref_count"] * WEIGHTS["reference_integrity_flag_count"],
            f'<ul>{items}</ul>',
        ))

    if r["ai_count"] > 0:
        flags = json.loads(r["ai_flags"] or "[]")
        pats = ", ".join(esc(f.get("pattern")) for f in flags[:4])
        parts.append(row(f'AI-text tells ({r["ai_count"]})', r["ai_count"] * WEIGHTS["ai_text_tell_flag_count"], pats))

    if r["pval_count"] > 0:
        flags = json.loads(r["pval_flags"] or "[]")
        note = flags[0].get("reason") if flags else "p-value clustering"
        parts.append(row(f'p-value pattern ({r["pval_count"]})', r["pval_count"] * WEIGHTS["p_value_hacking_flag_count"], esc(note)))

    if r["erratum_flag"]:
        parts.append(row("Erratum on record", WEIGHTS["pubmed_erratum_flag"], "A published erratum exists (weak signal — most errata are benign corrections)."))

    # --- non-scored review context ---
    ctx = []
    if r["pubpeer_total"] > 0:
        cats = pp_cats.get(r["doi"], {})
        cat_str = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])) or "uncategorised"
        ar = ' &middot; <strong>author has responded</strong>' if r["pubpeer_author_response"] else ""
        url = r["pubpeer_url"] or f"https://pubpeer.com/search?q={esc(r['doi'])}"
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">PubPeer</span> '
            f'<a href="{esc(url)}" target="_blank" rel="noopener">{r["pubpeer_total"]} comment(s)</a>{ar}<br>'
            f'<span class="muted">concern comments by nature — {esc(cat_str)}. '
            'Community attention, NOT a guilt signal, and not part of the score.</span></div>'
        )
    if r["gds_prob"] is not None:
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">GDS prior</span> {r["gds_prob"]:.3f} '
            '<span class="muted">— weak/capped/domain-shifted learned prior, labelled only, never scored.</span></div>'
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

    pc = pc_runs.get(r["doi"])
    if pc:
        f = pc.get("findings") or {}
        if pc.get("outcome"):
            # Non-scan outcome (no data / figures-only): show the recorded reason,
            # never a findings count (there was no numeric scan).
            detail = f'{esc(pc.get("conclusion") or "not scanned")} '
        else:
            counts = f'{f.get("high", 0)} high &middot; {f.get("medium", 0)} medium &middot; {f.get("low", 0)} low'
            draft = ' <strong>(draft — not yet adjudicated)</strong>' if pc.get("needs_adjudication") else ""
            top = f'Top signal: {esc(pc["top_finding"])}. ' if pc.get("top_finding") else ""
            detail = f'{counts}.{draft} {esc(pc.get("conclusion") or "")}. {top}'
        ctx.append(
            f'<div class="ctx"><span class="ctx-t">paperconan</span> '
            f'<span class="muted">(numeric-forensics on the data tables, v{esc(pc.get("tool_version") or "?")})</span><br>'
            f'<span class="muted">{detail}'
            'A separate forensic input — signal, not verdict, and NOT part of the score. '
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
        ranked = sorted(((score(r), r) for r in rows), key=lambda t: t[0], reverse=True)[: args.top]
        cards = []
        tag_counts: dict[str, int] = {}
        for rank, (sc, r) in enumerate(ranked, 1):
            coauthors = s.run(COAUTHOR_QUERY, doi=r["doi"], reasons=MISCONDUCT_REASONS).data() if r["coauthor_misconduct"] else []
            badges, tags = [], []
            if r["ori_flag"]:
                badges.append('<span class="badge badge-ori">ORI Finding</span>'); tags.append("ori")
            if r["eoc_flag"]:
                badges.append('<span class="badge badge-eoc">Expression of Concern</span>'); tags.append("eoc")
            if r["coauthor_misconduct"] > 0:
                badges.append(f'<span class="badge badge-flag">Co-author of misconduct work ({r["coauthor_misconduct"]})</span>'); tags.append("coauthor")
            if r["ret_count"] > 0:
                badges.append(f'<span class="badge badge-flag">Cites retracted work ({r["ret_count"]})</span>'); tags.append("cites-retracted")
            if r["journal_count"] > 0:
                badges.append('<span class="badge badge-flag">Journal integrity flag</span>'); tags.append("journal")
            if r["ref_count"] > 0:
                badges.append(f'<span class="badge badge-flag">Reference integrity ({r["ref_count"]})</span>'); tags.append("reference")
            if r["ai_count"] > 0:
                badges.append(f'<span class="badge badge-flag">AI-text tells ({r["ai_count"]})</span>'); tags.append("ai")
            if r["pval_count"] > 0:
                badges.append(f'<span class="badge badge-flag">p-value pattern ({r["pval_count"]})</span>'); tags.append("pval")
            if r["erratum_flag"]:
                badges.append('<span class="badge badge-flag">Erratum on record</span>'); tags.append("erratum")
            if r["pubpeer_total"] > 0:
                badges.append(f'<span class="badge badge-pp">{r["pubpeer_total"]} PubPeer</span>'); tags.append("pubpeer")
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
            for t in tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1
            cards.append(f'''
    <article class="card" data-score="{sc:.2f}" data-rank="{rank}" data-doi="{esc(r['doi'])}" data-tags="{' '.join(tags)}">
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
        {render_evidence(r, coauthors, pp_cats, pc_runs)}
        <div class="decision" data-doi="{esc(r['doi'])}">
          <span class="dlabel">Reviewer decision <span class="muted">(saved to this browser only — preview)</span>:</span>
          <button data-v="legit">Legit</button>
          <button data-v="look">Needs deeper look</button>
          <button data-v="problem">Confirmed problematic</button>
        </div>
      </div>
    </article>''')
    driver.close()

    n_eoc = sum(1 for _, r in ranked if r["eoc_flag"])
    n_pp = sum(1 for _, r in ranked if r["pubpeer_total"] > 0)
    generated = date.today().isoformat()

    chips = "".join(
        f'<button class="chip" data-tag="{k}" onclick="toggleTag(this)">{esc(label)}'
        f'<span class="chip-n">{tag_counts[k]}</span></button>'
        for k, label in TAG_LABELS if tag_counts.get(k)
    )
    page = PAGE_TEMPLATE.format(
        n=len(ranked), n_eoc=n_eoc, n_pp=n_pp, generated=generated,
        cards="".join(cards), chips=chips,
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
  :root {{ --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#e3e3e6; --card:#fafafa;
           --accent:#7a5cff; --eoc:#c2410c; --pp:#0369a1; --warn:#92400e; --warnbg:#fef3c7; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#16161a; --fg:#e8e8ea; --muted:#9a9aa2; --line:#2c2c33; --card:#1d1d22;
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
  main {{ max-width:960px; margin:0 auto; padding:10px 20px 60px; }}
  .card {{ border:1px solid var(--line); border-radius:11px; margin:10px 0; background:var(--card); overflow:hidden; }}
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
  .badge-pc {{ background:color-mix(in srgb,#0d9488 20%,transparent); color:#0d9488; }}
  .badge-pc-hi {{ background:color-mix(in srgb,#dc2626 20%,transparent); color:#dc2626; }}
  .badge-pc-ok {{ background:color-mix(in srgb,var(--muted) 22%,transparent); color:var(--muted); }}
  .badge-suppl {{ background:color-mix(in srgb,var(--pp) 16%,transparent); color:var(--pp); text-decoration:none; }}
  .badge-suppl:hover {{ background:color-mix(in srgb,var(--pp) 28%,transparent); }}
  .ctx code {{ font-size:.82em; background:color-mix(in srgb,var(--fg) 8%,transparent); padding:1px 5px; border-radius:4px; }}
  .chev {{ color:var(--muted); transition:transform .15s; }}
  .card.open .chev {{ transform:rotate(180deg); }}
  .card-b {{ display:none; padding:4px 16px 16px; border-top:1px solid var(--line); }}
  .card.open .card-b {{ display:block; }}
  .ev {{ margin:12px 0; }}
  .ev-h {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; }}
  .ev-t {{ font-weight:600; font-size:.92rem; }}
  .ev-c {{ color:var(--accent); font-weight:700; font-variant-numeric:tabular-nums; font-size:.85rem; }}
  .ev-b {{ font-size:.88rem; color:var(--fg); margin-top:4px; }}
  .ev-b ul {{ margin:5px 0; padding-left:0; list-style:none; }}
  .ev-b li {{ margin:3px 0; }}
  .ev-b a {{ color:var(--pp); }}
  .muted {{ color:var(--muted); }}
  .caveat {{ background:var(--warnbg); color:var(--warn); border-radius:7px; padding:7px 10px; font-size:.85rem; margin:4px 0 7px; }}
  .ctx-wrap {{ margin-top:16px; border-top:1px dashed var(--line); padding-top:10px; }}
  .ctx-head {{ font-size:.78rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin-bottom:6px; }}
  .ctx {{ font-size:.87rem; margin:6px 0; }}
  .ctx-t {{ font-weight:600; }}
  .ctx a {{ color:var(--pp); }}
  .decision {{ margin-top:16px; padding-top:12px; border-top:1px solid var(--line); display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
  .dlabel {{ font-size:.85rem; margin-right:4px; }}
  .decision button {{ padding:5px 12px; border:1px solid var(--line); border-radius:7px; background:var(--bg); color:var(--fg); cursor:pointer; font-size:.85rem; }}
  .decision button:hover {{ border-color:var(--accent); }}
  .decision button.sel {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
  .chips {{ max-width:960px; margin:8px auto 0; padding:0 20px; display:flex; gap:7px; flex-wrap:wrap; align-items:center; }}
  .chip {{ font-size:.82rem; padding:4px 11px; border:1px solid var(--line); border-radius:20px; background:var(--card); color:var(--fg); cursor:pointer; display:inline-flex; gap:6px; align-items:center; }}
  .chip:hover {{ border-color:var(--accent); }}
  .chip.active {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
  .chip-n {{ font-size:.72rem; opacity:.7; font-variant-numeric:tabular-nums; }}
  .chip.active .chip-n {{ opacity:.85; }}
  .filterbar {{ max-width:960px; margin:6px auto 0; padding:0 20px; font-size:.82rem; color:var(--muted); display:flex; gap:12px; align-items:center; }}
  #clear {{ background:none; border:none; color:var(--pp); cursor:pointer; font-size:.82rem; padding:0; display:none; }}
</style>
</head>
<body>
<header>
  <h1>Papers flagged for human review</h1>
  <div class="gen">Generated {generated} &middot; private local file — not shared</div>
  <div class="banner">
    <strong>These are hypotheses for review, not accusations.</strong> A retraction or a flag is not proof of fraud, and
    nothing here asserts misconduct by any named person — author role and responsibility vary paper to paper.
    🚩 shows review priority from the weighted score (🚩🚩🚩🚩🚩 ≥12 &middot; 🚩🚩🚩🚩 ≥9 &middot; 🚩🚩🚩 ≥6 &middot;
    🚩🚩 ≥3 &middot; 🚩 &gt;0); the exact score sits under each gauge and drives the sort. Every point maps to a named,
    sourced flag you can inspect below.
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
</div>
<div class="chips">{chips}</div>
<div class="filterbar">
  <span id="shown"></span>
  <button id="clear" onclick="clearTags()">clear filters ✕</button>
  <span class="muted">tags combine with AND — a paper must carry every selected tag</span>
</div>
<main id="list">
{cards}
</main>
<script>
  // Reviewer decisions: localStorage only (this browser). Clearly a preview until the backend lands.
  const KEY = 'review-decisions-v1';
  const store = JSON.parse(localStorage.getItem(KEY) || '{{}}');
  function paint() {{
    document.querySelectorAll('.decision').forEach(d => {{
      const v = store[d.dataset.doi];
      d.querySelectorAll('button').forEach(b => b.classList.toggle('sel', b.dataset.v === v));
    }});
  }}
  document.querySelectorAll('.decision button').forEach(b => b.addEventListener('click', e => {{
    e.stopPropagation();
    const doi = b.closest('.decision').dataset.doi;
    store[doi] = (store[doi] === b.dataset.v) ? undefined : b.dataset.v;
    if (store[doi] === undefined) delete store[doi];
    localStorage.setItem(KEY, JSON.stringify(store));
    paint();
  }}));
  paint();

  // Combined filtering: free-text query AND the set of active tag chips.
  let query = '';
  const activeTags = new Set();
  const cards = Array.from(document.querySelectorAll('.card'));
  function applyFilters() {{
    let shown = 0;
    cards.forEach(c => {{
      const textOk = c.textContent.toLowerCase().includes(query);
      const tags = (c.dataset.tags || '').split(' ');
      const tagOk = [...activeTags].every(t => tags.includes(t));
      const vis = textOk && tagOk;
      c.style.display = vis ? '' : 'none';
      if (vis) shown++;
    }});
    const s = document.getElementById('shown');
    s.textContent = (query || activeTags.size) ? `showing ${{shown}} of ${{cards.length}}` : '';
  }}
  function doFilter(v) {{ query = v.toLowerCase(); applyFilters(); }}
  function toggleTag(btn) {{
    const t = btn.dataset.tag;
    if (activeTags.has(t)) {{ activeTags.delete(t); btn.classList.remove('active'); }}
    else {{ activeTags.add(t); btn.classList.add('active'); }}
    document.getElementById('clear').style.display = activeTags.size ? '' : 'none';
    applyFilters();
  }}
  function clearTags() {{
    activeTags.clear();
    document.querySelectorAll('.chip.active').forEach(b => b.classList.remove('active'));
    document.getElementById('clear').style.display = 'none';
    applyFilters();
  }}
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
