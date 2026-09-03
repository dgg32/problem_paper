#!/usr/bin/env python3
"""
link_instances.py — Step 2 of the instance-based author identity layer.

Draws PROBABLY_SAME_AS edges between AuthorInstance nodes that MAY be the same
person. Nothing is merged; every edge is a reversible, weighted hypothesis that
downstream clustering (Step 3) resolves at a chosen confidence threshold.

Blocking: pairs sharing first+last name token (subsumes exact-name matches;
measured to add no common-name explosion on this data).

Edge structure is HYBRID: same-ORCID groups are linked as a spanning tree
(k-1 edges — lossless, since every such edge is a flat 0.97), while the
varied-confidence name-based links stay full pairwise (redundancy there
protects threshold-based clustering). This cuts a mega-author like Raoult
from 25,651 clique edges to 226 without changing any cluster.

Confidence tiers (per the agreed design; ORCID is a LOW-TRUST signal because
OpenAlex mis-assigns it — so a conflicting ORCID lowers confidence, it does NOT
veto the edge):

  orcid_match          0.97   both carry the SAME ORCID (strongest available)
  name_coauthor        0.70-0.90  no ORCID conflict; share >=1 co-author
  name_institution     ~0.50  share an institution only
  name_only            ~0.35  same name, nothing else
  name_orcid_conflict  0.05-0.14  DIFFERENT ORCIDs -> lowest, but still recorded
                                  (shared co-authors nudge it up: the signature
                                   of a possible OpenAlex mis-assignment to review)

Exact full-name match is +0.05 vs a first+last-only match (-0.05).

Co-authors are compared by name_key (the only identity available pre-clustering).
Field/topic overlap (a listed tier) is deferred — the graph has no topic data yet.

Run:  python graph_processing/link_instances.py            # write edges
      python graph_processing/link_instances.py --dry-run  # report only
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from itertools import combinations
from pathlib import Path
import sys

import yaml
from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_authors import resolve_connection  # noqa: E402

BATCH = 5000
OVERRIDES = Path(__file__).resolve().parent.parent / "data" / "graph" / "identity_overrides.yaml"


def forced_merge_edges(session):
    """Human MERGE overrides -> forced PROBABLY_SAME_AS edges (confidence 1.0)."""
    if not OVERRIDES.exists():
        return []
    merges = (yaml.safe_load(OVERRIDES.read_text()) or {}).get("merges") or []
    edges = []
    for m in merges:
        iids = []
        for mem in m.get("members", []):
            for r in session.run(
                    "MATCH (a:AuthorInstance)-[:WROTE]->(:Paper {doi:$doi}) "
                    "WHERE a.name = $name RETURN a.instance_id AS iid",
                    doi=mem.get("doi"), name=mem.get("name")):
                iids.append(r["iid"])
        for a, b in combinations(sorted(set(iids)), 2):
            edges.append({
                "a": a, "b": b, "confidence": 1.0, "basis": "human_verified",
                "shared_coauthors": 0, "shared_institutions": 0,
                "same_orcid": False, "orcid_conflict": False, "name_match": "human",
            })
    return edges


def score(same_orcid, orcid_conflict, shared_co, shared_inst, name_exact):
    if same_orcid:
        return 0.97, "orcid_match"
    if orcid_conflict:
        return round(0.05 + 0.03 * min(shared_co, 3), 3), "name_orcid_conflict"
    if shared_co > 0:
        base, basis = 0.70 + 0.04 * min(shared_co, 5), "name_coauthor"
    elif shared_inst > 0:
        base, basis = 0.50, "name_institution"
    else:
        base, basis = 0.35, "name_only"
    base += 0.05 if name_exact else -0.05
    return round(min(0.95, base), 3), basis


def first_last(name_key: str):
    t = name_key.split()
    return (t[0], t[-1]) if len(t) >= 2 else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        # --- pull instance data ---
        nk = {}       # iid -> name_key
        orcid = {}    # iid -> orcid or None
        doi_of = {}   # iid -> doi
        members = defaultdict(list)   # doi -> [(iid, name_key)]
        for r in s.run("MATCH (a:AuthorInstance)-[:WROTE]->(p:Paper) "
                       "RETURN a.instance_id AS iid, a.name_key AS nk, "
                       "a.orcid AS orcid, p.doi AS doi"):
            nk[r["iid"]] = r["nk"]
            orcid[r["iid"]] = r["orcid"]
            doi_of[r["iid"]] = r["doi"]
            members[r["doi"]].append((r["iid"], r["nk"]))

        insts = defaultdict(set)      # iid -> {institution_id}
        for r in s.run("MATCH (a:AuthorInstance)-[:AFFILIATED_WITH]->(i:Institution) "
                       "RETURN a.instance_id AS iid, i.institution_id AS inst"):
            insts[r["iid"]].add(r["inst"])

        # co-author name_keys per instance (exclude self, blanks)
        coauth = {}
        for iid, doi in doi_of.items():
            self_nk = nk[iid]
            coauth[iid] = {m_nk for (m_iid, m_nk) in members[doi]
                           if m_iid != iid and m_nk and m_nk != self_nk}

        # --- blocking on first+last ---
        blocks = defaultdict(list)
        for iid, key in nk.items():
            fl = first_last(key)
            if fl:
                blocks[fl].append(iid)

        # --- score candidate pairs (HYBRID) ---
        edges = []

        # (1) same-ORCID groups -> spanning tree (star from the lowest instance_id).
        #     Lossless for clustering because every intra-group edge is a flat 0.97,
        #     so redundant clique edges carry no extra information. Grouped by ORCID
        #     directly (not via name blocking), so it also links the same person's
        #     instances even when the name SPELLING differs — ORCID is the signal.
        orcid_groups = defaultdict(list)
        for iid, o in orcid.items():
            if o:
                orcid_groups[o].append(iid)
        for o, ids in orcid_groups.items():
            if len(ids) < 2:
                continue
            ids_sorted = sorted(ids)
            rep = ids_sorted[0]
            for other in ids_sorted[1:]:
                edges.append({
                    "a": rep, "b": other, "confidence": 0.97, "basis": "orcid_match",
                    "shared_coauthors": len(coauth[rep] & coauth[other]),
                    "shared_institutions": len(insts[rep] & insts[other]),
                    "same_orcid": True, "orcid_conflict": False,
                    "name_match": "exact" if nk[rep] == nk[other] else "variant",
                })

        # (2) name-based pairs (varied confidence) -> full pairwise, EXCLUDING the
        #     same-ORCID pairs already covered by the spanning tree above. Full
        #     pairwise is kept here because confidences vary within a block, so
        #     redundant edges protect threshold-based clustering correctness.
        for fl, ids in blocks.items():
            if len(ids) < 2:
                continue
            for a, b in combinations(sorted(ids), 2):
                oa, ob = orcid[a], orcid[b]
                if oa and oa == ob:
                    continue  # handled by the spanning tree in (1)
                orcid_conflict = bool(oa) and bool(ob) and oa != ob
                shared_co = len(coauth[a] & coauth[b])
                shared_inst = len(insts[a] & insts[b])
                name_exact = nk[a] == nk[b]
                # keep only pairs with some support (drops bare first+last noise)
                if not (orcid_conflict or shared_co or shared_inst or name_exact):
                    continue
                conf, basis = score(False, orcid_conflict, shared_co,
                                    shared_inst, name_exact)
                edges.append({
                    "a": a, "b": b, "confidence": conf, "basis": basis,
                    "shared_coauthors": shared_co, "shared_institutions": shared_inst,
                    "same_orcid": False, "orcid_conflict": orcid_conflict,
                    "name_match": "exact" if name_exact else "first_last",
                })

        forced = forced_merge_edges(s)
        if forced:
            edges.extend(forced)
            print(f"+ {len(forced)} forced human_verified merge edge(s) from overrides")

        summarize(edges)
        if args.dry_run:
            print("\nDRY-RUN — no edges written")
            driver.close()
            return

        # Delete + rewrite as ONE explicit transaction (BUG.md R3-15): each of these
        # used to be its own auto-committed statement/batch, so a crash between the
        # delete and the last UNWIND batch left a partial identity layer with no
        # detection -- a subsequent cluster_instances.py run would silently cluster
        # over the truncated edges. Wrapping them together means either every edge
        # lands or none do; a mid-run crash now rolls back to the pre-run state
        # instead of a half-written one.
        with s.begin_transaction() as tx:
            tx.run("MATCH ()-[r:PROBABLY_SAME_AS]->() DELETE r")
            for i in range(0, len(edges), BATCH):
                tx.run(
                    "UNWIND $rows AS r "
                    "MATCH (a:AuthorInstance {instance_id:r.a}), "
                    "      (b:AuthorInstance {instance_id:r.b}) "
                    "MERGE (a)-[e:PROBABLY_SAME_AS]->(b) "
                    "SET e.confidence=r.confidence, e.basis=r.basis, "
                    "    e.shared_coauthors=r.shared_coauthors, "
                    "    e.shared_institutions=r.shared_institutions, "
                    "    e.same_orcid=r.same_orcid, e.orcid_conflict=r.orcid_conflict, "
                    "    e.name_match=r.name_match",
                    rows=edges[i:i + BATCH])
            tx.commit()
        print(f"\nwrote {len(edges)} PROBABLY_SAME_AS edges")
        verify(s)
    driver.close()


def summarize(edges) -> None:
    by_basis, buckets = defaultdict(int), defaultdict(int)
    for e in edges:
        by_basis[e["basis"]] += 1
        buckets[f"{int(e['confidence']*10)*10:>3}%+"] += 1
    print(f"candidate edges: {len(edges)}")
    print("  by basis:")
    for b in ("orcid_match", "name_coauthor", "name_institution", "name_only",
              "name_orcid_conflict"):
        if by_basis[b]:
            print(f"    {b:20}: {by_basis[b]:6}")
    print("  by confidence bucket:")
    for k in sorted(buckets, reverse=True):
        print(f"    {k:6}: {buckets[k]:6}")


def verify(s) -> None:
    print("\n=== verification ===")
    print("  Bing Liu instances — pairwise links (expect lowest-tier conflict):")
    for r in s.run(
        "MATCH (a:AuthorInstance {name_key:'bing liu'})-[e:PROBABLY_SAME_AS]"
        "-(b:AuthorInstance {name_key:'bing liu'}) "
        "RETURN DISTINCT a.orcid AS oa, b.orcid AS ob, e.confidence AS c, "
        "e.basis AS basis, e.shared_coauthors AS sc"):
        print(f"    {r['oa']} ~ {r['ob']}  conf={r['c']} {r['basis']} (shared_co={r['sc']})")
    for who in ("didier raoult", "florence fenollar"):
        r = s.run(
            "MATCH (a:AuthorInstance {name_key:$k})-[e:PROBABLY_SAME_AS]-(:AuthorInstance {name_key:$k}) "
            "RETURN count(DISTINCT e) AS edges, avg(e.confidence) AS conf", k=who).single()
        print(f"  {who}: {r['edges']} internal links, avg conf {r['conf']:.2f}")


if __name__ == "__main__":
    main()
