#!/usr/bin/env python3
"""
refresh_ori_findings.py — cross-check our graph against ORI's actual
published "Findings of Research Misconduct" notices, via the Federal
Register's public, keyless API.

Why the Federal Register and not ori.hhs.gov/content/case_summary directly:
that page ONLY lists respondents with a CURRENTLY ACTIVE administrative
action (it explicitly excludes anyone whose sanction period has expired) --
useless for cross-checking Retraction Watch's "Investigation by ORI" reason,
which routinely points at cases from a decade+ ago. ORI findings are also
published permanently in the Federal Register under the standard title
"Findings of Research Misconduct" (confirmed live 2026-07-19: 209 notices,
1999-2026). robots.txt has no restriction on this API or on individual
document pages (checked live; only /documents/search, /documents/current
and a few UI paths are disallowed, no named-bot block).

Two citation styles, two matching strategies (confirmed live 2026-09-09
against document 2020-10253, the Shin-Hee Kim case, and 2022-27316):
  - NEWER notices cite affected papers with a literal "doi: 10.xxxx/..."
    string in the body text -- DOI_RE below matches these directly, exact
    and unambiguous.
  - OLDER notices (at least Kim's, and plausibly many pre-2021 ones) cite
    papers the traditional way instead: "Title. Journal Year;Vol(Iss):Page"
    with no DOI anywhere in the document. DOI_RE finds nothing for these,
    so this script used to silently skip them entirely -- confirmed to have
    missed Kim's own seven-paper finding, this project's own flagship
    example case.
  Fallback for the second style: extract the title text preceding each such
  citation (CITATION_RE) and fuzzy-match it against titles ALREADY in our
  own graph (not a fresh Crossref lookup -- tested that first and it
  resolves ~70% of the time to the PAPER'S OWN RETRACTION NOTICE's separate
  DOI instead of the original article's DOI, e.g. "Mutations in the fusion
  protein..." resolves via Crossref bibliographic search to
  10.1371/journal.pone.0244076, the retraction notice, not
  10.1371/journal.pone.0050598, the original fabricated-data paper -- both
  score >0.9 title similarity against the query, so similarity alone can't
  tell them apart). Matching against our own already-imported paper titles
  sidesteps that ambiguity entirely, since Retraction Watch's
  OriginalPaperDOI field (what populates Paper.doi here) is already the
  original article, never the retraction notice. Validated 7/7 exact DOI
  matches on Kim's own citations, ratio 0.99-1.00, before this was wired in.
  Accepted only above TITLE_MATCH_THRESHOLD; anything below is reported and
  skipped, never guessed -- this is the highest-weighted signal in the
  scoring system (tier_a_scoring.py, weight 4.0), a false positive here is
  far costlier than a missed one.

What this does:
  1. Pull every "Findings of Research Misconduct" notice (Federal Register
     API, one call, 209 results as of writing).
  2. For each, fetch the full-text HTML and extract every cited paper, by
     DOI where the notice states one directly, by fuzzy title match against
     our own graph otherwise, plus the respondent's name (from the standard
     SUMMARY sentence).
  3. Cross-check those DOIs against EVERY paper in our graph (not just
     not-yet-retracted candidates) -- an ORI finding is an authoritative fact
     independent of a paper's current retraction status; retraction and ORI
     adjudication are separate processes that can lag each other by years.

Fields written (facts, not verdicts -- plan.md §0):
  ori_finding_doc_url    : the Federal Register notice URL
  ori_finding_date       : notice publication date
  ori_respondent_name    : name as it appears in the notice
  ori_document_number    : Federal Register document number (citable)
  ori_checked_date       : provenance
  ori_finding_low_confidence : True iff resolved via the title-fallback path
                                rather than a literal DOI citation (mirrors
                                the *_low_confidence convention used by
                                author_retraction_rate_external.py etc.)
  ori_finding_match_ratio    : difflib title-similarity ratio (1.0 for a
                                direct DOI citation, since nothing was
                                fuzzy-matched)

This is the single strongest fact-based signal available to this project --
a federal agency's adjudicated finding, naming this exact paper, not a
proxy or a community opinion. Wired into tier_a_scoring.py at weight 4.0,
intentionally the highest in the system (see that file's WEIGHTS docstring).
misconduct_label (author_retraction_rate_external.py) is a SEPARATE,
independently-sourced fact straight from Retraction Watch's own Reason
column -- the two can and do legitimately disagree (confirmed live: RW
codes Kim's jvi.01570-14 retraction as "Error in Image", not misconduct,
while ORI's own notice calls the same paper fabricated) and neither script
should overwrite the other's field to force agreement.

Idempotent: safe to re-run; overwrites with fresh Federal Register data.

Usage:
  python graph_processing/refresh_ori_findings.py             # check + apply
  python graph_processing/refresh_ori_findings.py --dry-run    # check only
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

FR_API = "https://www.federalregister.gov/api/v1"
SEARCH_TERM = '"Findings of Research Misconduct"'
REQUEST_DELAY = 0.4  # polite self-imposed pace; no stated rate limit

DOI_RE = re.compile(r"\bdoi:\s*(10\.\d{4,}(?:\.\d+)*/[^\s,;)\]\"'<]+)", re.I)
RESPONDENT_RE = re.compile(
    r"[Ff]indings? of research misconduct (?:have|has) been made against ([^,]+(?:,\s*(?:M\.?D\.?|Ph\.?D\.?))?)", re.I
)

# Fallback citation-title extraction for notices with no "doi:" string at
# all. Tuned and validated against document 2020-10253 (see module
# docstring) -- captures "<Title>. <Journal> <Year>;<Vol>(<Iss>):<Page>".
# A miss here just means the notice is skipped (same as before this fallback
# existed), never a guess.
CITATION_RE = re.compile(
    r"([A-Z][^.]{20,300}?)\.\s+"
    r"[A-Za-z][A-Za-z.&\s]{1,40}?\s+"
    r"((?:19|20)\d{2});(\d{1,4})(?:\((?:Pt\s*)?[^)]{1,15}\))?:([A-Za-z]?\d[\w\-]{0,15})"
)

# How similar a fallback-matched graph title must be (difflib ratio, 0-1) to
# the notice's cited title before we'll accept it. Chosen well above the
# 0.90-1.00 range observed on the 7 validated Kim matches, with real margin
# below that for genuine hits, while (per the Crossref test in the module
# docstring) a paper and its OWN separate retraction notice can also score
# >0.9 against each other on raw text similarity -- matching only against
# our own graph's titles (never a fresh external search) is what actually
# prevents that specific confusion; this threshold is a second, independent
# guard against noisy/partial extraction, not the primary defense.
TITLE_MATCH_THRESHOLD = 0.90

RETRACTION_PREFIX_RE = re.compile(
    r"^\s*(retracted|retraction( notice)?|retraction for [^:]+)\s*[:–-]\s*", re.I
)


def canon_doi(doi: str) -> str:
    d = doi.strip().lower().rstrip(".,;)")
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


def norm_title(t: str) -> str:
    return RETRACTION_PREFIX_RE.sub("", t or "").strip().lower()


def fetch_all_notices() -> list[dict]:
    r = requests.get(
        f"{FR_API}/documents.json",
        params={
            "conditions[term]": SEARCH_TERM,
            "per_page": 1000,
            "order": "newest",
            "fields[]": ["title", "document_number", "publication_date", "html_url", "body_html_url"],
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("results", [])


def fetch_notice_text(body_html_url: str) -> str:
    r = requests.get(body_html_url, timeout=30)
    r.raise_for_status()
    text = re.sub(r"<[^>]+>", " ", r.text)
    text = text.replace("&ldquo;", '"').replace("&rdquo;", '"')
    return text


def extract_citation_titles(text: str) -> list[str]:
    """Candidate title strings preceding a 'Year;Vol(Iss):Page' citation.
    May include leading noise before the true title starts (e.g. a list
    intro ending '...NIH: Actual Title'); norm_title() only strips a
    retraction-style prefix, not this, so best_title_match's fuzzy ratio is
    what tolerates the rest -- validated at ratio>=0.99 even with such noise
    present (see module docstring)."""
    return [m.group(1).strip() for m in CITATION_RE.finditer(text)]


def best_title_match(query_title: str, graph_titles: list[tuple[str, str]]) -> tuple[str, float] | None:
    """graph_titles: list of (doi, title). Returns (doi, ratio) for the
    single best match, or None if graph_titles is empty. Caller applies
    TITLE_MATCH_THRESHOLD."""
    if not graph_titles:
        return None
    qn = norm_title(query_title)
    best_doi, best_ratio = None, -1.0
    for doi, title in graph_titles:
        ratio = difflib.SequenceMatcher(None, norm_title(title), qn).ratio()
        if ratio > best_ratio:
            best_doi, best_ratio = doi, ratio
    return best_doi, best_ratio


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        graph_dois_raw = {r["doi"] for r in s.run("MATCH (p:Paper) WHERE p.doi IS NOT NULL RETURN p.doi AS doi")}
        graph_titles = [
            (r["doi"], r["title"])
            for r in s.run("MATCH (p:Paper) WHERE p.doi IS NOT NULL AND p.title IS NOT NULL "
                            "RETURN p.doi AS doi, p.title AS title")
        ]
    print(f"  {len(graph_titles)} graph papers with titles available for fallback matching")

    notices = fetch_all_notices()
    print(f"  {len(notices)} 'Findings of Research Misconduct' notices found (Federal Register, 1999-present)")

    doi_to_finding: dict[str, dict] = {}
    n_fallback_accepted = n_fallback_rejected = 0
    for i, n in enumerate(notices, 1):
        time.sleep(REQUEST_DELAY)
        try:
            text = fetch_notice_text(n["body_html_url"])
        except requests.RequestException as exc:
            print(f"  WARNING: could not fetch {n['document_number']}: {exc}", file=sys.stderr)
            continue

        text_collapsed = re.sub(r"\s+", " ", text)
        m = RESPONDENT_RE.search(text_collapsed)
        respondent = m.group(1).strip() if m else None

        base = {
            "doc_url": n["html_url"],
            "date": n["publication_date"],
            "respondent": respondent,
            "document_number": n["document_number"],
        }

        dois = {canon_doi(d) for d in DOI_RE.findall(text_collapsed)}
        for doi in dois:
            # A DOI can appear in multiple notices (corrections, follow-ups) --
            # keep the earliest (original finding), never overwrite silently.
            if doi not in doi_to_finding:
                doi_to_finding[doi] = {**base, "low_confidence": False, "match_ratio": 1.0}

        if not dois:
            for cand_title in extract_citation_titles(text_collapsed):
                match = best_title_match(cand_title, graph_titles)
                if match is None:
                    continue
                cand_doi, ratio = match
                if ratio < TITLE_MATCH_THRESHOLD:
                    n_fallback_rejected += 1
                    continue
                doi = canon_doi(cand_doi)
                if doi in doi_to_finding:
                    continue
                doi_to_finding[doi] = {**base, "low_confidence": True, "match_ratio": round(ratio, 3)}
                n_fallback_accepted += 1
                print(f"    [fallback match ratio={ratio:.3f}] {doi}  <-  {cand_title[:80]!r}  ({n['document_number']})")

        if i % 50 == 0:
            print(f"  [{i}/{len(notices)}]", file=sys.stderr)

    print(f"\n  {len(doi_to_finding)} distinct DOIs cited across all ORI findings notices "
          f"({n_fallback_accepted} via title fallback, {n_fallback_rejected} candidate titles rejected below threshold)")

    # doi_to_finding keys are already canon_doi()'d (lowercased). Graph DOIs are
    # stored in whatever case they arrived in (Elsevier-style uppercase suffixes
    # like "10.1016/S0895-4356(00)00298-4" are routine). Compare case-insensitively
    # but key `matches` by the graph's OWN casing, so the write MATCH below (an
    # exact-equality lookup) actually finds the node (BUG.md R3-6).
    graph_doi_by_lower = {canon_doi(d): d for d in graph_dois_raw}
    matches = {
        graph_doi_by_lower[doi]: f
        for doi, f in doi_to_finding.items()
        if doi in graph_doi_by_lower
    }
    print(f"  {len(matches)} of those DOIs are papers already in our graph")

    if matches:
        with driver.session(database=conn["database"]) as s:
            for doi, f in matches.items():
                row = s.run(
                    "MATCH (p:Paper {doi:$doi}) RETURN p.is_retracted AS is_retracted, p.title AS title",
                    doi=doi,
                ).single()
                status = "NOT-YET-RETRACTED" if row and not row["is_retracted"] else "already retracted"
                conf = "LOW-CONFIDENCE" if f["low_confidence"] else "direct"
                print(f"    [{status}] [{conf}, ratio={f['match_ratio']:.3f}] {doi}  respondent={f['respondent']}  ({f['doc_url']})")
                if row:
                    print(f"        {row['title'][:90]}")

    if args.dry_run:
        print("\n  --dry-run: no changes written.")
        driver.close()
        return

    if matches:
        today = str(date.today())
        with driver.session(database=conn["database"]) as s:
            for doi, f in matches.items():
                s.run(
                    """
                    MATCH (p:Paper {doi:$doi})
                    SET p.ori_finding_doc_url = $doc_url,
                        p.ori_finding_date = $date,
                        p.ori_respondent_name = $respondent,
                        p.ori_document_number = $document_number,
                        p.ori_checked_date = $today,
                        p.ori_finding_low_confidence = $low_confidence,
                        p.ori_finding_match_ratio = $match_ratio
                    """,
                    doi=doi, today=today,
                    doc_url=f["doc_url"], date=f["date"], respondent=f["respondent"],
                    document_number=f["document_number"],
                    low_confidence=f["low_confidence"], match_ratio=f["match_ratio"],
                )
        print(f"\n  wrote ORI finding fields for {len(matches)} paper(s)")
    driver.close()


if __name__ == "__main__":
    main()
