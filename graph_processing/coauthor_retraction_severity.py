#!/usr/bin/env python3
"""
coauthor_retraction_severity.py — Tier-A graph feature: MIDDLE co-authors'
(fuzzy-cluster-matched) retracted-work history, split by severity.

MERGED 2026-07-22 (user request) with what used to be author_retraction_
rate_external.py's first/last-only external rate, into one "did any of this
paper's authors co-author other retracted work, and was it misconduct?"
model. This script is the graph-internal, fuzzy-cluster-matched, MIDDLE-
author half; author_retraction_rate_external.py is the ORCID-strict,
first/last-only, EXTERNAL half -- kept as two separate sensors because they
use fundamentally different matching disciplines (see that script's
docstring for why first/last gets the stricter, more expensive treatment and
middle authors don't).

This REPLACES the old coauthor_other_misconduct field for TIER-A SCORING
PURPOSES, but does not touch or remove coauthor_other_misconduct itself --
that property is still computed by gds_node_classification.py and used
there as a Tier-B model FEATURE (see FEATURE_PROPS in that file); changing
or removing it would silently alter the trained model's inputs. This script
adds two NEW, more granular properties instead:
  - mid_any_count: distinct middle-position "probable person" clusters
    among this paper's OWN authors who wrote some OTHER paper (mp != p)
    that was retracted for a NON-misconduct reason (and not ALSO for a
    misconduct reason -- see below).
  - mid_misconduct_count: same, but for a MISCONDUCT-coded reason
    (MISCONDUCT_REASONS, same list used everywhere else in this pipeline).
Mutually exclusive per cluster: a co-author with BOTH a misconduct-reason
and a non-misconduct-reason retraction elsewhere counts ONLY in
mid_misconduct_count, never double-counted in both buckets.

SAME "leakage-aware" cluster-matching discipline as the original
coauthor_other_misconduct query (probable-person clustering via
AuthorInstance.cluster_id, not strict ORCID -- weaker match confidence than
author_retraction_rate_external.py's ORCID path, which is exactly why
middle co-authors are scored at half the per-position weight of a first/last
author; see config/weights.yaml's mid_*/fl_* minmax targets).

VOLUME (2026-08-16, user request, same fix as author_retraction_rate_
external.py's fl_any_volume/fl_misconduct_volume -- see that module's
docstring for the full "why" and the live severity example that motivated
it). mid_any_count/mid_misconduct_count above only ask "does this cluster
have ANY other qualifying retraction" (0/1 per cluster) -- a middle co-author
with 1 other retracted paper and one with 200+ contribute identically.
mid_any_volume/mid_misconduct_volume restore that severity signal as an
ADDITIVE, minmax-scaled layer (same corpus-worst-anchored mechanism as the
entity-rate signals, so a single extreme cluster cannot blow up the score the
way a flat per-paper weight would) -- checked live 2026-08-16: the worst
middle-position cluster in this corpus has 227 other retracted papers, shared
across multiple candidate papers where it appears as a middle co-author.
SUM across a paper's qualifying clusters (not max), matching mid_any_count's
existing convention of summing cluster presence rather than keeping only the
worst one.

Idempotent: recomputes all four Paper properties from scratch every run.

Usage:
  python graph_processing/coauthor_retraction_severity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

# Same list as author_retraction_rate_external.py / gds_node_classification.py
# -- kept as a local copy (established convention in this pipeline) but MUST
# stay in sync with both.
MISCONDUCT_REASONS = [
    "Misconduct - Official Investigation(s) and/or Finding(s)",
    "Investigation by ORI",
    "Paper Mill",
    "Falsification/Fabrication of Data",
    "Falsification/Fabrication of Image",
    "Falsification/Fabrication of Results",
    "Manipulation of Images",
    "Manipulation of Results",
    "Euphemisms for Misconduct",
    "Misconduct by Author",
]


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    with driver.session(database=conn["database"]) as s:
        print("[coauthor_retraction_severity] resetting mid_any_count/mid_misconduct_count/"
              "mid_any_volume/mid_misconduct_volume...", file=sys.stderr)
        s.run("MATCH (p:Paper) SET p.mid_any_count = 0, p.mid_misconduct_count = 0, "
              "p.mid_any_volume = 0, p.mid_misconduct_volume = 0").consume()

        print("[coauthor_retraction_severity] mid_misconduct_count (middle co-authors, misconduct-reason)...",
              file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)<-[w:WROTE {author_position: 'middle'}]-(a:AuthorInstance)
            WITH p, collect(DISTINCT a.cluster_id) AS clusters
            UNWIND clusters AS cid
            OPTIONAL MATCH (m:AuthorInstance {cluster_id: cid})-[:WROTE]->(mp:Paper)-[:RETRACTED_FOR]->(r:Reason)
            WHERE mp <> p AND r.code IN $reasons
            WITH p, cid, count(mp) AS mpaper_count
            WITH p, sum(CASE WHEN mpaper_count > 0 THEN 1 ELSE 0 END) AS c
            SET p.mid_misconduct_count = c
            """,
            reasons=MISCONDUCT_REASONS,
        ).consume()

        print("[coauthor_retraction_severity] mid_any_count (middle co-authors, any OTHER reason)...",
              file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)<-[w:WROTE {author_position: 'middle'}]-(a:AuthorInstance)
            WITH p, collect(DISTINCT a.cluster_id) AS clusters
            UNWIND clusters AS cid
            OPTIONAL MATCH (m:AuthorInstance {cluster_id: cid})-[:WROTE]->(mp:Paper)-[:RETRACTED_FOR]->(r:Reason)
            WHERE mp <> p AND NOT r.code IN $reasons
            WITH p, cid, count(mp) AS mpaper_count
            WITH p, sum(CASE WHEN mpaper_count > 0 THEN 1 ELSE 0 END) AS c
            SET p.mid_any_count = c
            """,
            reasons=MISCONDUCT_REASONS,
        ).consume()

        # A cluster can have BOTH a misconduct-reason and a non-misconduct-
        # reason retracted paper elsewhere -- it must count only once, in
        # mid_misconduct_count (the more severe bucket). The two queries
        # above independently count "any misconduct-reason paper" and "any
        # NON-misconduct-reason paper" per cluster, so a cluster with both
        # is currently double-counted (once in each). Fix up: subtract the
        # overlap from mid_any_count.
        print("[coauthor_retraction_severity] de-duplicating clusters counted in both buckets...", file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)<-[w:WROTE {author_position: 'middle'}]-(a:AuthorInstance)
            WITH p, collect(DISTINCT a.cluster_id) AS clusters
            UNWIND clusters AS cid
            OPTIONAL MATCH (m:AuthorInstance {cluster_id: cid})-[:WROTE]->(mp:Paper)-[:RETRACTED_FOR]->(r:Reason)
            WHERE mp <> p
            WITH p, cid, collect(DISTINCT r.code) AS codes
            WITH p, sum(CASE WHEN any(c IN codes WHERE c IN $reasons)
                             AND any(c IN codes WHERE NOT c IN $reasons)
                        THEN 1 ELSE 0 END) AS overlap
            WHERE overlap > 0
            SET p.mid_any_count = p.mid_any_count - overlap
            """,
            reasons=MISCONDUCT_REASONS,
        ).consume()

        # mid_any_volume/mid_misconduct_volume: SUM (not cluster-count) of each
        # qualifying cluster's own other-retracted-papers count -- see module
        # docstring "VOLUME" note. Classifies each retracted paper mp (grouped
        # DISTINCT per cluster first, so a paper carrying multiple Reason nodes
        # is never double-counted) as misconduct if it has ANY misconduct-coded
        # reason, else any-other -- same "misconduct wins" exclusivity as the
        # mid_misconduct_count/mid_any_count overlap-dedup above, but computed
        # directly instead of via post-hoc subtraction. Verified live 2026-08-16:
        # zero mismatches between (mc_vol>0)/(any_vol>0) and the existing
        # mid_misconduct_count/mid_any_count booleans across the full corpus.
        print("[coauthor_retraction_severity] mid_any_volume/mid_misconduct_volume "
              "(SUM of underlying retraction counts, minmax-scored)...", file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)<-[w:WROTE {author_position: 'middle'}]-(a:AuthorInstance)
            WITH p, collect(DISTINCT a.cluster_id) AS clusters
            UNWIND clusters AS cid
            OPTIONAL MATCH (m:AuthorInstance {cluster_id: cid})-[:WROTE]->(mp:Paper)-[:RETRACTED_FOR]->(r:Reason)
            WHERE mp <> p
            WITH p, cid, mp, collect(DISTINCT r.code) AS codes
            WITH p, cid,
                 sum(CASE WHEN mp IS NOT NULL AND any(c IN codes WHERE c IN $reasons) THEN 1 ELSE 0 END) AS mc_n,
                 sum(CASE WHEN mp IS NOT NULL AND NOT any(c IN codes WHERE c IN $reasons) THEN 1 ELSE 0 END) AS any_n
            WITH p, sum(mc_n) AS mc_vol, sum(CASE WHEN mc_n = 0 THEN any_n ELSE 0 END) AS any_vol
            SET p.mid_misconduct_volume = mc_vol, p.mid_any_volume = any_vol
            """,
            reasons=MISCONDUCT_REASONS,
        ).consume()

        dist = s.run(
            """
            MATCH (p:Paper {is_retracted:false})
            RETURN count(*) AS n,
                   sum(CASE WHEN p.mid_any_count > 0 THEN 1 ELSE 0 END) AS with_any,
                   sum(CASE WHEN p.mid_misconduct_count > 0 THEN 1 ELSE 0 END) AS with_misconduct,
                   max(p.mid_any_count) AS max_any,
                   max(p.mid_misconduct_count) AS max_misconduct,
                   max(p.mid_any_volume) AS max_any_volume,
                   max(p.mid_misconduct_volume) AS max_misconduct_volume
            """
        ).single()

    driver.close()

    print("\n=== Co-author retraction severity (middle authors, fuzzy cluster) — verification ===", file=sys.stderr)
    print(f"Not-yet-retracted candidates: {dist['with_any']} any-reason, "
          f"{dist['with_misconduct']} misconduct-reason (of {dist['n']})", file=sys.stderr)
    print(f"Max mid_any_count in corpus: {dist['max_any']}  |  max mid_misconduct_count: {dist['max_misconduct']}",
          file=sys.stderr)
    print(f"Max mid_any_volume in corpus: {dist['max_any_volume']}  |  "
          f"max mid_misconduct_volume: {dist['max_misconduct_volume']}  "
          f"(minmax-scored, see tier_a_scoring.py)", file=sys.stderr)


if __name__ == "__main__":
    main()
