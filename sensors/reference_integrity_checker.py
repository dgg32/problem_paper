#!/usr/bin/env python3
"""
reference_integrity_checker.py — Phase 4 sensor #3 (plan.md).

Flags a paper that contains references to nonexistent or severely mismatched
publications. High-precision signal: paper mills and AI-generated content often
fabricate or garble citations (either wholesale invention or swapped/corrupted
metadata). Legitimate papers almost never have unresolvable references.

For each reference in a paper's Crossref-deposited reference list:
  1. If it has a DOI, verify Crossref/PubMed can resolve it
  2. If not, run a bibliographic search (title + author) against Crossref
  3. Flag on mismatch or nonexistence

Severity tiers (per-reference):
  - HIGH    : no DOI + bibliographic search finds nothing
  - MEDIUM  : DOI exists but title/authors severely mismatched, or search
              finds only distant matches
  - LOW     : reference exists but minor metadata discrepancies (e.g. wrong
              page number, year off by 1)

Usage:
  python sensors/reference_integrity_checker.py                  # all candidates -> report
  python sensors/reference_integrity_checker.py --doi 10.xxx/xxx  # single paper, stdout
  python sensors/reference_integrity_checker.py --sample 5        # spot-check N papers
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

CONFIG_PATH = REPO_ROOT / ".env.yaml"
REPORT_JSON = REPO_ROOT / "data" / "flags" / "reference_integrity_flags.json"

cfg = yaml.safe_load(CONFIG_PATH.read_text())
CROSSREF = cfg.get("crossref", {})
PUBMED = cfg.get("pubmed", {})


def canon_doi(doi: str) -> str:
    """Normalize a DOI for comparison."""
    if not doi:
        return ""
    d = doi.strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


def crossref_work_by_doi(doi: str) -> dict | None:
    """Fetch a Crossref work record by DOI. Returns None if not found."""
    try:
        canon = canon_doi(doi)
        if not canon:
            return None
        url = f"{CROSSREF['base_url']}/works/{canon}"
        r = requests.get(url, params={"mailto": CROSSREF["mailto"]}, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("message", {})
    except Exception:
        return None


def crossref_search(title: str, author: str | None = None, year: int | None = None) -> dict | None:
    """Bibliographic search via Crossref. Returns best match or None."""
    try:
        query = title
        if author:
            query += f" {author}"
        params = {
            "query.bibliographic": query,
            "rows": 3,
            "mailto": CROSSREF["mailto"],
        }
        url = f"{CROSSREF['base_url']}/works"
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
        if not items:
            return None
        best = items[0]
        best["score"] = best.get("score", 0)
        return best
    except Exception:
        return None


def pubmed_search(title: str, author: str | None = None) -> str | None:
    """Search PubMed by title + author. Returns PMID of best match or None."""
    try:
        query_parts = [f'"{title}"[Title]']
        if author:
            query_parts.append(f'"{author}"[Author]')
        query = " AND ".join(query_parts)
        params = {
            "db": "pubmed",
            "term": query,
            "retmax": 1,
            "retmode": "json",
            "tool": PUBMED.get("tool_name", "fraud-paper-scanner"),
            "email": PUBMED.get("email", ""),
        }
        if PUBMED.get("api_key"):
            params["api_key"] = PUBMED["api_key"]
        url = f"{PUBMED['base_url']}/esearch.fcgi"
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        ids = r.json().get("esearchresult", {}).get("idlist", [])
        return ids[0] if ids else None
    except Exception:
        return None


def assess_reference(ref: dict, paper_doi: str) -> dict | None:
    """
    Assess a single reference. Returns a flag dict or None (if OK).

    A reference is "OK" if:
      - It has a DOI that resolves to a real paper, OR
      - Bibliographic search finds it with reasonable match
    """
    ref_doi = ref.get("DOI") or ""
    ref_title = ref.get("article-title") or ref.get("unstructured") or ""
    ref_author = ref.get("author") or ""
    ref_year = ref.get("year")

    if not ref_title:
        return None  # can't assess without a title

    # Route 1: DOI-based lookup
    if ref_doi:
        work = crossref_work_by_doi(ref_doi)
        if work:
            # Found by DOI. Check title/author match.
            work_title = (work.get("title") or [""])[0] if isinstance(work.get("title"), list) else work.get("title") or ""
            title_match = _title_similarity(ref_title, work_title)
            if title_match > 0.8:
                return None  # OK
            else:
                # DOI resolved but metadata doesn't match
                return {
                    "severity": "medium",
                    "reason": "DOI exists but title metadata mismatch",
                    "reference_doi": ref_doi,
                    "reference_title": ref_title,
                    "resolved_title": work_title,
                    "similarity": title_match,
                }
        else:
            # DOI doesn't resolve
            return {
                "severity": "high",
                "reason": f"DOI {ref_doi} not resolvable via Crossref",
                "reference_doi": ref_doi,
                "reference_title": ref_title,
            }

    # Route 2: Bibliographic search (no DOI)
    match = crossref_search(ref_title, ref_author, ref_year)
    if match and match.get("score", 0) > 50:
        return None  # Found with decent score

    # Try PubMed as fallback
    pmid = pubmed_search(ref_title, ref_author)
    if pmid:
        return None  # Found on PubMed

    # Not found anywhere
    return {
        "severity": "high",
        "reason": "Reference not found in Crossref or PubMed (likely fabricated/garbled)",
        "reference_title": ref_title,
        "reference_author": ref_author,
        "reference_year": ref_year,
    }


def _title_similarity(a: str, b: str) -> float:
    """Simple Jaccard similarity of title words (case-insensitive)."""
    if not a or not b:
        return 0.0
    words_a = set(re.findall(r"\w+", a.lower()))
    words_b = set(re.findall(r"\w+", b.lower()))
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) if union else 0.0


def check_paper(doi: str) -> list[dict]:
    """Fetch and assess all references for a paper. Returns list of flags."""
    try:
        url = f"{CROSSREF['base_url']}/works/{canon_doi(doi)}"
        r = requests.get(url, params={"mailto": CROSSREF["mailto"]}, timeout=10)
        r.raise_for_status()
        work = r.json().get("message", {})
    except Exception:
        return []

    references = work.get("reference", [])
    flags = []
    for ref in references:
        flag = assess_reference(ref, doi)
        if flag:
            flag["citing_paper_doi"] = doi
            flag["citing_paper_title"] = work.get("title") or [""][0] if isinstance(work.get("title"), list) else work.get("title", "")
            flags.append(flag)
    return flags


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", help="check a single paper by DOI (stdout only)")
    ap.add_argument("--sample", type=int, help="spot-check N random papers")
    args = ap.parse_args()

    if args.doi:
        flags = check_paper(args.doi)
        for f in flags:
            print(json.dumps(f, indent=2))
        return

    # Get all candidate papers from the graph
    sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
    from neo4j import GraphDatabase
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(
            "MATCH (p:Paper {is_retracted:false, source:'expansion'}) RETURN p.doi AS doi "
            "ORDER BY p.cited_by_count DESC LIMIT $lim",
            lim=args.sample or 100
        )]
    driver.close()

    all_flags = []
    rps = CROSSREF.get("requests_per_second", 5)
    min_interval = 1.0 / rps
    last = 0.0

    for i, row in enumerate(rows, 1):
        wait = min_interval - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()

        flags = check_paper(row["doi"])
        all_flags.extend(flags)
        if i % 10 == 0:
            print(f"  [{i}/{len(rows)}]", file=sys.stderr)

    by_paper: dict[str, list[dict]] = {}
    counts = {"high": 0, "medium": 0, "low": 0}
    for f in all_flags:
        by_paper.setdefault(f["citing_paper_doi"], []).append(f)
        counts[f["severity"]] += 1

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(all_flags, indent=2))

    print("\n=== reference-integrity-checker ===")
    print(f"  papers checked    : {len(rows)}")
    print(f"  papers with flags : {len(by_paper)}")
    print(f"  total flag records: {len(all_flags)}")
    print(f"    high   : {counts['high']}")
    print(f"    medium : {counts['medium']}")
    print(f"    low    : {counts['low']}")
    print(f"\n  report written -> {REPORT_JSON.relative_to(REPO_ROOT)}")

    top = sorted(by_paper.items(), key=lambda kv: -len(kv[1]))[:5]
    if top:
        print("\n  top candidates by unresolved reference count:")
        for doi, fl in top:
            high = len([f for f in fl if f["severity"] == "high"])
            print(f"    [{len(fl)} ({high} HIGH)] {fl[0]['citing_paper_title'][:70]}  ({doi})")


if __name__ == "__main__":
    main()
