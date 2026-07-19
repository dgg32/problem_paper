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

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

OUTPUT = REPO_ROOT / "review" / "index.html"
PUBPEER_CATEGORIES = REPO_ROOT / "data" / "flags" / "pubpeer_comment_categories.json"

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


def render_evidence(r: dict, coauthors: list[dict], pp_cats: dict) -> str:
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
    if ctx:
        parts.append('<div class="ctx-wrap"><div class="ctx-head">Review context (not scored)</div>' + "".join(ctx) + "</div>")

    return "".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()

    pp_cats = load_pubpeer_categories()
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(QUERY)]
        ranked = sorted(((score(r), r) for r in rows), key=lambda t: t[0], reverse=True)[: args.top]
        cards = []
        for rank, (sc, r) in enumerate(ranked, 1):
            coauthors = s.run(COAUTHOR_QUERY, doi=r["doi"], reasons=MISCONDUCT_REASONS).data() if r["coauthor_misconduct"] else []
            badges = []
            if r["ori_flag"]:
                badges.append('<span class="badge badge-ori">ORI Finding</span>')
            if r["eoc_flag"]:
                badges.append('<span class="badge badge-eoc">Expression of Concern</span>')
            if r["coauthor_misconduct"] > 0:
                badges.append(f'<span class="badge badge-flag">Co-author of misconduct work ({r["coauthor_misconduct"]})</span>')
            if r["ret_count"] > 0:
                badges.append(f'<span class="badge badge-flag">Cites retracted work ({r["ret_count"]})</span>')
            if r["journal_count"] > 0:
                badges.append('<span class="badge badge-flag">Journal integrity flag</span>')
            if r["ref_count"] > 0:
                badges.append(f'<span class="badge badge-flag">Reference integrity ({r["ref_count"]})</span>')
            if r["ai_count"] > 0:
                badges.append(f'<span class="badge badge-flag">AI-text tells ({r["ai_count"]})</span>')
            if r["pval_count"] > 0:
                badges.append(f'<span class="badge badge-flag">p-value pattern ({r["pval_count"]})</span>')
            if r["erratum_flag"]:
                badges.append('<span class="badge badge-flag">Erratum on record</span>')
            if r["pubpeer_total"] > 0:
                badges.append(f'<span class="badge badge-pp">{r["pubpeer_total"]} PubPeer</span>')
            cards.append(f'''
    <article class="card" data-score="{sc:.2f}" data-rank="{rank}" data-doi="{esc(r['doi'])}">
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
        {render_evidence(r, coauthors, pp_cats)}
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

    page = PAGE_TEMPLATE.format(
        n=len(ranked), n_eoc=n_eoc, n_pp=n_pp, generated=generated,
        cards="".join(cards),
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
  .toolbar input {{ flex:1; padding:8px 11px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg); }}
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
  <input id="filter" type="search" placeholder="Filter by title, DOI, or journal…" oninput="doFilter(this.value)">
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
  function doFilter(q) {{
    q = q.toLowerCase();
    document.querySelectorAll('.card').forEach(c => {{
      c.style.display = c.textContent.toLowerCase().includes(q) ? '' : 'none';
    }});
  }}
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
