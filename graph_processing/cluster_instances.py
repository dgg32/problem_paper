#!/usr/bin/env python3
"""
cluster_instances.py — Step 3 of the instance-based author identity layer.

Soft resolution: groups AuthorInstance nodes into "probable persons" by running
weakly-connected-components over PROBABLY_SAME_AS edges AT OR ABOVE a confidence
threshold. Writes a recomputable `cluster_id` (+ `cluster_size`) on each node.
This is a DERIVED grouping for querying/features — not a merge. Re-run at any
threshold; nothing about the underlying instances or edges changes.

Also computes a review-only COHERENCE flag. The conflict detector (Step 2)
catches mis-assignment when two instances carry DIFFERENT ORCIDs. It cannot see
the opposite case — an instance wrongly given the SAME ORCID as a prolific
namesake, which then joins that cluster at 0.97 with no conflict. Such an
instance betrays itself by sharing NEITHER a co-author NOR an institution with
the rest of its cluster. We flag those (`coherence_outlier = true`) for a human
to look at; we never auto-detach (attribution stays human-gated, plan §0).

Default threshold 0.70 = orcid_match + name_coauthor (co-author-corroborated),
excluding institution-only / name-only / conflict tiers.

Run:  python graph_processing/cluster_instances.py [--threshold 0.70]
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path
import sys

import yaml
from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_authors import resolve_connection  # noqa: E402

BATCH = 2000
OVERRIDES = Path(__file__).resolve().parent.parent / "data" / "graph" / "identity_overrides.yaml"


def load_confirmations() -> dict[str, dict]:
    """orcid -> {reason, sources} for human-reviewed coherence_outlier clusters."""
    if not OVERRIDES.exists():
        return {}
    confs = (yaml.safe_load(OVERRIDES.read_text()) or {}).get("confirmations") or []
    return {c["orcid"]: c for c in confs if c.get("orcid")}


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:      # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.70,
                    help="min PROBABLY_SAME_AS confidence to merge into a cluster")
    args = ap.parse_args()
    t = args.threshold

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        # --- per-instance data (coauthors by name_key, institutions, retraction) ---
        nk, doi_of, retracted, orcid_of = {}, {}, {}, {}
        members = defaultdict(list)
        insts = defaultdict(set)
        for r in s.run("MATCH (a:AuthorInstance)-[:WROTE]->(p:Paper) "
                       "RETURN a.instance_id AS iid, a.name_key AS nk, p.doi AS doi, "
                       "p.is_retracted AS retr, a.orcid AS orcid"):
            nk[r["iid"]] = r["nk"]; doi_of[r["iid"]] = r["doi"]
            retracted[r["iid"]] = bool(r["retr"])
            orcid_of[r["iid"]] = r["orcid"]
            members[r["doi"]].append((r["iid"], r["nk"]))
        for r in s.run("MATCH (a:AuthorInstance)-[:AFFILIATED_WITH]->(i:Institution) "
                       "RETURN a.instance_id AS iid, i.institution_id AS inst"):
            insts[r["iid"]].add(r["inst"])
        coauth = {iid: {mnk for (mi, mnk) in members[doi]
                        if mi != iid and mnk and mnk != nk[iid]}
                  for iid, doi in doi_of.items()}

        # --- weakly-connected-components over edges >= threshold ---
        uf = UnionFind()
        for iid in nk:                      # seed singletons
            uf.find(iid)
        n_edges = 0
        for r in s.run("MATCH (a:AuthorInstance)-[e:PROBABLY_SAME_AS]->(b:AuthorInstance) "
                       "WHERE e.confidence >= $t RETURN a.instance_id AS a, b.instance_id AS b",
                       t=t):
            uf.union(r["a"], r["b"]); n_edges += 1

        comps = defaultdict(list)
        for iid in nk:
            comps[uf.find(iid)].append(iid)
        # stable, human-readable cluster id = smallest instance_id in the component
        cluster_of = {iid: min(mem) for mem in comps.values() for iid in mem}
        size_of = {iid: len(mem) for mem in comps.values() for iid in mem}

        # --- coherence outliers (shares no co-author AND no institution w/ cluster) ---
        by_cluster = defaultdict(list)
        for iid in nk:
            by_cluster[cluster_of[iid]].append(iid)
        outlier = {}
        for cid, mem in by_cluster.items():
            if len(mem) < 2:
                for iid in mem:
                    outlier[iid] = False
                continue
            co_count, inst_count = Counter(), Counter()
            for iid in mem:
                for x in coauth[iid]:
                    co_count[x] += 1
                for x in insts[iid]:
                    inst_count[x] += 1
            for iid in mem:
                shares_co = any(co_count[x] >= 2 for x in coauth[iid])
                shares_inst = any(inst_count[x] >= 2 for x in insts[iid])
                outlier[iid] = not (shares_co or shares_inst)

        # --- human confirmations (reviewed, no split needed) ---
        confirmations = load_confirmations()
        confirmed_iids = {iid for iid in nk if orcid_of.get(iid) in confirmations}

        # --- write cluster props ---
        rows = [{"iid": iid, "cid": cluster_of[iid], "size": size_of[iid],
                 "outlier": outlier[iid],
                 "reviewed": iid in confirmed_iids,
                 "verdict": "confirmed_same_person" if iid in confirmed_iids else None,
                 "reason": confirmations.get(orcid_of.get(iid), {}).get("reason")
                           if iid in confirmed_iids else None}
                for iid in nk]
        s.run("CREATE INDEX author_instance_cluster IF NOT EXISTS "
              "FOR (a:AuthorInstance) ON (a.cluster_id)")
        for i in range(0, len(rows), BATCH):
            s.run("UNWIND $rows AS r MATCH (a:AuthorInstance {instance_id:r.iid}) "
                  "SET a.cluster_id=r.cid, a.cluster_size=r.size, "
                  "    a.coherence_outlier=r.outlier, "
                  "    a.coherence_reviewed=r.reviewed, "
                  "    a.coherence_verdict=r.verdict, "
                  "    a.coherence_review_reason=r.reason", rows=rows[i:i + BATCH])

        report(comps, by_cluster, cluster_of, outlier, retracted, nk, t, n_edges, s,
              confirmed_iids)
    driver.close()


def report(comps, by_cluster, cluster_of, outlier, retracted, nk, t, n_edges, s,
          confirmed_iids) -> None:
    sizes = sorted((len(m) for m in comps.values()), reverse=True)
    n_clusters = len(sizes)
    singletons = sum(1 for x in sizes if x == 1)
    multi = n_clusters - singletons
    outliers = [iid for iid, o in outlier.items() if o]
    # outliers sitting in a cluster that contains a retraction = highest priority,
    # ranked by cluster size DESC: a lone stranger inside a big coherent cluster is
    # a strong same-ORCID mis-assignment signal; a size-2 pair is the noisy tail.
    # Already human-CONFIRMED clusters (data/graph/identity_overrides.yaml) are
    # excluded here so a reviewed case doesn't keep resurfacing — the raw
    # coherence_outlier flag on the node stays true regardless.
    prio = [iid for iid in outliers
            if iid not in confirmed_iids
            and any(retracted[j] for j in by_cluster[cluster_of[iid]])]
    prio.sort(key=lambda iid: len(by_cluster[cluster_of[iid]]), reverse=True)
    strong = [iid for iid in prio if len(by_cluster[cluster_of[iid]]) >= 3]
    weak = len(prio) - len(strong)
    n_confirmed = len(confirmed_iids)

    print(f"=== clustering @ threshold {t} ({n_edges} qualifying edges) ===")
    print(f"  instances            : {len(nk)}")
    print(f"  probable persons     : {n_clusters}  ({multi} multi-instance, {singletons} singletons)")
    print(f"  largest clusters     : {sizes[:8]}")
    print(f"  coherence outliers   : {len(outliers)}  ({n_confirmed} human-confirmed, excluded below; "
          f"{len(prio)} unreviewed in a retraction cluster: "
          f"{len(strong)} strong [size>=3], {weak} weak [size 2])")

    for who in ("didier raoult", "florence fenollar"):
        ids = [i for i in nk if nk[i] == who]
        cids = {s.run("MATCH (a:AuthorInstance {instance_id:$i}) RETURN a.cluster_id AS c",
                      i=i).single()["c"] for i in ids}
        print(f"  {who}: {len(ids)} instances -> {len(cids)} cluster(s)")
    bing = s.run("MATCH (a:AuthorInstance {name_key:'bing liu'}) "
                 "RETURN count(DISTINCT a.cluster_id) AS c, count(*) AS n").single()
    print(f"  bing liu: {bing['n']} instances -> {bing['c']} clusters (expect all singletons)")

    if strong:
        print("\n  Priority coherence outliers (size>=3, ranked by cluster size — "
              "review for same-ORCID mis-assignment):")
        for iid in strong[:10]:
            r = s.run("MATCH (a:AuthorInstance {instance_id:$i})-[:WROTE]->(p:Paper) "
                      "RETURN a.name AS name, a.orcid AS orcid, a.cluster_size AS cs, "
                      "p.title AS title, p.is_retracted AS retr", i=iid).single()
            print(f"    {r['name']} ({r['orcid']}) cluster_size={r['cs']} "
                  f"retracted={r['retr']} :: {(r['title'] or '')[:55]}")


if __name__ == "__main__":
    main()
