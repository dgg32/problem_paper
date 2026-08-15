#!/usr/bin/env python3
"""
export_graph_snapshot.py — full Neo4j graph snapshot for cross-machine
reproducibility (added 2026-07-21, after rebuilding this graph from scratch
on a new computer surfaced how expensive that rebuild really is: the
identity/expansion pipeline alone burns through hundreds of live OpenAlex/
Crossref calls, and the Phase-4 sensor scan + GDS training is another
lengthy, API-bound pass. Re-deriving all of that from source on every new
machine is wasteful when the graph itself -- the actual expensive artifact
-- can just be shipped as a file).

Dumps EVERY node and relationship, with ALL properties, via APOC's streaming
JSON export (`apoc.export.json.all(..., {stream:true})`). Streaming is
deliberate: `apoc.export.file.enabled` is OFF by default (confirmed live --
calling the file-writing form raises "Export to files not enabled"), and
turning it on requires editing apoc.conf + restarting the DBMS. Streaming
sidesteps that entirely -- the JSON never touches the DBMS's filesystem, it
comes back over the same bolt connection every other script already uses, so
this script writes straight into the repo's own `data/graph/` (which the
user already carries between machines by hand, same as `data/graph/*.tsv`
and `.env.yaml` -- see AGENTS.md).

Companion script: `import_graph_snapshot.py` restores this file onto a fresh
Neo4j instance -- MERGE-based on each label's natural key (never Neo4j's
internal node id, which isn't stable across databases), so it's safe to run
against a graph that already has some data too.

This is NOT Phase-4-specific -- it's a full-graph snapshot, so restoring it
skips the ENTIRE pipeline (build_instances -> ... -> mark_adjudication ->
Phase-4 sensors -> gds_node_classification -> tier_a_scoring), not just the
sensor scan. Re-running the pipeline from source remains the way to pick up
new data (new retractions, new OpenAlex records); this snapshot is for
"reproduce what I already have," not "refresh it."

Usage:
  python graph_processing/export_graph_snapshot.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

OUT_PATH = REPO_ROOT / "data" / "graph" / "full_graph_snapshot.jsonl"
META_PATH = REPO_ROOT / "data" / "graph" / "full_graph_snapshot.meta.json"

# Every label/relationship-type in the schema (import_cypher.txt + build_instances.py
# + gds_node_classification.py's transient :LabeledPaper/:CandidatePaper, which carry
# no extra data worth a separate pass -- they're plain Paper nodes).
NODE_LABELS = ["Paper", "Journal", "Institution", "Reason", "AuthorInstance"]
REL_TYPES = [
    "WROTE", "AFFILIATED_WITH", "PROBABLY_SAME_AS",
    "PUBLISHED_IN", "INVOLVES", "RETRACTED_FOR", "CITES",
]

QUERY_TEMPLATE = (
    "CALL apoc.export.json.query($cypher, null, "
    "{stream:true, useTypes:true, jsonFormat:'JSON_LINES'}) "
    "YIELD data RETURN data"
)


def fetch_lines(session, cypher: str) -> list[str]:
    """Runs one apoc.export.json.query call, scoped to a single label/rel-type
    (see module docstring) so no single call has to buffer the whole graph in
    JVM heap at once -- confirmed live 2026-07-21: a single apoc.export.json.
    all() call OOM'd Neo4j Desktop's default 1GB heap once Phase-4's
    sensor-flag JSON properties made the graph's total property payload much
    larger; per-label/per-type calls each stay well under that.

    APOC's own streamed chunk boundaries don't align with JSON-line
    boundaries once a single property value (e.g. a large flag-JSON blob) is
    bigger than one chunk -- splitting each chunk independently breaks
    mid-string (confirmed live: 'Unterminated string' JSON errors). Safe fix:
    concatenate every chunk for this one (already-scoped, already-small)
    query client-side in Python before splitting into lines -- the JVM-side
    scoping above is what avoids the OOM, not avoiding a client-side string.

    Split on a literal '\\n' ONLY -- NOT str.splitlines(), which also breaks
    on U+2028/U+2029 and other Unicode line-boundary characters. Confirmed
    live: a retracted paper's own title contains an embedded U+2028 (LINE
    SEPARATOR) -- perfectly valid, unescaped content inside a JSON string
    (JSON only requires escaping '\"', '\\\\', and control chars U+0000-
    U+001F; U+2028 isn't one), but splitlines() fragmented that record's
    JSON mid-string anyway."""
    result = session.run(QUERY_TEMPLATE, cypher=cypher)
    full_text = "".join(record["data"] for record in result)
    return [line for line in full_text.split("\n") if line.strip()]


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with driver.session(database=conn["database"]) as s, OUT_PATH.open("w") as f:
        for label in NODE_LABELS:
            print(f"  exporting :{label} nodes...", file=sys.stderr)
            lines = fetch_lines(s, f"MATCH (n:`{label}`) RETURN n")
            for line in lines:
                # apoc.export.json.query wraps each row by RETURN alias
                # ("n"/"r") -- unwrap so lines match .all()'s own unwrapped
                # {"type":"node"/"relationship", ...} shape, which
                # import_graph_snapshot.py expects.
                obj = json.loads(line)
                f.write(json.dumps(obj["n"]) + "\n")
            print(f"    {len(lines)} node(s)", file=sys.stderr)
        for rel_type in REL_TYPES:
            print(f"  exporting :{rel_type} relationships...", file=sys.stderr)
            lines = fetch_lines(s, f"MATCH (a)-[r:`{rel_type}`]->(b) RETURN r")
            for line in lines:
                obj = json.loads(line)
                f.write(json.dumps(obj["r"]) + "\n")
            print(f"    {len(lines)} relationship(s)", file=sys.stderr)
    driver.close()

    # Don't trust apoc's yielded nodes/relationships/properties counters for
    # reporting -- observed inconsistent chunking between .all() and .graph()
    # during testing (sometimes one row per entity, sometimes the whole
    # export in one row). Count from the file we actually wrote instead.
    labels: dict[str, int] = {}
    rel_types: dict[str, int] = {}
    with OUT_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("type") == "node":
                for label in rec.get("labels", ["(none)"]):
                    labels[label] = labels.get(label, 0) + 1
            elif rec.get("type") == "relationship":
                rt = rec.get("label", "(none)")
                rel_types[rt] = rel_types.get(rt, 0) + 1

    n_nodes = sum(labels.values())
    n_rels = sum(rel_types.values())
    meta = {
        "exported_date": str(date.today()),
        "nodes_by_label": labels,
        "relationships_by_type": rel_types,
        "total_nodes": n_nodes,
        "total_relationships": n_rels,
        "file_size_bytes": OUT_PATH.stat().st_size,
    }
    META_PATH.write_text(json.dumps(meta, indent=2))

    print(f"wrote {OUT_PATH}  ({OUT_PATH.stat().st_size / 1e6:.1f} MB)")
    print(f"wrote {META_PATH}")
    print(f"\n  nodes         : {n_nodes}")
    for label, n in sorted(labels.items(), key=lambda x: -x[1]):
        print(f"    {label:<20} {n}")
    print(f"  relationships : {n_rels}")
    for rt, n in sorted(rel_types.items(), key=lambda x: -x[1]):
        print(f"    {rt:<20} {n}")
    print("\n  restore with: python graph_processing/import_graph_snapshot.py")


if __name__ == "__main__":
    main()
