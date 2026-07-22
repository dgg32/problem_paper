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

ALSO computes global_retraction_count (added 2026-07-22, prompted by the same
publisher/country external-rate discussion in skill_worth_exploring.md): how
many retractions in the FULL retraction_watch.csv (71,106 records, all
subjects -- NOT filtered to data.seed_subset) name this institution, at all.

IMPORTANT, checked empirically 2026-07-22 (do not assume otherwise):
`Institution.name` in this graph is NOT the csv's raw Institution-column text
-- it is OpenAlex's canonical institution name (build_instances.py /
expand_targets.py build Institution nodes from each author's OpenAlex
`institutions` list; extract_enrich.py's own `csv_institutions` field, the
csv's raw per-paper text, is captured but never used to create Institution
nodes/INVOLVES edges -- see extract_enrich.py's module docstring: "institution
identities come from OpenAlex/ORCID canonical entities"). The csv's own
Institution column, by contrast, is a free-text "Department, Institution,
City, CountryCode" string per author (e.g. "Stroke Medicine, University
Hospital Coventry and Warwickshire NHS Trust, Coventry, GBR"), so an exact
match against our clean OpenAlex name only hits when a canonical university
name happens to appear verbatim inside that longer string -- true for large,
simply-named universities (confirmed hits: Harvard University, Cornell
University, University of Michigan, ...) but NOT for hospitals/departments/
institutes whose csv text differs from their OpenAlex canonical form (e.g.
OpenAlex's "University Hospital Coventry" vs the csv's "University Hospital
Coventry and Warwickshire NHS Trust" -- same real institution, different
string, correctly does NOT match here). This is the SAME name-matching
problem publisher_retraction_rate.py solves with substring/alias matching,
just at a scale (133,025 distinct csv institution strings vs ~1,700 for
publishers) that isn't worth hand-tuning right now -- so this stays a
deliberately CONSERVATIVE exact-match-only count: real matches are always
correct (no false positives), coverage is genuinely partial (confirmed
2026-07-22: only 130/2992 graph institutions get a nonzero count, even
though 1,993 of them are provably tied to an actual RW-seeded retraction and
so DO appear in the csv under some string). A count of 0 means "no exact
string match found," never "verified zero retractions" -- same "missing !=
zero" rule as publisher/country. This is a raw COUNT, not a rate (there is no
reliable external "total papers by this institution" denominator to divide
by), so it is stored as unscored context only -- surfaced next to
institution_retr_rate/paper_count in build_review_page.py and
flag_evidence_report.py, never wired into tier_a_scoring.py's WEIGHTS. A
big, prolific institution ("Harvard University") will have a huge count for
reasons having nothing to do with risk; the point is comparative context
("this institution has 3 retractions ever recorded, globally" vs "400"), not
a normalized signal.

Usage:
  python graph_processing/institution_retraction_rate.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

CONFIG_PATH = REPO_ROOT / ".env.yaml"
cfg = yaml.safe_load(CONFIG_PATH.read_text())
RW_CSV_PATH = (REPO_ROOT / cfg["data"]["retraction_watch_csv"]).resolve()


def load_global_institution_counts() -> dict[str, int]:
    """{institution name (as it appears verbatim in the csv): count of
    retraction records naming it}, from the FULL csv -- deliberately NOT
    filtered to data.seed_subset, same reasoning as publisher/country."""
    counts: dict[str, int] = {}
    with RW_CSV_PATH.open(encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            for name in (row.get("Institution", "") or "").split(";"):
                name = name.strip()
                if name:
                    counts[name] = counts.get(name, 0) + 1
    return counts


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    print("[institution_retraction_rate] loading full retraction_watch.csv (unfiltered)...", file=sys.stderr)
    global_counts = load_global_institution_counts()
    print(f"  {sum(global_counts.values())} retraction-institution records across {len(global_counts)} distinct institution strings",
          file=sys.stderr)

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

        print("[institution_retraction_rate] global_retraction_count (full RW csv, exact name match)...", file=sys.stderr)
        institution_names = [r["name"] for r in s.run("MATCH (i:Institution) RETURN i.name AS name")]
        unmatched = 0
        for name in institution_names:
            count = global_counts.get(name, 0)
            if count == 0:
                unmatched += 1
            s.run(
                "MATCH (i:Institution {name: $name}) SET i.global_retraction_count = $count",
                name=name, count=count,
            )
        if unmatched:
            print(f"  {unmatched}/{len(institution_names)} institutions had NO exact match in the full csv "
                  f"(left at 0 -- see module docstring; should be rare since this graph's Institution.name "
                  f"values come directly from the same csv column)", file=sys.stderr)

        print("[institution_retraction_rate] per-paper max across involved institutions...", file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)
            SET p.institution_retr_rate = 0.0,
                p.institution_retr_rate_name = null,
                p.institution_retr_rate_n = null,
                p.institution_global_retraction_count = null,
                p.institution_global_retraction_count_name = null
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
        s.run(
            """
            MATCH (p:Paper)-[:INVOLVES]->(i:Institution)
            WHERE i.global_retraction_count > 0
            WITH p, i ORDER BY i.global_retraction_count DESC
            WITH p, collect({name: i.name, count: i.global_retraction_count})[0] AS top
            SET p.institution_global_retraction_count = top.count,
                p.institution_global_retraction_count_name = top.name
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
                   avg(p.institution_retr_rate) AS avg_rate,
                   sum(CASE WHEN p.institution_global_retraction_count > 0 THEN 1 ELSE 0 END) AS with_global_count
            """
        ).single()

        top_global = s.run(
            """
            MATCH (i:Institution) WHERE i.global_retraction_count > 0
            RETURN i.name AS name, i.country AS country, i.global_retraction_count AS count
            ORDER BY count DESC LIMIT 15
            """
        ).data()

    driver.close()

    print("\n=== Institution retraction rate — verification ===", file=sys.stderr)
    print(f"Not-yet-retracted candidates with a nonzero institution_retr_rate: "
          f"{dist['with_signal']} / {dist['n']}  (avg {dist['avg_rate']:.3f})", file=sys.stderr)
    print(f"Not-yet-retracted candidates with a nonzero institution_global_retraction_count: "
          f"{dist['with_global_count']} / {dist['n']}", file=sys.stderr)
    print("\nTop institutions by retraction rate (paper_count >= 3, so not single-paper noise):", file=sys.stderr)
    for row in top_institutions:
        print(f"  {row['rate']:.1%}  n={row['n']:<4} {row['name']} ({row['country'] or '?'})", file=sys.stderr)
    print("\nTop institutions by GLOBAL retraction count (full RW csv, all subjects, unscored context):", file=sys.stderr)
    for row in top_global:
        print(f"  {row['count']:<5} {row['name']} ({row['country'] or '?'})", file=sys.stderr)


if __name__ == "__main__":
    main()
