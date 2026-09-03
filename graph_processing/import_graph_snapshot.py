#!/usr/bin/env python3
"""
import_graph_snapshot.py — restore a full graph snapshot written by
export_graph_snapshot.py (added 2026-07-21; see that script's docstring for
why this exists). Run this on a fresh Neo4j instance on a new computer
INSTEAD OF the entire build_instances -> apply_overrides -> expand_targets ->
refresh_* -> link_instances -> cluster_instances -> mark_adjudication ->
Phase-4 sensors -> gds_node_classification -> tier_a_scoring chain -- the
snapshot already has every property every one of those stages would have
computed.

Deliberately does NOT use apoc.import.json: that requires
`apoc.import.file.enabled=true` in apoc.conf plus a DBMS restart, and the
file has to live inside the DBMS's own (per-machine, per-DBMS-instance) import
directory. This script instead parses the JSONL itself and writes via the
same bolt driver every other script in this repo already uses -- no Neo4j
config changes, no DBMS-specific paths, fully portable.

Matching is by each label's NATURAL KEY (doi / name / institution_id / code /
instance_id -- the same properties the CREATE CONSTRAINT statements in
import_cypher.txt and build_instances.py already key on), never Neo4j's
internal node id, which is not stable across databases. MERGE + `SET n +=
props` throughout, so it's safe to run against a graph that already has some
data (e.g. a partial rebuild) -- existing properties not in the snapshot are
left alone, everything in the snapshot is overwritten to match.

Creates the same uniqueness constraints backbone loading + build_instances.py
would (idempotent, IF NOT EXISTS) before importing, since a brand-new DBMS
has none yet and MERGE without a constraint is a slow linear scan.

Usage:
  python graph_processing/import_graph_snapshot.py [--file path]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from neo4j import GraphDatabase
from neo4j.time import Date

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

DEFAULT_PATH = REPO_ROOT / "data" / "graph" / "full_graph_snapshot.jsonl"
BATCH = 1000

# Natural key per label -- must match the CREATE CONSTRAINT statements in
# import_cypher.txt (Paper/Journal/Institution/Reason) and build_instances.py
# (AuthorInstance). Never Neo4j's internal node id.
NATURAL_KEY = {
    "Paper": "doi",
    "Journal": "name",
    "Institution": "institution_id",
    "Reason": "code",
    "AuthorInstance": "instance_id",
}

# Properties the live pipeline writes as Neo4j `date()` values (expand_targets.py's
# published_date, refresh_retraction_status.py's retraction_date -- both Paper-only).
# APOC's JSON export serializes a Date as an ISO string, and `SET n += r.props` on a
# plain JSON string leaves it a String, not a Date, on import -- a silent type
# regression nothing today reads temporally (grep-verified), but a structural break
# of "restore exactly what the pipeline would have produced" and a landmine for any
# future Cypher that expects a real Date (BUG.md R3-14). Converted back explicitly
# below, per label, before the write.
DATE_PROPS = {
    "Paper": ("published_date", "retraction_date"),
}

CONSTRAINTS = [
    "CREATE CONSTRAINT paper_doi IF NOT EXISTS FOR (p:Paper) REQUIRE p.doi IS UNIQUE",
    "CREATE CONSTRAINT journal_name IF NOT EXISTS FOR (j:Journal) REQUIRE j.name IS UNIQUE",
    "CREATE CONSTRAINT inst_id IF NOT EXISTS FOR (i:Institution) REQUIRE i.institution_id IS UNIQUE",
    "CREATE CONSTRAINT reason_code IF NOT EXISTS FOR (r:Reason) REQUIRE r.code IS UNIQUE",
    "CREATE CONSTRAINT author_instance_id IF NOT EXISTS FOR (a:AuthorInstance) REQUIRE a.instance_id IS UNIQUE",
]


def node_key(labels: list[str], props: dict) -> tuple[str, str, object] | None:
    """First label (in the snapshot's own label list) with a known natural
    key present in its properties. Returns (label, key_name, key_value)."""
    for label in labels:
        key = NATURAL_KEY.get(label)
        if key and key in props:
            return label, key, props[key]
    return None


def run_batched(session, query: str, rows: list[dict]) -> None:
    for i in range(0, len(rows), BATCH):
        session.run(query, rows=rows[i:i + BATCH])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(DEFAULT_PATH), help="snapshot .jsonl path")
    args = ap.parse_args()
    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"snapshot not found: {path}")

    # Single pass: bucket nodes by label, relationships by (type, start label/key, end label/key).
    nodes_by_label: dict[str, list[dict]] = {}
    rels_by_group: dict[tuple[str, str, str, str, str], list[dict]] = {}
    skipped_nodes = 0
    skipped_rels = 0

    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("type") == "node":
                nk = node_key(rec.get("labels", []), rec.get("properties", {}))
                if not nk:
                    skipped_nodes += 1
                    continue
                label, _key, val = nk
                props = rec.get("properties", {})
                date_props = DATE_PROPS.get(label)
                if date_props:
                    props = dict(props)
                    for prop in date_props:
                        v = props.get(prop)
                        if isinstance(v, str):
                            try:
                                props[prop] = Date.from_iso_format(v)
                            except ValueError:
                                pass  # leave as-is rather than fail the whole import over one bad value
                nodes_by_label.setdefault(label, []).append(
                    {"key": val, "props": props}
                )
            elif rec.get("type") == "relationship":
                start_nk = node_key(rec["start"].get("labels", []), rec["start"].get("properties", {}))
                end_nk = node_key(rec["end"].get("labels", []), rec["end"].get("properties", {}))
                if not start_nk or not end_nk:
                    skipped_rels += 1
                    continue
                sl, sk, sv = start_nk
                el, ek, ev = end_nk
                group = (rec.get("label", "RELATED_TO"), sl, sk, el, ek)
                rels_by_group.setdefault(group, []).append(
                    {"sv": sv, "ev": ev, "props": rec.get("properties") or {}}
                )

    total_nodes = sum(len(v) for v in nodes_by_label.values())
    total_rels = sum(len(v) for v in rels_by_group.values())
    print(f"  parsed: {total_nodes} node(s) across {len(nodes_by_label)} label(s), "
          f"{total_rels} relationship(s) across {len(rels_by_group)} type/endpoint group(s)")
    if skipped_nodes or skipped_rels:
        print(f"  skipped (no known natural key): {skipped_nodes} node(s), {skipped_rels} relationship(s)")

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        for stmt in CONSTRAINTS:
            s.run(stmt)

        for label, rows in nodes_by_label.items():
            key_name = NATURAL_KEY[label]
            run_batched(
                s,
                f"UNWIND $rows AS r MERGE (n:`{label}` {{`{key_name}`: r.key}}) SET n += r.props",
                rows,
            )
            print(f"  {label}: {len(rows)} node(s) written")

        for (rel_type, sl, sk, el, ek), rows in rels_by_group.items():
            run_batched(
                s,
                f"UNWIND $rows AS r "
                f"MATCH (a:`{sl}` {{`{sk}`: r.sv}}), (b:`{el}` {{`{ek}`: r.ev}}) "
                f"MERGE (a)-[rel:`{rel_type}`]->(b) SET rel += r.props",
                rows,
            )
            print(f"  {rel_type} ({sl}->{el}): {len(rows)} relationship(s) written")
    driver.close()

    print(f"\nrestored {total_nodes} node(s), {total_rels} relationship(s) from {path}")


if __name__ == "__main__":
    main()
