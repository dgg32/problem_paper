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
1999-2026), and -- unlike ori.hhs.gov -- they cite the actual affected papers
by DOI in the notice body. robots.txt has no restriction on this API or on
individual document pages (checked live; only /documents/search,
/documents/current and a few UI paths are disallowed, no named-bot block).

What this does:
  1. Pull every "Findings of Research Misconduct" notice (Federal Register
     API, one call, 209 results as of writing).
  2. For each, fetch the full-text HTML and extract every cited DOI + the
     respondent's name (from the standard SUMMARY sentence).
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

This is the single strongest fact-based signal available to this project --
a federal agency's adjudicated finding, naming this exact paper by DOI, not a
proxy or a community opinion. Wired into tier_a_scoring.py at weight 4.0,
intentionally the highest in the system (see that file's WEIGHTS docstring).

Idempotent: safe to re-run; overwrites with fresh Federal Register data.

Usage:
  python graph_processing/refresh_ori_findings.py             # check + apply
  python graph_processing/refresh_ori_findings.py --dry-run    # check only
"""
from __future__ import annotations

import argparse
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


def canon_doi(doi: str) -> str:
    d = doi.strip().lower().rstrip(".,;)")
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


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
    return re.sub(r"<[^>]+>", " ", r.text)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    notices = fetch_all_notices()
    print(f"  {len(notices)} 'Findings of Research Misconduct' notices found (Federal Register, 1999-present)")

    doi_to_finding: dict[str, dict] = {}
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

        dois = {canon_doi(d) for d in DOI_RE.findall(text)}
        for doi in dois:
            # A DOI can appear in multiple notices (corrections, follow-ups) --
            # keep the earliest (original finding), never overwrite silently.
            if doi not in doi_to_finding:
                doi_to_finding[doi] = {
                    "doc_url": n["html_url"],
                    "date": n["publication_date"],
                    "respondent": respondent,
                    "document_number": n["document_number"],
                }
        if i % 50 == 0:
            print(f"  [{i}/{len(notices)}]", file=sys.stderr)

    print(f"\n  {len(doi_to_finding)} distinct DOIs cited across all ORI findings notices")

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        graph_dois_raw = {r["doi"] for r in s.run("MATCH (p:Paper) WHERE p.doi IS NOT NULL RETURN p.doi AS doi")}
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
                print(f"    [{status}] {doi}  respondent={f['respondent']}  ({f['doc_url']})")
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
                        p.ori_checked_date = $today
                    """,
                    doi=doi, today=today, **f,
                )
        print(f"\n  wrote ORI finding fields for {len(matches)} paper(s)")
    driver.close()


if __name__ == "__main__":
    main()
