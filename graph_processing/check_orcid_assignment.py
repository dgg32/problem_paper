#!/usr/bin/env python3
"""
check_orcid_assignment.py — measure OpenAlex ORCID mis-assignment on the graph's
highest-stakes nodes: the adjudicated authors (flagged for a formal misconduct
finding). For each such author WITH an ORCID, ask their OWN ORCID record whether
they actually claim the paper(s) that triggered the adjudication.

Verdicts (per author, over their adjudicated_dois):
  CONFIRMED     the ORCID record claims >=1 of the adjudicated DOIs -> flag stands
  MISASSIGNED   record is non-empty but claims NONE of them        -> likely wrong
                (OpenAlex stamped this person's ORCID on someone else's paper)
  INCONCLUSIVE  ORCID record has no works with DOIs -> can't judge from claims
  ERROR         ORCID API lookup failed

The MISASSIGNED rate is measured only over authors whose record is non-empty
(CONFIRMED + MISASSIGNED) — an empty record proves nothing (plan discussion).

Output: console summary + a TSV of the MISASSIGNED candidates for human review.
No graph writes. Read-only measurement.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orcid_client import OrcidClient, canon_doi, load_config       # noqa: E402
from normalize_authors import resolve_connection                  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_TSV = REPO_ROOT / "data" / "graph" / "adjudicated_orcid_check.tsv"

ADJUDICATED_QUERY = """
MATCH (a:Author {adjudicated:true})
WHERE a.orcid IS NOT NULL
RETURN a.orcid AS orcid, a.name AS name, a.adjudicated_dois AS dois
ORDER BY a.name
"""


def main() -> None:
    cfg = load_config()
    rps = cfg["orcid"].get("requests_per_second", 8)
    min_interval = 1.0 / rps if rps else 0.0

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as session:
        authors = [dict(r) for r in session.run(ADJUDICATED_QUERY)]
    driver.close()

    client = OrcidClient(cfg)
    client.token()  # fail fast if credentials are wrong

    results = []
    last = 0.0
    for i, a in enumerate(authors, 1):
        wait = min_interval - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()

        adj_dois = {canon_doi(d) for d in (a["dois"] or []) if d}
        try:
            claimed = set(client.get_claimed_dois(a["orcid"]))
        except Exception as e:  # noqa: BLE001
            results.append({**a, "verdict": "ERROR", "n_claimed": 0,
                            "detail": str(e)[:80]})
            print(f"  [{i}/{len(authors)}] {a['name']:<28} ERROR")
            continue

        if not claimed:
            verdict = "INCONCLUSIVE"
        elif adj_dois & claimed:
            verdict = "CONFIRMED"
        else:
            verdict = "MISASSIGNED"
        results.append({**a, "adj_dois": adj_dois, "claimed": claimed,
                        "verdict": verdict, "n_claimed": len(claimed)})
        print(f"  [{i}/{len(authors)}] {a['name']:<28} {verdict} "
              f"(claims {len(claimed)})")

    summarize(results, len(authors))


def summarize(results: list[dict], total: int) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    confirmed = counts.get("CONFIRMED", 0)
    misassigned = counts.get("MISASSIGNED", 0)
    judged = confirmed + misassigned
    rate = (misassigned / judged * 100) if judged else 0.0

    print("\n=== ORCID assignment check — adjudicated authors ===")
    print(f"  adjudicated authors with ORCID : {total}")
    for v in ("CONFIRMED", "MISASSIGNED", "INCONCLUSIVE", "ERROR"):
        if v in counts:
            print(f"    {v:12}: {counts[v]:4}")
    print(f"  MIS-ASSIGNMENT RATE (of judged): {misassigned}/{judged} = {rate:.1f}%")
    print("    (judged = CONFIRMED + MISASSIGNED; empty records excluded)")

    mis = [r for r in results if r["verdict"] == "MISASSIGNED"]
    if mis:
        REPORT_TSV.parent.mkdir(parents=True, exist_ok=True)
        lines = ["\t".join(["name", "orcid", "adjudicated_dois", "n_claimed",
                            "sample_claimed_dois"])]
        for r in sorted(mis, key=lambda x: -x["n_claimed"]):
            lines.append("\t".join([
                r["name"], r["orcid"],
                " | ".join(sorted(r["adj_dois"])),
                str(r["n_claimed"]),
                " | ".join(sorted(r["claimed"])[:5]),
            ]))
        REPORT_TSV.write_text("\n".join(lines) + "\n")
        print(f"\n  {len(mis)} MISASSIGNED candidates -> {REPORT_TSV.relative_to(REPO_ROOT)}")
        print("  (each is a misconduct flag that may sit on the wrong person — review)")


if __name__ == "__main__":
    main()
