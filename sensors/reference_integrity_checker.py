#!/usr/bin/env python3
"""
reference_integrity_checker.py — Phase 4 sensor #3 (plan.md).

*** HELD OUT OF THE ROUTINE SENSOR ROLLOUT (2026-07-20) -- see plan.md M4. ***
Code and Route 1 (DOI-based) logic are both intact and correct -- Route 1 is
precise, cross-checked against the universal doi.org resolver (fixed
2026-07-20) to catch DataCite/Zenodo-registered DOIs Crossref alone 404s on.
But Route 2 (no-DOI bibliographic search) flagged ~71% of the corpus HIGH,
overwhelmingly real citations that just don't index well for title search
(Bergey's Manual taxonomic chapters, pre-DOI species-naming authorities,
LPSN, gray literature) -- so its output is NOT wired into tier_a_scoring.py's
WEIGHTS (see that file's comment). On top of being unscored, a full run
takes ~2 hours even with 3x concurrency (Route 2's Crossref+PubMed fallback
fires per no-DOI reference, and most references in this corpus lack a DOI).
Not worth that cost for a signal that isn't scored -- kept out of routine
runs until Route 2 is fixed or split out. Still runnable manually; see Usage
below. Do not delete -- Route 1 alone may be worth reviving as a scored,
DOI-only signal later.

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
from concurrent.futures import ThreadPoolExecutor
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

# Crossref's polite pool documents a concurrency limit of 3 simultaneous
# connections (https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/).
# Per-reference lookups dominate runtime (a single paper can carry hundreds of
# references), so this is where parallelism actually pays off; stay at the
# documented cap rather than the requests_per_second value, which only paces
# the outer per-paper loop.
REF_CONCURRENCY = 3


def canon_doi(doi: str) -> str:
    """Normalize a DOI for comparison."""
    if not doi:
        return ""
    d = doi.strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


def _first_or_str(title) -> str:
    """Crossref 'title' is a list; some records give a bare string or None."""
    if isinstance(title, list):
        return title[0] if title else ""
    return title or ""


class LookupUnavailable(Exception):
    """A source (Crossref/PubMed) could not be reached — distinct from 'not found'.

    Critical for §0: a transport failure must NEVER be recorded as a fabricated /
    unresolvable reference (a HIGH-severity, accusatory flag). Callers treat this
    as 'could not assess' and emit no flag."""


def crossref_work_by_doi(doi: str) -> dict | None:
    """Fetch a Crossref work record by DOI. Returns None ONLY for a genuine 404;
    raises LookupUnavailable on any transport/parse error (never 'not found')."""
    canon = canon_doi(doi)
    if not canon:
        return None
    try:
        url = f"{CROSSREF['base_url']}/works/{canon}"
        r = requests.get(url, params={"mailto": CROSSREF["mailto"]}, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("message", {})
    except (requests.RequestException, ValueError) as e:
        raise LookupUnavailable(f"Crossref DOI lookup failed: {e}") from e


def doi_resolves(doi: str) -> bool:
    """Check whether a DOI is registered at all via the universal doi.org
    resolver, which covers every registration agency (Crossref, DataCite,
    mEDRA, ...) — not just Crossref. Crossref's own API 404s on any DOI it
    doesn't register, e.g. Zenodo/DataCite software & dataset DOIs, which are
    routine citations in bioinformatics papers and are NOT evidence of
    fabrication. A single hop with redirects disabled is enough: doi.org
    answers from its own registry, so this doesn't depend on the target
    site supporting HEAD. Raises LookupUnavailable on transport error."""
    try:
        r = requests.head(f"https://doi.org/{doi}", timeout=10, allow_redirects=False)
        return r.status_code in (200, 301, 302, 303, 307, 308)
    except requests.RequestException as e:
        raise LookupUnavailable(f"doi.org lookup failed: {e}") from e


def crossref_search(title: str, author: str | None = None, year: int | None = None) -> dict | None:
    """Bibliographic search via Crossref. Returns best match, or None for a
    genuine empty result; raises LookupUnavailable on transport/parse error."""
    query = title
    if author:
        query += f" {author}"
    params = {
        "query.bibliographic": query,
        "rows": 3,
        "mailto": CROSSREF["mailto"],
    }
    try:
        url = f"{CROSSREF['base_url']}/works"
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
    except (requests.RequestException, ValueError) as e:
        raise LookupUnavailable(f"Crossref search failed: {e}") from e
    if not items:
        return None
    best = items[0]
    best["score"] = best.get("score", 0)
    return best


def pubmed_search(title: str, author: str | None = None) -> str | None:
    """Search PubMed by title + author. Returns PMID of best match, or None for a
    genuine empty result; raises LookupUnavailable on transport/parse error."""
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
    try:
        url = f"{PUBMED['base_url']}/esearch.fcgi"
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        ids = r.json().get("esearchresult", {}).get("idlist", [])
    except (requests.RequestException, ValueError) as e:
        raise LookupUnavailable(f"PubMed search failed: {e}") from e
    return ids[0] if ids else None


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

    try:
        return _assess_reference_online(ref_doi, ref_title, ref_author, ref_year)
    except LookupUnavailable:
        # A source was unreachable — cannot assess. Emit NO flag rather than a
        # false "fabricated reference" accusation (§0).
        return None


def _assess_reference_online(ref_doi, ref_title, ref_author, ref_year) -> dict | None:
    # Route 1: DOI-based lookup
    if ref_doi:
        work = crossref_work_by_doi(ref_doi)
        if work:
            # Found by DOI. Check title/author match.
            work_title = _first_or_str(work.get("title"))
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
            # Not Crossref-registered. Could still be a legitimate DOI from a
            # different registration agency (e.g. Zenodo/DataCite) — check the
            # universal resolver before calling it fabricated.
            if doi_resolves(ref_doi):
                return None  # valid DOI, just not a Crossref registrant
            return {
                "severity": "high",
                "reason": f"DOI {ref_doi} not resolvable via Crossref or doi.org",
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
    citing_title = _first_or_str(work.get("title"))
    flags = []
    with ThreadPoolExecutor(max_workers=REF_CONCURRENCY) as pool:
        for flag in pool.map(lambda ref: assess_reference(ref, doi), references):
            if flag:
                flag["citing_paper_doi"] = doi
                flag["citing_paper_title"] = citing_title
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
            "MATCH (p:Paper {is_retracted:false}) RETURN p.doi AS doi "
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
