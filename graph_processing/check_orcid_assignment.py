#!/usr/bin/env python3
"""
check_orcid_assignment.py — measure OpenAlex ORCID mis-assignment on the graph's
highest-stakes nodes: AuthorInstance nodes directly on a misconduct-retracted
paper (on_misconduct_paper=true, set by mark_adjudication.py), grouped by ORCID.
For each such ORCID, ask the person's OWN ORCID record whether they actually
claim the paper(s) that triggered the flag.

FIXED 2026-09-16: this originally queried a flat (:Author {adjudicated:true,
adjudicated_dois:[...]}) node, a schema from before the instance-based identity
layer (build_instances.py / link_instances.py / cluster_instances.py /
mark_adjudication.py) replaced it. That label has 0 nodes in the current graph,
so this script silently audited nothing. Rewritten against the current
AuthorInstance/on_misconduct_paper schema. See also link_instances.py's
orcid_match tier (0.97, unconditional) and cluster_instances.py's
coherence_outlier flag, both downstream consumers of exactly the ORCID trust
this script measures.

Verdicts (per ORCID, over the DOIs of its on_misconduct_paper instances):
  CONFIRMED     the ORCID record claims >=1 of those DOIs -> flag stands
  MISASSIGNED   record is non-empty but claims NONE of them        -> likely wrong
                (OpenAlex stamped this person's ORCID on someone else's paper)
  INCONCLUSIVE  ORCID record has no works with DOIs -> can't judge from claims
  ERROR         ORCID API lookup failed

The MISASSIGNED rate is measured only over ORCIDs whose record is non-empty
(CONFIRMED + MISASSIGNED) — an empty record proves nothing (plan discussion).

Output: console summary + a TSV of the MISASSIGNED candidates for human review.

WRITES 2026-09-16 (previously read-only): sets `orcid_doi_confirmed` on each
on_misconduct_paper AuthorInstance, true/false/absent (never written when
INCONCLUSIVE or ERROR, so "property missing" always means "not judged" rather
than a silent false). This is per (orcid, doi), not per person, since the same
person can be CONFIRMED on one misconduct paper and unconfirmed on another.
build_review_page.py reads it to show a trust/caution badge on co-author
evidence rows (graph_processing/build_review_page.py's COAUTHOR_QUERY), instead
of silently trusting or silently doubting every ORCID-sourced attribution alike.
Never auto-detaches or reweights anything itself, same discipline as
coherence_outlier (cluster_instances.py); a reviewer decides what to do with it.
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
MATCH (a:AuthorInstance {on_misconduct_paper:true})-[:WROTE]->(p:Paper)
WHERE a.orcid IS NOT NULL
WITH a.orcid AS orcid, collect(DISTINCT a.name)[0] AS name, collect(DISTINCT p.doi) AS dois
RETURN orcid, name, dois
ORDER BY name
"""


def main() -> None:
    cfg = load_config()
    rps = cfg["orcid"].get("requests_per_second", 8)
    min_interval = 1.0 / rps if rps else 0.0

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as session:
        authors = [dict(r) for r in session.run(ADJUDICATED_QUERY)]

    client = OrcidClient(cfg)
    client.token()  # fail fast if credentials are wrong

    results = []
    doi_writes = []  # per-(orcid, doi) rows for the graph write below
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

        # Per-DOI, not per-person: the same ORCID can be CONFIRMED on one
        # misconduct paper and unconfirmed on another. Only write when the
        # record was non-empty (claimed); INCONCLUSIVE/ERROR write nothing,
        # so a missing property always means "not judged", never a silent False.
        if claimed:
            for doi in adj_dois:
                doi_writes.append({"orcid": a["orcid"], "doi": doi,
                                   "confirmed": doi in claimed})

    summarize(results, len(authors))
    write_doi_verdicts(driver, conn["database"], doi_writes)
    driver.close()


def write_doi_verdicts(driver, db: str, rows: list[dict]) -> None:
    if not rows:
        print("\n  no CONFIRMED/MISASSIGNED verdicts to write (all INCONCLUSIVE/ERROR)")
        return
    with driver.session(database=db) as s:
        s.run(
            "UNWIND $rows AS r "
            "MATCH (a:AuthorInstance {orcid:r.orcid, on_misconduct_paper:true})"
            "-[:WROTE]->(p:Paper {doi:r.doi}) "
            "SET a.orcid_doi_confirmed = r.confirmed",
            rows=rows,
        )
    n_confirmed = sum(1 for r in rows if r["confirmed"])
    print(f"\n  wrote orcid_doi_confirmed on {len(rows)} AuthorInstance-Paper pair(s) "
          f"({n_confirmed} confirmed, {len(rows) - n_confirmed} not confirmed)")


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
