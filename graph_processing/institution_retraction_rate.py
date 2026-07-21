#!/usr/bin/env python3
"""
institution_retraction_rate.py — Tier-A graph feature: per-institution
retraction rate (plan.md §2.2g, reserved as "shared-institution" but never
implemented until now; see skill_worth_exploring.md 2026-07-21).

Same reasoning as gds_node_classification.py's journal_retr_rate: an
institution's retraction rate measured directly in this graph (retracted
papers involving it / all papers involving it) is a hard, sourced fact, not a
heuristic guess -- so it is safe to weight into the Tier-A score, unlike the
noisier metadata sensors in the same review (plan.md §0).

Institution is a PAPER-LEVEL fact here (plan.md §1: the CSV's Institution
column is a deduplicated pool per paper, not a per-author mapping), so this
reads (Paper)-[:INVOLVES]->(Institution) only -- never AuthorInstance-level.
An institution's rate says nothing about which specific author on a
multi-institution paper is associated with it (§0: same person/place ≠ same
responsibility).

A paper can touch several institutions (rel_involves.tsv averages several per
paper). We take the MAX rate among a paper's institutions, not the average --
one high-risk institution is a real signal even if the paper's other
institutions are clean, and averaging would dilute exactly the case worth
flagging. The name and paper_count of whichever institution produced that max
are stored alongside the rate so a reviewer can see whether it rests on 1
paper (near-meaningless) or dozens (robust). Deliberately no hard-coded
minimum-count cutoff baked into the number itself -- that would be an
unreviewable judgment call; the count is surfaced as context instead, same
"labelled, not silently thresholded" spirit as plan.md §0.

Idempotent: recomputes both Institution and Paper properties from scratch
every run.

Usage:
  python graph_processing/institution_retraction_rate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    with driver.session(database=conn["database"]) as s:
        print("[institution_retraction_rate] per-institution rate + paper_count...", file=sys.stderr)
        s.run(
            """
            MATCH (i:Institution)<-[:INVOLVES]-(p:Paper)
            WITH i, toFloat(sum(CASE WHEN p.is_retracted THEN 1 ELSE 0 END)) / count(p) AS rate,
                 count(p) AS n
            SET i.retraction_rate = rate, i.paper_count = n
            """
        ).consume()

        print("[institution_retraction_rate] per-paper max across involved institutions...", file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)
            SET p.institution_retr_rate = 0.0,
                p.institution_retr_rate_name = null,
                p.institution_retr_rate_n = null
            """
        ).consume()
        s.run(
            """
            MATCH (p:Paper)-[:INVOLVES]->(i:Institution)
            WITH p, i ORDER BY i.retraction_rate DESC
            WITH p, collect({name: i.name, rate: i.retraction_rate, n: i.paper_count})[0] AS top
            SET p.institution_retr_rate = top.rate,
                p.institution_retr_rate_name = top.name,
                p.institution_retr_rate_n = top.n
            """
        ).consume()

        top_institutions = s.run(
            """
            MATCH (i:Institution) WHERE i.paper_count >= 3
            RETURN i.name AS name, i.country AS country, i.retraction_rate AS rate, i.paper_count AS n
            ORDER BY rate DESC, n DESC LIMIT 15
            """
        ).data()

        dist = s.run(
            """
            MATCH (p:Paper {is_retracted:false})
            RETURN count(*) AS n,
                   sum(CASE WHEN p.institution_retr_rate > 0 THEN 1 ELSE 0 END) AS with_signal,
                   avg(p.institution_retr_rate) AS avg_rate
            """
        ).single()

    driver.close()

    print("\n=== Institution retraction rate — verification ===", file=sys.stderr)
    print(f"Not-yet-retracted candidates with a nonzero institution_retr_rate: "
          f"{dist['with_signal']} / {dist['n']}  (avg {dist['avg_rate']:.3f})", file=sys.stderr)
    print("\nTop institutions by retraction rate (paper_count >= 3, so not single-paper noise):", file=sys.stderr)
    for row in top_institutions:
        print(f"  {row['rate']:.1%}  n={row['n']:<4} {row['name']} ({row['country'] or '?'})", file=sys.stderr)


if __name__ == "__main__":
    main()
