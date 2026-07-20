#!/usr/bin/env python3
"""
refresh_full_text.py — batch-acquire open-access full text for all candidates.

The full-text-dependent sensors (tortured-phrases, ai-text-tell, p-value,
reference-integrity) and paperconan/image forensics are only as good as our
full-text coverage. A broken Europe PMC endpoint (fixed 2026-07-20) meant the
best OA source never fired, leaving most papers with short Crossref landing-page
text. This re-runs the fixed fetcher across every candidate to populate
data/full_text_cache/ with real full text, and records coverage on the node.

Idempotent: fetch_full_text() returns cached text only when it's already real
full text (>= MIN_FULLTEXT), so re-running only re-fetches the short/failed ones.
Writes onto each Paper: full_text_chars, full_text_source, full_text_checked_date.

Usage:
  python graph_processing/refresh_full_text.py            # all active candidates
  python graph_processing/refresh_full_text.py --limit 30 # test on a handful
  python graph_processing/refresh_full_text.py --in-pmc   # only papers in PMC (highest hit rate)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
sys.path.insert(0, str(REPO_ROOT / "sensors"))
from _conn import resolve_connection  # noqa: E402
from full_text_fetcher import fetch_full_text, MIN_FULLTEXT  # noqa: E402

CACHE = REPO_ROOT / "data" / "full_text_cache"
REQ_INTERVAL = 0.25  # polite pacing between papers (each may hit multiple sources)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="only the first N candidates (0 = all)")
    ap.add_argument("--in-pmc", action="store_true", help="only papers in PMC (pmc_suppl / no_pmc_suppl)")
    args = ap.parse_args()

    where = ("p.pmc_suppl_status IN ['pmc_suppl','no_pmc_suppl']" if args.in_pmc
             else "true")
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(
            f"MATCH (p:Paper {{is_retracted:false}}) WHERE {where} "
            "RETURN p.doi AS doi ORDER BY p.cited_by_count DESC")]
    if args.limit:
        rows = rows[: args.limit]
    print(f"  acquiring full text for {len(rows)} candidate(s) (MIN_FULLTEXT={MIN_FULLTEXT})")

    got = short = failed = 0
    updates = []
    for i, r in enumerate(rows, 1):
        res = fetch_full_text(r["doi"], cache_dir=CACHE)
        n = len(res.get("text") or "")
        if n >= MIN_FULLTEXT:
            got += 1
        elif res.get("status") == "ok":
            short += 1
        else:
            failed += 1
        updates.append({"doi": r["doi"], "chars": n, "source": res.get("source")})
        if i % 25 == 0 or i == len(rows):
            print(f"  [{i}/{len(rows)}] full={got} short={short} none={failed}", file=sys.stderr)
        time.sleep(REQ_INTERVAL)

    today = str(date.today())
    with driver.session(database=conn["database"]) as s:
        for u in updates:
            s.run("MATCH (p:Paper {doi:$doi}) "
                  "SET p.full_text_chars=$chars, p.full_text_source=$source, p.full_text_checked_date=$today",
                  doi=u["doi"], chars=u["chars"], source=u["source"], today=today)
    driver.close()

    print("\n=== full-text acquisition ===")
    print(f"  real full text (>= {MIN_FULLTEXT} chars) : {got}  ({100*got//max(len(rows),1)}%)")
    print(f"  short (landing page / abstract)          : {short}")
    print(f"  no OA full text found                    : {failed}")
    print(f"  wrote full_text_chars onto {len(updates)} Paper node(s)")


if __name__ == "__main__":
    main()
