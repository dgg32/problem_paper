#!/usr/bin/env python3
"""
build_instances.py — Step 1 of the instance-based author identity layer.

Replaces the merged `Author` nodes with one `AuthorInstance` per AUTHORSHIP
(one author slot on one paper). Nothing is merged — not even by ORCID. Identity
becomes a separate, reversible layer of PROBABLY_SAME_AS edges (Step 2).

Source of truth: data/graph/graph.json (per-authorship detail from OpenAlex;
names + byline affiliations independently confirmed against PubMed). ORCID is
kept as-assigned but treated as a low-trust signal downstream.

Paper / Journal / Institution / Reason and their edges are left untouched.

Model built here:
  (:AuthorInstance {instance_id, name, name_key, orcid?, has_orcid, orcid_source})
  (AuthorInstance)-[:WROTE {author_position, is_corresponding, affiliation}]->(Paper)
  (AuthorInstance)-[:AFFILIATED_WITH]->(Institution)   # per-authorship byline

Run:  python graph_processing/build_instances.py
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_authors import resolve_connection  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_JSON = REPO_ROOT / "data" / "graph" / "graph.json"
BATCH = 2000


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def name_key(name: str) -> str:
    """Same normalization as extract_enrich.py (weak identity for blocking)."""
    if not name:
        return ""
    s = strip_accents(name).lower()
    s = s.replace("‐", " ").replace("-", " ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_rows(records: list[dict], valid_dois: set[str]):
    """Return (instance_nodes, wrote_edges, affiliated_edges)."""
    # graph.json has duplicate records per DOI (1631 records -> 1554 papers);
    # keep one per DOI, the one with the fullest author list.
    best: dict[str, dict] = {}
    for rec in records:
        doi = rec["doi"]
        if doi not in valid_dois:
            continue
        if doi not in best or len(rec.get("authors", [])) > len(best[doi].get("authors", [])):
            best[doi] = rec

    nodes, wrote, affiliated = [], [], []
    for doi, rec in best.items():
        for i, a in enumerate(rec.get("authors", [])):
            iid = f"{doi}::{i}"
            orcid = a.get("orcid") or None
            nodes.append({
                "instance_id": iid,
                "name": a.get("name") or "",
                "name_key": name_key(a.get("name") or ""),
                "orcid": orcid,
                "has_orcid": bool(orcid),
                "orcid_source": a.get("orcid_source") or None,
            })
            wrote.append({
                "iid": iid, "doi": doi,
                "props": {
                    "author_position": a.get("position") or None,
                    "is_corresponding": bool(a.get("is_corresponding")),
                    "affiliation": a.get("affiliation") or None,
                },
            })
            for inst in a.get("institutions", []):
                if inst.get("id"):
                    affiliated.append({"iid": iid, "inst": inst["id"]})
    return nodes, wrote, affiliated


def batched(seq, n=BATCH):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main() -> None:
    records = json.loads(GRAPH_JSON.read_text())
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    with driver.session(database=conn["database"]) as s:
        valid_dois = {r["doi"] for r in s.run("MATCH (p:Paper) RETURN p.doi AS doi")}
        nodes, wrote, affiliated = build_rows(records, valid_dois)
        print(f"built {len(nodes)} instances, {len(wrote)} WROTE, "
              f"{len(affiliated)} AFFILIATED_WITH (from {len(records)} records)")

        # 1. drop the old merged Author layer + any prior AuthorInstance (idempotent)
        s.run("MATCH (a:Author) DETACH DELETE a")
        s.run("MATCH (a:AuthorInstance) DETACH DELETE a")
        # 2. fresh AuthorInstance layer
        s.run("CREATE CONSTRAINT author_instance_id IF NOT EXISTS "
              "FOR (a:AuthorInstance) REQUIRE a.instance_id IS UNIQUE")

        for chunk in batched(nodes):
            s.run("UNWIND $rows AS r CREATE (a:AuthorInstance) SET a += r", rows=chunk)
        for chunk in batched(wrote):
            s.run(
                "UNWIND $rows AS r "
                "MATCH (a:AuthorInstance {instance_id:r.iid}), (p:Paper {doi:r.doi}) "
                "CREATE (a)-[w:WROTE]->(p) SET w += r.props",
                rows=chunk)
        for chunk in batched(affiliated):
            s.run(
                "UNWIND $rows AS r "
                "MATCH (a:AuthorInstance {instance_id:r.iid}), "
                "      (i:Institution {institution_id:r.inst}) "
                "CREATE (a)-[:AFFILIATED_WITH]->(i)",
                rows=chunk)

        verify(s)
    driver.close()


def verify(s) -> None:
    print("\n=== verification ===")
    for label, q in [
        ("AuthorInstance nodes", "MATCH (a:AuthorInstance) RETURN count(*) AS n"),
        ("  with ORCID", "MATCH (a:AuthorInstance {has_orcid:true}) RETURN count(*) AS n"),
        ("WROTE edges", "MATCH (:AuthorInstance)-[w:WROTE]->(:Paper) RETURN count(w) AS n"),
        ("AFFILIATED_WITH", "MATCH (:AuthorInstance)-[r:AFFILIATED_WITH]->(:Institution) RETURN count(r) AS n"),
        ("orphan instances (no WROTE)",
         "MATCH (a:AuthorInstance) WHERE NOT (a)-[:WROTE]->() RETURN count(*) AS n"),
        ("leftover :Author nodes", "MATCH (a:Author) RETURN count(*) AS n"),
    ]:
        print(f"  {label:32}: {s.run(q).single()['n']}")

    print("\n  Bing Liu instances (were the mis-assignment case):")
    for r in s.run(
        "MATCH (a:AuthorInstance {name_key:'bing liu'})-[:WROTE]->(p:Paper) "
        "RETURN a.instance_id AS iid, a.orcid AS orcid, p.doi AS doi ORDER BY iid"):
        print(f"    {r['iid']:<28} orcid={r['orcid'] or '-':<21} doi={r['doi']}")

    n = s.run("MATCH (a:AuthorInstance {name_key:'florence fenollar'}) "
              "RETURN count(*) AS n").single()["n"]
    print(f"\n  Florence Fenollar authorship instances: {n} (was 1 merged node + fragments)")


if __name__ == "__main__":
    main()
