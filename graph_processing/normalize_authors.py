#!/usr/bin/env python3
"""
normalize_authors.py — Phase 2 author identity resolution (CANDIDATE stage).

Sits between "Enrich" and "Node property prediction" in plan.md §4 Phase 2.
It finds Author nodes that MAY be the same real person and records the guess as
a reversible ``SAME_AS`` link — it NEVER merges nodes. That keeps identity
resolution auditable and undo-able, which plan §0/§7 require (a wrong merge can
attribute one person's retraction to another — a defamation risk).

Blocking key (candidate generation)
------------------------------------
Two Author nodes are a candidate pair when they share:
  * the same FIRST and LAST name token (looser than the full-name node key), and
  * at least one Institution (ROR-backed AFFILIATED_WITH edge).

Classification (per pair)
-------------------------
ORCID is the hard gate; a shared co-author is the precision booster:

  REJECTED  both sides carry ORCIDs and they DIFFER  -> provably different
            people; never linked. (Measured: ~1/3 of raw candidates.)
  HIGH      exactly one side is ORCID-identified, the other is name-only, AND
            they share >=1 co-author OR have an identical full name.
  MEDIUM    one side ORCID-identified, other name-only, no corroboration.
  LOW       neither side has an ORCID (weakest identity — review only).

(Pairs where both sides share the same ORCID cannot occur: such authors are
already the same node, since author_id == orcid when present.)

Accepted pairs are written as, in author_id-ascending direction:
  (a)-[:SAME_AS {confidence, score, shared_institutions, shared_coauthors,
                 same_full_name, rule, created}]->(b)

Nothing is merged. Review the edges / the TSV report, then a SEPARATE, later
step may collapse high-confidence links for feature computation.

Usage
-----
  python graph_processing/normalize_authors.py                 # write + report
  python graph_processing/normalize_authors.py --dry-run       # report only
  python graph_processing/normalize_authors.py --min-tier HIGH # write HIGH only
  python graph_processing/normalize_authors.py --clear         # drop SAME_AS first

Connection is resolved from env (NEO4J_URI/USERNAME/PASSWORD/DATABASE), falling
back to the neo4j block in ./.mcp.json.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parent.parent
REPORT_TSV = ROOT / "data" / "graph" / "author_same_as_candidates.tsv"
REPORT_JSON = ROOT / "data" / "graph" / "author_same_as_candidates.json"

# Tier -> base numeric score (for sortable ranking; corroboration adds a bump).
TIER_SCORE = {"HIGH": 0.85, "MEDIUM": 0.60, "LOW": 0.40}
TIER_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}  # for --min-tier filtering


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
def resolve_connection() -> dict:
    """Prefer env vars; fall back to the neo4j server block in .mcp.json."""
    cfg = {
        "uri": os.getenv("NEO4J_URI"),
        "user": os.getenv("NEO4J_USERNAME"),
        "password": os.getenv("NEO4J_PASSWORD"),
        "database": os.getenv("NEO4J_DATABASE"),
    }
    if not all([cfg["uri"], cfg["user"], cfg["password"]]):
        mcp = ROOT / ".mcp.json"
        if mcp.exists():
            env = (
                json.loads(mcp.read_text())
                .get("mcpServers", {})
                .get("neo4j", {})
                .get("env", {})
            )
            cfg["uri"] = cfg["uri"] or env.get("NEO4J_URI")
            cfg["user"] = cfg["user"] or env.get("NEO4J_USERNAME")
            cfg["password"] = cfg["password"] or env.get("NEO4J_PASSWORD")
            cfg["database"] = cfg["database"] or env.get("NEO4J_DATABASE")
    cfg["database"] = cfg["database"] or "neo4j"
    missing = [k for k in ("uri", "user", "password") if not cfg[k]]
    if missing:
        raise SystemExit(f"Missing Neo4j connection settings: {', '.join(missing)}")
    return cfg


# --------------------------------------------------------------------------- #
# Candidate generation (blocking key + evidence, in one query)
# --------------------------------------------------------------------------- #
CANDIDATE_QUERY = """
MATCH (a1:Author)-[:AFFILIATED_WITH]->(i:Institution)<-[:AFFILIATED_WITH]-(a2:Author)
WHERE a1.author_id < a2.author_id
  AND size(split(a1.name_key,' ')) >= 2
  AND size(split(a2.name_key,' ')) >= 2
  AND split(a1.name_key,' ')[0]  = split(a2.name_key,' ')[0]
  AND split(a1.name_key,' ')[-1] = split(a2.name_key,' ')[-1]
WITH a1, a2, collect(DISTINCT i.name) AS shared_insts
// co-authors of each side (excluding the pair itself)
OPTIONAL MATCH (a1)-[:WROTE]->(:Paper)<-[:WROTE]-(c1:Author)
WHERE c1 <> a1 AND c1 <> a2
WITH a1, a2, shared_insts, collect(DISTINCT c1.author_id) AS co1
OPTIONAL MATCH (a2)-[:WROTE]->(:Paper)<-[:WROTE]-(c2:Author)
WHERE c2 <> a1 AND c2 <> a2
WITH a1, a2, shared_insts, co1, collect(DISTINCT c2.author_id) AS co2
RETURN a1.author_id AS id1, a1.name AS name1, a1.orcid AS orcid1, a1.name_key AS key1,
       a2.author_id AS id2, a2.name AS name2, a2.orcid AS orcid2, a2.name_key AS key2,
       shared_insts,
       size([x IN co1 WHERE x IN co2]) AS shared_coauthors
