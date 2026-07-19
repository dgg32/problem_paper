#!/usr/bin/env python3
"""
suppl_data_check.py — flag which candidate papers have fetchable supplementary
data, so the file-dependent forensic sensors (image-duplication / "Paperconan"-
style checks, data-table sensors) only run where there is actually something to
download.

Why this exists: those sensors need the paper's supplementary figures/tables in
hand. Rather than have each of them independently discover (and re-discover)
whether a paper HAS suppl files, this writes one shared, cached fact onto each
Paper node up front. It is a gate, not a verdict — it says nothing about
integrity, only about file availability.

Source: Europe PMC's RESTful web service (EMBL-EBI), keyless and explicitly
built for programmatic reuse (the /europepmc/webservices/ path IS the API — no
robots issue like the DOAJ bulk file). Its `core` result carries a per-article
`hasSuppl` (Y/N) flag, and for `hasSuppl=Y` papers a companion endpoint
  https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles
returns the actual files as a ZIP (verified content-type: application/zip). So a
Y here gives the forensic sensor a direct download URL, not just a boolean.

Three-state, NOT a boolean (this is the important nuance): `hasSuppl` is a
reliable POSITIVE but a soft negative. `Y` means PMC has downloadable suppl
files. `N` can mean "no suppl" OR "suppl exists only behind the publisher's
paywall, not mirrored in PMC" — and for papers not in PMC at all, Europe PMC
simply can't see the suppl either way. So we record:
  pmc_suppl        : PMC has suppl files (fetchable — the useful case)
  no_pmc_suppl     : article IS in PMC but no suppl files found there
  unknown          : article not in PMC (or not indexed) — can't tell

Fields written onto Paper nodes (facts, not verdicts — plan.md §0):
  pmc_suppl_status   : one of the three states above
  has_pmc_suppl      : bool convenience (status == "pmc_suppl")
  pmc_suppl_url      : the supplementaryFiles ZIP endpoint, when fetchable
  suppl_checked_date : provenance

Usage:
  python sensors/suppl_data_check.py                 # all active candidates
  python sensors/suppl_data_check.py --limit 20      # test on a handful
  python sensors/suppl_data_check.py --dry-run       # fetch + report, no writes
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import requests
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
SUPPL_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"

BATCH = 40          # PMIDs per Europe PMC query (keeps the URL well-sized)
REQ_INTERVAL = 0.2  # self-imposed politeness pace (~5 req/s); no stated hard limit


def classify(rec: dict) -> tuple[str, str | None]:
    """(pmc_suppl_status, pmc_suppl_url) from a Europe PMC core result."""
    has_suppl = (rec.get("hasSuppl") or "").upper() == "Y"
    in_pmc = (rec.get("inPMC") or "").upper() == "Y"
    pmcid = rec.get("pmcid")
    if has_suppl and pmcid:
        return "pmc_suppl", SUPPL_URL.format(pmcid=pmcid)
    if in_pmc:
        return "no_pmc_suppl", None
    return "unknown", None


def fetch_batch(session: requests.Session, pmids: list[str]) -> dict[str, dict]:
    """Query Europe PMC for a batch of PMIDs; returns {pmid: core_result}."""
    query = "(" + " OR ".join(f"EXT_ID:{p}" for p in pmids) + ") AND SRC:MED"
    r = session.get(
        EPMC_SEARCH,
        params={"query": query, "resultType": "core",
                "format": "json", "pageSize": len(pmids)},
        timeout=30,
    )
    r.raise_for_status()
    out: dict[str, dict] = {}
    for res in r.json().get("resultList", {}).get("result", []):
        # For SRC:MED the `id` field is the PMID.
        pid = res.get("id") or res.get("pmid")
        if pid:
            out[str(pid)] = res
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="only check the first N candidates (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, but do not write to the graph")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(
            "MATCH (p:Paper {is_retracted:false}) WHERE p.pmid IS NOT NULL "
            "RETURN p.doi AS doi, p.pmid AS pmid ORDER BY p.cited_by_count DESC"
        )]
    if args.limit:
        rows = rows[: args.limit]
    print(f"  checking {len(rows)} candidate(s) with a PMID against Europe PMC")

    session = requests.Session()
    session.headers["User-Agent"] = "problem-paper-poc/1.0 (research-integrity triage)"

    results: list[dict] = []
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        by_pmid = fetch_batch(session, [r["pmid"] for r in chunk])
        for r in chunk:
            rec = by_pmid.get(str(r["pmid"]))
            if rec is None:
                status, url = "unknown", None  # not returned by Europe PMC MED
            else:
                status, url = classify(rec)
            results.append({"doi": r["doi"], "pmid": r["pmid"], "status": status, "url": url})
        print(f"  [{min(i + BATCH, len(rows))}/{len(rows)}]", file=sys.stderr)
        time.sleep(REQ_INTERVAL)

    counts = {"pmc_suppl": 0, "no_pmc_suppl": 0, "unknown": 0}
    for r in results:
        counts[r["status"]] += 1
    print("\n=== suppl-data-check ===")
    print(f"  pmc_suppl    (fetchable files) : {counts['pmc_suppl']}")
    print(f"  no_pmc_suppl (in PMC, none)    : {counts['no_pmc_suppl']}")
    print(f"  unknown      (not in PMC)      : {counts['unknown']}")

    if args.dry_run:
        print("\n  --dry-run: no graph writes")
        for r in results[:10]:
            print(f"    {r['status']:12} {r['doi']}  {r['url'] or ''}")
        driver.close()
        return

    today = str(date.today())
    with driver.session(database=conn["database"]) as s:
        for r in results:
            s.run(
                "MATCH (p:Paper {doi:$doi}) "
                "SET p.pmc_suppl_status=$status, p.has_pmc_suppl=$has, "
                "    p.pmc_suppl_url=$url, p.suppl_checked_date=$today",
                doi=r["doi"], status=r["status"], has=(r["status"] == "pmc_suppl"),
                url=r["url"], today=today,
            )
    driver.close()
    print(f"\n  wrote suppl status for {len(results)} paper(s)")
    print(f"  {counts['pmc_suppl']} now have a fetchable pmc_suppl_url for the forensic sensors")


if __name__ == "__main__":
    main()
