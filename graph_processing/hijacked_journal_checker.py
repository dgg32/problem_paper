#!/usr/bin/env python3
"""
hijacked_journal_checker.py — cross-checks this graph's Journal nodes against
the Retraction Watch / Anna Abalkina "Hijacked Journal Checker" (skill_worth_
exploring.md 2026-07-22: "Hijacked Journal Checker", build-now).

WHAT A HIJACKED JOURNAL IS: a scam website that clones a real, legitimate
journal's name/ISSN (sometimes exactly) to solicit and "publish" articles for
a fee, trading on the real journal's reputation and indexing. DOAJ-seal or
Scopus/WoS-delisting checks (journal_integrity_check.py's other two checks)
can't catch this: DOAJ still lists the REAL journal's ISSN as legitimate,
because the hijack doesn't touch the real journal's own standing -- it just
impersonates it on a different domain.

Source: the Retraction Watch Hijacked Journal Checker, a public Google Sheet
maintained by Anna Abalkina + Retraction Watch (456 entries as of this
writing), columns: Hijacked Journal Title / URL (Hijacked) / ISSN (Hijacked)
/ Original journal / ISSN (Original) / URL (Original Journal). Pulled via
Google Sheets' CSV export endpoint, no auth needed.

DELIBERATELY NOT SCORED, unlike the doc's own suggestion to feed this
straight into journal_integrity_flag_count as a 4th check. Reasoning
(plan.md §0 -- every scored point must be an explainable, low-false-positive
fact about THIS candidate, not about a bad actor elsewhere): many entries in
this sheet show the hijacked clone using the EXACT SAME ISSN as the real
journal (that's literally what "mirrors real ISSNs" means) -- and this
graph's Journal nodes are deduplicated by name/ISSN with no per-paper
publisher-domain data to tell which "instance" (real site vs. clone) a given
paper actually came from. Scoring this would risk penalizing a paper
legitimately published in the REAL, unhijacked journal for the unrelated
fact that someone else is impersonating its name elsewhere -- the wrong
direction of false positive. So this is surfaced as labelled REVIEW CONTEXT
only (same bucket as institution_global_retraction_count/GDS prior): "this
journal's name/ISSN has been documented as a hijacking target -- verify
which site this paper actually appeared on," never a scored flag.

Matching: normalized-name exact match (same normalize() idiom as
journal_retraction_rate_external.py) OR exact ISSN match, checked against
BOTH the hijacked-clone columns and the original-journal columns (either
means this journal's identity is entangled in a real, documented hijacking
case). Absence of a match is left null, never guessed.

NOTE on "which side matched": many sheet rows use the IDENTICAL title (and
even the identical ISSN) for both the clone and the real journal -- that's
literally what a hijack is. So rather than try to label "this is the real
one, that's the clone" (unreliable when both sides read the same), every
match just stores BOTH URLs from the matched row unconditionally -- a
reviewer can always tell them apart because one is the clone domain
(journal_hijack_hijacked_url) and one is the legitimate publisher domain
(journal_hijack_original_url), regardless of which column triggered the
match.

Fields written (Journal, then copied straight to Paper via PUBLISHED_IN --
one journal per paper, no aggregation needed unlike the retr_rate sensors):
  journal_hijack_flag          : true/false
  journal_hijack_original_url  : the real journal's own site (sheet's
                                  "URL (Original Journal)")
  journal_hijack_hijacked_url  : the clone site's URL, for a reviewer to check
  journal_hijack_checked_date  : provenance

Usage:
  python graph_processing/hijacked_journal_checker.py
"""
from __future__ import annotations

import csv
import io
import re
import sys
from datetime import date
from pathlib import Path

import requests
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

SHEET_ID = "1ak985WGOgGbJRJbZFanoktAN_UFeExpE"
SHEET_GID = "5255084"
SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"

_PAREN_RE = re.compile(r"\([^)]*\)")
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize(name: str) -> str:
    if not name:
        return ""
    n = _PAREN_RE.sub(" ", name)
    n = n.replace("&", " and ")
    n = _PUNCT_RE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip().lower()
    return n


def load_hijack_entries() -> list[dict]:
    """Downloads and parses the Hijacked Journal Checker sheet. Row 0 is a
    donation/description banner, row 1 is the real header -- both skipped."""
    r = requests.get(SHEET_CSV_URL, timeout=60)
    r.raise_for_status()
    reader = csv.reader(io.StringIO(r.text))
    rows = list(reader)
    entries = []
    for row in rows[2:]:
        if len(row) < 7 or not any(c.strip() for c in row):
            continue
        _, htitle, hurl, hissn, otitle, oissn, ourl = row[:7]
        entries.append({
            "hijacked_title": htitle.strip(), "hijacked_url": hurl.strip(),
            "hijacked_issn": [s.strip() for s in hissn.split(",") if s.strip()],
            "original_title": otitle.strip(), "original_url": ourl.strip(),
            "original_issn": [s.strip() for s in oissn.split(",") if s.strip()],
        })
    return entries


