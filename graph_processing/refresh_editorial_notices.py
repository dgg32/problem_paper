#!/usr/bin/env python3
"""
refresh_editorial_notices.py — pull PubMed's own CommentsCorrectionsList for
every not-yet-retracted candidate and surface Expression of Concern / Erratum
notices our graph currently has zero visibility into.

Found 2026-07-19: our is_retracted field only tracks full retractions
(sourced from Retraction Watch + refresh_retraction_status.py's OpenAlex
recheck). It has no concept of "formally flagged by the journal but not
retracted" -- the intermediate state a paper can sit in for years. PubMed's
E-utilities efetch (already keyed in .env.yaml, same credentials used
throughout this project) exposes exactly that via each PubmedArticle's
CommentsCorrectionsList, with typed entries: RetractionIn,
ExpressionOfConcernIn, ErratumIn, CommentIn (ordinary editorial
commentary/letters -- NOT an integrity signal, ignored here).

Measured on the 732 not-yet-retracted candidates with a PMID (2026-07-19):
227 (31%) have a live Expression of Concern; 49 more have only an Erratum.
The three papers that motivated this whole investigation -- our top-3 by
PubPeer comment volume -- all turned out to have dated, DOI-linked EoC
notices neither OpenAlex nor Crossref surfaced. NOTE the EoC set is NOT 227
independent discoveries: 166 of 227 are a single coordinated mass action by
one journal (New Microbes and New Infections, tied to the IHU/Raoult
ethics-committee protocol cluster already visible in some PubPeer comments).
Each of those 166 papers is still individually, formally EoC'd -- that's a
real per-paper fact -- but be aware the ranking will cluster heavily on that
one journal unless read with that context.

Deliberately NOT touched here: the one PMID that also had a RetractionIn
during initial exploration turned out to be a shared PMID across 8 different
conference-poster-abstract Paper nodes (PubMed assigns one aggregate PMID to
an entire congress supplement issue) -- ambiguous which specific paper the
retraction notice applies to, needs manual disambiguation, not a clean
automated fix. This script only writes EoC/Erratum fields, never touches
is_retracted (that stays refresh_retraction_status.py's job).

Fields written (facts, not verdicts -- plan.md §0):
  pubmed_eoc_status          : "expression_of_concern" | "erratum_only" | "none"
  pubmed_eoc_date             : best-effort parsed date (YYYY, YYYY-MM, or
                                 YYYY-MM-DD depending on precision in the
                                 PubMed citation) -- never fabricated finer
                                 than what the citation actually states
  pubmed_eoc_source_doi       : DOI of the EoC/Erratum notice itself
  pubmed_eoc_source_citation  : raw PubMed citation string (evidence)
  pubmed_notices_checked_date : provenance

Idempotent: safe to re-run: overwrites with fresh PubMed data every time.

Usage:
  python graph_processing/refresh_editorial_notices.py             # check + apply
  python graph_processing/refresh_editorial_notices.py --dry-run    # check only
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import requests
import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

CONFIG_PATH = REPO_ROOT / ".env.yaml"
cfg = yaml.safe_load(CONFIG_PATH.read_text())
PUBMED = cfg.get("pubmed", {})

BATCH_SIZE = 150

MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}
DATE_RE = re.compile(r"(\d{4})\s+(\w{3})(?:\s+(\d{1,2}))?")
DOI_RE = re.compile(r"doi:\s*(10\.\S+?)\.?\s*$", re.I)


def parse_citation(ref_source: str) -> tuple[str | None, str | None]:
    """Returns (best_effort_date, doi) from a PubMed RefSource citation string.
    Never fabricates precision beyond what's stated -- YYYY-MM-DD only if a
    day is present, else YYYY-MM, else None."""
    m = DATE_RE.search(ref_source)
    parsed_date = None
    if m:
        year, mon_abbr, day = m.groups()
        month = MONTHS.get(mon_abbr[:3])
        if month:
            parsed_date = f"{year}-{month}-{day.zfill(2)}" if day else f"{year}-{month}"
    dm = DOI_RE.search(ref_source)
    doi = dm.group(1) if dm else None
    return parsed_date, doi


def fetch_batch(pmids: list[str], limiter_state: dict) -> dict[str, list[tuple[str, str]]]:
    """One efetch call for up to BATCH_SIZE pmids. Returns {pmid: [(reftype, refsource), ...]}."""
    rps = PUBMED.get("requests_per_second", 10)
    min_interval = 1.0 / rps
    wait = min_interval - (time.time() - limiter_state["last"])
    if wait > 0:
        time.sleep(wait)
    limiter_state["last"] = time.time()

    params = {
        "db": "pubmed", "id": ",".join(pmids), "retmode": "xml",
        "tool": PUBMED.get("tool_name", "fraud-paper-scanner"),
        "email": PUBMED.get("email", ""),
    }
    if PUBMED.get("api_key"):
        params["api_key"] = PUBMED["api_key"]

    r = requests.get(f"{PUBMED['base_url']}/efetch.fcgi", params=params, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.content)

    out: dict[str, list[tuple[str, str]]] = {}
    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext(".//PMID")
        ccl = article.find(".//CommentsCorrectionsList")
        entries = []
        if ccl is not None:
            for cc in ccl.findall("CommentsCorrections"):
                reftype = cc.get("RefType") or ""
                source = cc.findtext("RefSource") or ""
                if reftype in ("ExpressionOfConcernIn", "ErratumIn", "RetractionIn"):
                    entries.append((reftype, source))
        out[pmid] = entries
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="check only, do not write to the graph")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(
            "MATCH (p:Paper {is_retracted:false}) WHERE p.pmid IS NOT NULL AND p.pmid <> '' "
            "RETURN p.doi AS doi, p.pmid AS pmid"
        )]
    print(f"  checking {len(rows)} not-yet-retracted candidates with a PMID")

    pmids = [r["pmid"] for r in rows]
    limiter_state = {"last": 0.0}
    ccl_by_pmid: dict[str, list[tuple[str, str]]] = {}
    for i in range(0, len(pmids), BATCH_SIZE):
        batch = pmids[i:i + BATCH_SIZE]
        ccl_by_pmid.update(fetch_batch(batch, limiter_state))
        print(f"  [{min(i + BATCH_SIZE, len(pmids))}/{len(pmids)}]", file=sys.stderr)

    counts = {"expression_of_concern": 0, "erratum_only": 0, "none": 0}
    updates = []
    for r in rows:
        entries = ccl_by_pmid.get(r["pmid"], [])
        # RetractionIn deliberately excluded here -- see module docstring
        # (shared-PMID conference-supplement ambiguity found during exploration).
        eoc_entries = [(t, s) for t, s in entries if t == "ExpressionOfConcernIn"]
        erratum_entries = [(t, s) for t, s in entries if t == "ErratumIn"]

        if eoc_entries:
            status = "expression_of_concern"
            reftype, source = eoc_entries[0]
        elif erratum_entries:
            status = "erratum_only"
            reftype, source = erratum_entries[0]
        else:
            status = "none"
            source = None

        counts[status] += 1
        if status == "none":
            # Still an update, not a skip (BUG.md R3-5): if an earlier run wrote
            # pubmed_eoc_status='expression_of_concern' here and PubMed no longer
            # reports the notice (corrected, or was a transient CommentsCorrections
            # gap), the old status/date/source must be cleared -- otherwise a stale
            # EoC keeps contributing pubmed_eoc_flag's weight-10.0 (the single
            # largest weight in the system) to this paper's score indefinitely.
            updates.append({
                "doi": r["doi"],
                "status": "none",
                "date": None,
                "source_doi": None,
                "source_citation": None,
            })
            continue

        eoc_date, eoc_doi = parse_citation(source)
        updates.append({
            "doi": r["doi"],
            "status": status,
            "date": eoc_date,
            "source_doi": eoc_doi,
            "source_citation": source,
        })

    print(f"\n  expression_of_concern : {counts['expression_of_concern']}")
    print(f"  erratum_only          : {counts['erratum_only']}")
    print(f"  none                  : {counts['none']}")

    if args.dry_run:
        print("\n  --dry-run: no changes written.")
        driver.close()
        return

    today = str(date.today())
    with driver.session(database=conn["database"]) as s:
        for u in updates:
            s.run(
                """
                MATCH (p:Paper {doi: $doi})
                SET p.pubmed_eoc_status = $status,
                    p.pubmed_eoc_date = $date,
                    p.pubmed_eoc_source_doi = $source_doi,
                    p.pubmed_eoc_source_citation = $source_citation,
                    p.pubmed_notices_checked_date = $today
                """,
                today=today,
                **{k: v for k, v in u.items()},
            )
    driver.close()
    print(f"\n  wrote editorial-notice fields for {len(updates)} paper(s)")


if __name__ == "__main__":
    main()