ORDER BY id1, id2
"""

WRITE_QUERY = """
MATCH (a:Author {author_id:$id1}), (b:Author {author_id:$id2})
MERGE (a)-[r:SAME_AS]->(b)
SET r.confidence         = $tier,
    r.score              = $score,
    r.shared_institutions = $insts,
    r.shared_coauthors   = $shared_coauthors,
    r.same_full_name     = $same_full_name,
    r.rule               = 'first+last+institution',
    r.created            = datetime()
"""


def classify(pair: dict) -> dict:
    """Assign a tier + score to a candidate pair. Returns pair augmented in place."""
    o1, o2 = pair["orcid1"], pair["orcid2"]
    same_full_name = pair["key1"] == pair["key2"]
    shared_co = pair["shared_coauthors"] or 0

    if o1 and o2:
        # both identified, and different (same-ORCID pairs can't reach here)
        tier = "REJECTED"
    elif o1 or o2:
        # one identified, one name-only
        tier = "HIGH" if (shared_co > 0 or same_full_name) else "MEDIUM"
    else:
        tier = "LOW"

    score = TIER_SCORE.get(tier, 0.0)
    if tier != "REJECTED":
        score = min(0.99, score + 0.02 * min(shared_co, 5) + (0.03 if same_full_name else 0))

    pair["same_full_name"] = same_full_name
    pair["tier"] = tier
    pair["score"] = round(score, 3)
    return pair


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="generate the report but write no SAME_AS edges")
    ap.add_argument("--min-tier", choices=["LOW", "MEDIUM", "HIGH"], default="LOW",
                    help="lowest tier to WRITE as an edge (report always lists all)")
    ap.add_argument("--clear", action="store_true",
                    help="delete all existing :SAME_AS edges before writing")
    args = ap.parse_args()

    cfg = resolve_connection()
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    written = 0
    try:
        with driver.session(database=cfg["database"]) as session:
            rows = [dict(r) for r in session.run(CANDIDATE_QUERY)]
            pairs = [classify(p) for p in rows]

            if args.clear and not args.dry_run:
                session.run("MATCH ()-[r:SAME_AS]->() DELETE r")

            for p in pairs:
                if p["tier"] == "REJECTED":
                    continue
                if TIER_ORDER[p["tier"]] < TIER_ORDER[args.min_tier]:
                    continue
                if args.dry_run:
                    continue
                session.run(
                    WRITE_QUERY,
                    id1=p["id1"], id2=p["id2"], tier=p["tier"], score=p["score"],
                    insts=p["shared_insts"], shared_coauthors=p["shared_coauthors"],
                    same_full_name=p["same_full_name"],
                )
                written += 1
    finally:
        driver.close()

    write_report(pairs)
    print_summary(pairs, written, args)


def write_report(pairs: list[dict]) -> None:
    REPORT_TSV.parent.mkdir(parents=True, exist_ok=True)
    order = {"REJECTED": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    ranked = sorted(pairs, key=lambda p: (order[p["tier"]], -p["score"]))
    cols = ["tier", "score", "name1", "orcid1", "name2", "orcid2",
            "same_full_name", "shared_coauthors", "shared_institutions",
            "id1", "id2"]
    lines = ["\t".join(cols)]
    for p in ranked:
        lines.append("\t".join([
            p["tier"], f"{p['score']:.3f}",
            p["name1"] or "", p["orcid1"] or "",
            p["name2"] or "", p["orcid2"] or "",
            "yes" if p["same_full_name"] else "no",
            str(p["shared_coauthors"] or 0),
            " | ".join(p["shared_insts"] or []),
            p["id1"], p["id2"],
        ]))
    REPORT_TSV.write_text("\n".join(lines) + "\n")
    REPORT_JSON.write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).isoformat(), "pairs": ranked},
        indent=2, ensure_ascii=False))


def print_summary(pairs: list[dict], written: int, args) -> None:
    counts: dict[str, int] = {}
    for p in pairs:
        counts[p["tier"]] = counts.get(p["tier"], 0) + 1
    print("Author SAME_AS candidate generation")
    print("  blocking key : first+last name token AND a shared institution")
    print(f"  candidate pairs: {len(pairs)}")
    for tier in ("HIGH", "MEDIUM", "LOW", "REJECTED"):
        if tier in counts:
            note = "  (never linked — conflicting ORCIDs)" if tier == "REJECTED" else ""
            print(f"    {tier:9}: {counts[tier]:4}{note}")
    mode = "DRY-RUN (no edges written)" if args.dry_run \
        else f"wrote {written} SAME_AS edges (min-tier={args.min_tier})"
    print(f"  {mode}")
    print(f"  report: {REPORT_TSV.relative_to(ROOT)}")
    print(f"          {REPORT_JSON.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