def build_indexes(entries: list[dict]) -> tuple[dict, dict]:
    """{normalized name -> entry}, {issn -> entry}, indexed from BOTH the
    hijacked-clone and original-journal columns of each row."""
    name_index: dict[str, dict] = {}
    issn_index: dict[str, dict] = {}
    for e in entries:
        for title_key, issn_key in (("hijacked_title", "hijacked_issn"), ("original_title", "original_issn")):
            n = normalize(e[title_key])
            if n and n not in name_index:
                name_index[n] = e
            for issn in e[issn_key]:
                if issn not in issn_index:
                    issn_index[issn] = e
    return name_index, issn_index


def match(journal_name: str, journal_issn: str | None, name_index: dict, issn_index: dict) -> dict | None:
    if journal_issn and journal_issn in issn_index:
        return issn_index[journal_issn]
    n = normalize(journal_name)
    if n in name_index:
        return name_index[n]
    return None


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    print("[hijacked_journal_checker] downloading the Hijacked Journal Checker sheet...", file=sys.stderr)
    entries = load_hijack_entries()
    print(f"  {len(entries)} documented hijacking cases", file=sys.stderr)
    name_index, issn_index = build_indexes(entries)

    with driver.session(database=conn["database"]) as s:
        journals = [
            dict(r) for r in s.run(
                "MATCH (j:Journal) WHERE j.name IS NOT NULL "
                "RETURN j.name AS name, j.journal_crossref_issn AS issn"
            )
        ]
    print(f"[hijacked_journal_checker] {len(journals)} distinct journals in our graph", file=sys.stderr)

    today = str(date.today())
    results = []
    for j in journals:
        entry = match(j["name"], j.get("issn"), name_index, issn_index)
        if entry is None:
            results.append({"journal": j["name"], "flag": False})
            continue
        results.append({
            "journal": j["name"],
            "flag": True,
            "original_url": entry["original_url"],
            "hijacked_url": entry["hijacked_url"],
        })

    with driver.session(database=conn["database"]) as s:
        for r in results:
            if not r["flag"]:
                continue
            s.run(
                """
                MATCH (j:Journal {name: $journal})
                SET j.journal_hijack_flag = true,
                    j.journal_hijack_original_url = $original_url,
                    j.journal_hijack_hijacked_url = $hijacked_url,
                    j.journal_hijack_checked_date = $today
                """,
                journal=r["journal"], original_url=r["original_url"],
                hijacked_url=r["hijacked_url"], today=today,
            )

        print("[hijacked_journal_checker] per-paper copy from paper's own journal...", file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)
            SET p.journal_hijack_flag = null,
                p.journal_hijack_original_url = null,
                p.journal_hijack_hijacked_url = null
            """
        ).consume()
        s.run(
            """
            MATCH (p:Paper)-[:PUBLISHED_IN]->(j:Journal)
            WHERE j.journal_hijack_flag = true
            SET p.journal_hijack_flag = true,
                p.journal_hijack_original_url = j.journal_hijack_original_url,
                p.journal_hijack_hijacked_url = j.journal_hijack_hijacked_url
            """
        ).consume()

        dist = s.run(
            """
            MATCH (p:Paper {is_retracted:false})
            RETURN count(*) AS n, sum(CASE WHEN p.journal_hijack_flag THEN 1 ELSE 0 END) AS with_flag
            """
        ).single()

    driver.close()

    matched = [r for r in results if r["flag"]]
    print(f"\n=== Hijacked Journal Checker — verification ===", file=sys.stderr)
    print(f"Matched {len(matched)} / {len(journals)} graph journals to a documented hijacking case:", file=sys.stderr)
    for r in matched:
        print(f"  {r['journal']}  (real site: {r['original_url'] or 'unknown'}; "
              f"clone site: {r['hijacked_url']})", file=sys.stderr)
    print(f"\nNot-yet-retracted candidates with journal_hijack_flag: {dist['with_flag']} / {dist['n']}", file=sys.stderr)
    print(
        "\nNOTE: this is unscored review context, not a scored flag -- see this module's docstring "
        "for why (a hijacking case says a bad actor is impersonating this journal's name/ISSN "
        "elsewhere; it does NOT mean this specific paper came from the impersonating site).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
