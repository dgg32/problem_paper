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
returns the actual files as a ZIP. So a downloadable Y gives the forensic sensor
a direct download URL, not just a boolean.

CRITICAL nuance: `hasSuppl=Y` means "supplementary material exists per the
record", NOT "downloadable as a ZIP". The OA bundle endpoint only serves
articles in Europe PMC's open-access subset; for others it 404s even with
hasSuppl=Y (verified: PMC5796892 is hasSuppl=Y + inPMC=Y but its ZIP endpoint
returns 404). So this sensor HEAD-checks the ZIP endpoint before ever marking a
paper 'downloadable'. Four states result:
  pmc_suppl              : ZIP endpoint verified 200 — actually fetchable (the
                           useful case; the only state that sets has_pmc_suppl)
  suppl_not_downloadable : hasSuppl=Y but the ZIP endpoint 404s — suppl exists
                           per the record but Europe PMC can't serve it here
  no_pmc_suppl           : article IS in PMC but hasSuppl=N (no suppl found)
  unknown                : article not in PMC (or not indexed) — can't tell
                           (still a soft negative, not a confirmed "no")

Fields written onto Paper nodes (facts, not verdicts — plan.md §0):
  pmc_suppl_status   : one of the four states above
  has_pmc_suppl      : bool convenience (status == "pmc_suppl" — i.e. verified
                       downloadable, NOT merely hasSuppl=Y)
  pmc_suppl_url      : the supplementaryFiles ZIP endpoint, only when downloadable
  suppl_checked_date : provenance

Usage:
  python sensors/suppl_data_check.py                 # all active candidates
  python sensors/suppl_data_check.py --limit 20      # test on a handful
  python sensors/suppl_data_check.py --dry-run       # fetch + report, no writes
  python sensors/suppl_data_check.py --skip-verify   # trust hasSuppl, skip ZIP HEAD
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


def zip_available(session: requests.Session, pmcid: str) -> bool:
    """Does the OA supplementaryFiles ZIP endpoint actually serve this article?

    Critical: hasSuppl=Y means 'supplementary material exists per the record', NOT
    'downloadable as a ZIP'. The OA bundle endpoint only serves articles in Europe
    PMC's open-access subset; for others it 404s even with hasSuppl=Y (verified:
    PMC5796892 is hasSuppl=Y + inPMC=Y but its ZIP endpoint returns 404). So we
    HEAD the endpoint before ever calling a paper's suppl 'downloadable'."""
    try:
        r = session.head(SUPPL_URL.format(pmcid=pmcid), timeout=15, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def classify(rec: dict, session: requests.Session | None) -> tuple[str, str | None]:
    """(pmc_suppl_status, pmc_suppl_url) from a Europe PMC core result.

    Four states:
      pmc_suppl             — ZIP endpoint verified downloadable (200); url set
      suppl_not_downloadable — hasSuppl=Y but the OA ZIP endpoint 404s (exists,
                               not fetchable here)
      no_pmc_suppl          — in PMC, hasSuppl=N (no suppl found)
      unknown               — not in PMC (can't tell)
    Pass session=None to skip the live ZIP check (provisional, trusts hasSuppl)."""
    has_suppl = (rec.get("hasSuppl") or "").upper() == "Y"
    in_pmc = (rec.get("inPMC") or "").upper() == "Y"
    pmcid = rec.get("pmcid")
    if has_suppl and pmcid:
        if session is None or zip_available(session, pmcid):
            return "pmc_suppl", SUPPL_URL.format(pmcid=pmcid)
        return "suppl_not_downloadable", None
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
    ap.add_argument("--skip-verify", action="store_true",
                    help="trust hasSuppl=Y without HEAD-checking the ZIP endpoint (faster, less accurate)")
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

    verifier = None if args.skip_verify else session
    results: list[dict] = []
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        by_pmid = fetch_batch(session, [r["pmid"] for r in chunk])
        for r in chunk:
            rec = by_pmid.get(str(r["pmid"]))
            if rec is None:
                status, url = "unknown", None  # not returned by Europe PMC MED
            else:
                status, url = classify(rec, verifier)
            results.append({"doi": r["doi"], "pmid": r["pmid"], "status": status, "url": url})
        print(f"  [{min(i + BATCH, len(rows))}/{len(rows)}]", file=sys.stderr)
        time.sleep(REQ_INTERVAL)

    counts = {"pmc_suppl": 0, "suppl_not_downloadable": 0, "no_pmc_suppl": 0, "unknown": 0}
    for r in results:
        counts[r["status"]] += 1
    verified = " (hasSuppl trusted, ZIP not verified)" if args.skip_verify else " (ZIP endpoint verified 200)"
    print("\n=== suppl-data-check ===")
    print(f"  pmc_suppl             (downloadable){verified} : {counts['pmc_suppl']}")
    print(f"  suppl_not_downloadable (hasSuppl=Y, ZIP 404)   : {counts['suppl_not_downloadable']}")
    print(f"  no_pmc_suppl           (in PMC, none)          : {counts['no_pmc_suppl']}")
    print(f"  unknown                (not in PMC)            : {counts['unknown']}")

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
