#!/usr/bin/env python3
"""
known_miller_coauthor_checker.py — flags candidate papers CO-AUTHORED BY a
named, documented paper-miller (skill_worth_exploring.md 2026-07-22 "C. Known-
miller co-author list": the existing coauthor_other_misconduct Tier-A feature
only fires for co-authors of RETRACTED papers; this fires for authors named
in investigative reporting as running/participating in a mill -- whose own
papers haven't necessarily been retracted, or even exist in this graph at all).

Source list: data/known_millers.csv (name, orcid, source_url, note) --
hand-maintained. Currently seeded from one Retraction Watch investigation
(2026-07-21, "Exclusive: Medical student in Nepal behind busy research
factory"), naming Raghabendra Kumar Mahato (network founder, confirmed ORCID
in the article) and Shreya Singh Beniwal (co-administrator, no confirmed
ORCID). Add rows as new documented cases surface.

MATCHING: ORCID-only, same discipline as author_retraction_rate_external.py
(this session, earlier): a name alone is not a safe identifier for a
person-level fact. Confirmed concretely while sourcing this exact list: a
live search for "Shreya Singh Beniwal" surfaced a DIFFERENT, unrelated
"Shreya Singh" (a computer-science academic) with her own distinct ORCID --
exactly the collision risk that makes name-only matching unsafe here.
Millers without a confirmed ORCID stay in the csv for documentation/audit
but are skipped for graph matching entirely, never guessed by name
(plan.md §0).

Measured 2026-07-22: neither miller's ORCID nor name appears anywhere in
this graph -- expected, since this corpus is microbiology-scoped and the
source investigation's output is clinical cardiology/case-report-focused.
This sensor is built as reusable, low-maintenance infrastructure for when
(a) the corpus expands past microbiology, or (b) new documented millers are
added to the csv over time -- not because it currently finds anything.

Fields written (unscored context, same bucket as institution_global_
retraction_count / journal_hijack_flag -- guilt by co-authorship with a
known bad actor is associative, not a hard fact about THIS paper's own
conduct, per plan.md §0 "same person/place != same responsibility"):
  Paper.known_miller_coauthor       : true/false
  Paper.known_miller_coauthor_name  : the miller's name
  Paper.known_miller_source_url     : the article documenting them

Usage:
  python graph_processing/known_miller_coauthor_checker.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

MILLERS_CSV = REPO_ROOT / "data" / "known_millers.csv"


def load_millers() -> tuple[list[dict], list[dict]]:
    """Returns (with_orcid, without_orcid) -- only the first group is ever
    matched against the graph."""
    with_orcid, without_orcid = [], []
    with MILLERS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            (with_orcid if row.get("orcid") else without_orcid).append(row)
    return with_orcid, without_orcid


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    with_orcid, without_orcid = load_millers()
    print(f"[known_miller_coauthor_checker] {len(with_orcid)} miller(s) with a confirmed ORCID "
          f"(matched), {len(without_orcid)} without (documented but NOT matched -- no name-only "
          f"guessing, plan.md §0)", file=sys.stderr)
    if without_orcid:
        for m in without_orcid:
            print(f"  skipping (no ORCID): {m['name']}", file=sys.stderr)

    with driver.session(database=conn["database"]) as s:
        s.run(
            """
            MATCH (p:Paper)
            SET p.known_miller_coauthor = null,
                p.known_miller_coauthor_name = null,
                p.known_miller_source_url = null
            """
        ).consume()

        total_hits = 0
        for m in with_orcid:
            rows = s.run(
                """
                MATCH (a:AuthorInstance {orcid: $orcid})-[:WROTE]->(p:Paper)
                SET p.known_miller_coauthor = true,
                    p.known_miller_coauthor_name = $name,
                    p.known_miller_source_url = $source_url
                RETURN p.doi AS doi
                """,
                orcid=m["orcid"], name=m["name"], source_url=m["source_url"],
            ).data()
            if rows:
                print(f"  {m['name']} ({m['orcid']}): {len(rows)} paper(s) in this graph -- "
                      f"{[r['doi'] for r in rows]}", file=sys.stderr)
            total_hits += len(rows)

        dist = s.run(
            """
            MATCH (p:Paper {is_retracted:false})
            RETURN count(*) AS n, sum(CASE WHEN p.known_miller_coauthor THEN 1 ELSE 0 END) AS with_flag
            """
        ).single()

    driver.close()

    print("\n=== Known-miller co-author checker — verification ===", file=sys.stderr)
    print(f"Total paper-authorship hits across all confirmed-ORCID millers: {total_hits}", file=sys.stderr)
    print(f"Not-yet-retracted candidates with known_miller_coauthor: {dist['with_flag']} / {dist['n']}",
          file=sys.stderr)
    print(
        "\nNOTE: unscored review context, never part of the score -- co-authorship with a documented "
        "miller is associative, not a finding about this specific paper (plan.md §0).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
