#!/usr/bin/env python3
"""
mark_adjudication.py — Step 4 of the instance-based author identity layer.

Re-expresses the `adjudicated` fact at the right grain (plan §0/§3). Two levels:

  * PER-INSTANCE FACT (`on_misconduct_paper`): this authorship is on a paper
    retracted for a FORMAL misconduct finding — reason
    'Misconduct - Official Investigation(s) and/or Finding(s)' or
    'Investigation by ORI'. Fully sourced; the evidence chain is
    (AuthorInstance)-[:WROTE]->(Paper)-[:RETRACTED_FOR]->(Reason).

  * CLUSTER-DERIVED SIGNAL (`cluster_adjudicated`): a probable-person cluster
    (Step 3) is flagged for review iff ANY of its instances is on a misconduct
    paper. This is a review signal over a soft grouping — NOT a verdict stamped
    on a person. Depends on the current clustering; re-run after re-clustering.

Never a boolean truth about a named human; always a human-review hypothesis.
A cluster whose ONLY misconduct evidence comes from a `coherence_outlier`
instance is called out separately — that is the "innocent flagged via a
same-ORCID mis-assignment" risk and must be reviewed before trusting the flag.

Run (after cluster_instances.py):  python graph_processing/mark_adjudication.py
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sys

from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_authors import resolve_connection  # noqa: E402

ADJUDICATION_REASONS = [
    "Misconduct - Official Investigation(s) and/or Finding(s)",
    "Investigation by ORI",
]


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        # --- per-instance fact ---
        s.run("MATCH (a:AuthorInstance) SET a.on_misconduct_paper = false "
              "REMOVE a.misconduct_reasons")
        s.run(
            "MATCH (a:AuthorInstance)-[:WROTE]->(:Paper)-[:RETRACTED_FOR]->(r:Reason) "
            "WHERE r.code IN $codes "
            "WITH a, collect(DISTINCT r.code) AS reasons "
            "SET a.on_misconduct_paper = true, a.misconduct_reasons = reasons",
            codes=ADJUDICATION_REASONS)

        # --- cluster-derived review signal ---
        # cluster_adjudicated is a PERSON-level trigger ("look into this probable
        # person"), never a claim about the instance's OWN paper. Author role
        # (e.g. 1st/corresponding vs. middle author, decades apart) differs wildly
        # between the misconduct paper and any other paper in the cluster — see
        # the Hiroshi Asakura case: 1st author on the 2021 misconduct retraction,
        # middle (8th/17) author on an unrelated 2001 paper, same cluster. So every
        # flagged instance also carries `cluster_misconduct_dois`, a pointer back
        # to the SPECIFIC paper(s) that actually triggered the flag — the flag
        # must never be read standalone; always resolve it to that evidence.
        s.run("MATCH (a:AuthorInstance) SET a.cluster_adjudicated = false "
              "REMOVE a.cluster_misconduct_dois")
        s.run(
            "MATCH (a:AuthorInstance {on_misconduct_paper:true})-[:WROTE]->(p:Paper) "
            "WITH a.cluster_id AS cid, collect(DISTINCT p.doi) AS dois "
            "MATCH (b:AuthorInstance {cluster_id: cid}) "
            "SET b.cluster_adjudicated = true, b.cluster_misconduct_dois = dois")

        report(s)
    driver.close()


def report(s) -> None:
    n_inst = s.run("MATCH (a:AuthorInstance {on_misconduct_paper:true}) "
                   "RETURN count(*) AS n").single()["n"]
    persons = s.run("MATCH (a:AuthorInstance {cluster_adjudicated:true}) "
                    "RETURN count(DISTINCT a.cluster_id) AS n").single()["n"]
    multi = s.run("MATCH (a:AuthorInstance {cluster_adjudicated:true}) "
                  "WITH a.cluster_id AS c, max(a.cluster_size) AS sz "
                  "WHERE sz > 1 RETURN count(*) AS n").single()["n"]

    # adjudicated clusters whose misconduct evidence is ONLY from outlier instances
    # that a human has NOT already reviewed (coherence_reviewed=true, plan §2.1b,
    # data/graph/identity_overrides.yaml `confirmations` — e.g. Hiroshi Asakura,
    # confirmed same person, excluded here so it doesn't re-flag every run)
    onmis = defaultdict(list)   # cluster_id -> [(is_outlier, is_reviewed, size)]
    for r in s.run("MATCH (a:AuthorInstance {on_misconduct_paper:true}) "
                   "RETURN a.cluster_id AS c, a.coherence_outlier AS o, "
                   "a.coherence_reviewed AS rev, a.cluster_size AS sz"):
        onmis[r["c"]].append((r["o"], bool(r["rev"]), r["sz"]))
    shaky = [c for c, rows in onmis.items()
             if all(o and not rev for (o, rev, sz) in rows) and any(sz >= 2 for (o, rev, sz) in rows)]

    print("=== adjudication re-expressed (instance + cluster grain) ===")
    print(f"  instances on a misconduct paper       : {n_inst}")
    print(f"  adjudicated probable-persons (clusters): {persons}  ({multi} multi-instance)")
    print(f"  (was 201 'adjudicated authors' in the old merged model)")
    print(f"  adjudicated clusters resting ONLY on a coherence-outlier instance: "
          f"{len(shaky)}")
    if shaky:
        print("    (review first — possible misconduct flag on a mis-assigned instance)")
        for c in shaky[:8]:
            r = s.run("MATCH (a:AuthorInstance {cluster_id:$c, on_misconduct_paper:true}) "
                      "RETURN a.name AS name, a.orcid AS orcid, a.cluster_size AS sz "
                      "LIMIT 1", c=c).single()
            print(f"      {r['name']} ({r['orcid']}) cluster_size={r['sz']}")


if __name__ == "__main__":
    main()
