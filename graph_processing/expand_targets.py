#!/usr/bin/env python3
"""
expand_targets.py — targeted graph expansion (plan §2.1, second half).

Instead of casting a big net, pull ONLY the high-impact, not-yet-retracted
papers of the frequent-retraction authors (the top probable-person clusters).
These become the candidate papers Tier-A scoring (Phase 2.2) will rank.

Per target (a cluster's single ORCID) it makes ONE OpenAlex call:
  /works?filter=author.orcid:<orcid>,is_retracted:false
        &sort=cited_by_count:desc&per-page=<K>
i.e. the K most-cited non-retracted works. For each NEW paper it adds:
  Paper {is_retracted:false, source:'expansion', cited_by_count} + its full
  authorship (AuthorInstance + WROTE + AFFILIATED_WITH) + PUBLISHED_IN, and
  CITES edges to any paper ALREADY in the graph (the "cites a retracted paper"
  signal). It does NOT recurse into co-authors' own papers — one hop, then stop.

New AuthorInstance nodes flow through the identity pipeline unchanged; re-run
link_instances -> cluster_instances -> mark_adjudication after this.

Pipeline order: build_instances -> apply_overrides -> EXPAND_TARGETS ->
                link_instances -> cluster_instances -> mark_adjudication
Idempotent (MERGE throughout). Runs after build_instances (which rebuilds the
seed instances from graph.json and would otherwise wipe expansion authorships).

Run:  python graph_processing/expand_targets.py [--top-n 25] [--k 25] [--min-year 2015]
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import requests
import yaml
from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_authors import resolve_connection   # noqa: E402
from build_instances import name_key               # noqa: E402
from orcid_client import canon_doi                  # noqa: E402
from _crossref_verify import (                      # noqa: E402
    title_matches_crossref, reconcile_authors_with_crossref, LookupUnavailable,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".env.yaml"
BATCH = 1000
ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")


def canon_orcid(raw) -> str | None:
    if not raw:
        return None
    m = ORCID_RE.search(str(raw))
    return m.group(0) if m else None


TARGETS_QUERY = """
MATCH (a:AuthorInstance)-[:WROTE]->(p:Paper {is_retracted:true})
WITH a.cluster_id AS cid, count(DISTINCT p) AS retractions
ORDER BY retractions DESC LIMIT $topn
MATCH (b:AuthorInstance {cluster_id:cid}) WHERE b.orcid IS NOT NULL
WITH cid, retractions, collect(DISTINCT b.orcid) AS orcids, collect(DISTINCT b.name)[0] AS name
RETURN cid AS cluster_id, name, retractions, orcids[0] AS orcid
ORDER BY retractions DESC
"""


def fetch_works(orcid: str, k: int, min_year: int | None, oa: dict) -> list[dict]:
    flt = f"author.orcid:{orcid},is_retracted:false"
    if min_year:
        flt += f",from_publication_date:{min_year}-01-01"
    params = {"filter": flt, "sort": "cited_by_count:desc",
              "per-page": min(k, 200), "mailto": oa.get("mailto", "")}
    r = requests.get(f"{oa['base_url'].rstrip('/')}/works", params=params, timeout=60)
    r.raise_for_status()
    return r.json().get("results", [])[:k]


def parse_authors(work: dict) -> list[dict]:
    out = []
    for a in work.get("authorships", []):
        auth = a.get("author") or {}
        insts = []
        for inst in a.get("institutions", []):
            iid = inst.get("ror") or inst.get("id") or ""
            if iid:
                insts.append({"id": iid, "name": inst.get("display_name", ""),
                              "country": inst.get("country_code", "")})
        raw_aff = a.get("raw_affiliation_strings") or []
        out.append({
            "name": auth.get("display_name") or a.get("raw_author_name", ""),
            "orcid": canon_orcid(auth.get("orcid")),
            "position": a.get("author_position", ""),
            "is_corresponding": bool(a.get("is_corresponding")),
            "affiliation": raw_aff[0] if raw_aff else "",
            "institutions": insts,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=25, help="how many top clusters to expand")
    ap.add_argument("--k", type=int, default=25, help="high-impact papers per target")
    ap.add_argument("--min-year", type=int, default=None, help="only papers from this year on")
    args = ap.parse_args()

    oa = yaml.safe_load(CONFIG_PATH.read_text())["openalex"]
    rps = oa.get("requests_per_second", 10)
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    papers, instances, wrote, affil, pub_in, cites = [], [], [], [], [], []
    with driver.session(database=conn["database"]) as s:
        targets = [dict(r) for r in s.run(TARGETS_QUERY, topn=args.top_n)]
        existing_dois = {r["doi"] for r in s.run("MATCH (p:Paper) RETURN p.doi AS doi")}
        existing_oaids = {r["o"] for r in
                          s.run("MATCH (p:Paper) WHERE p.openalex_id<>'' RETURN p.openalex_id AS o")}

        seen: set[str] = set()
        last = 0.0
        for i, t in enumerate(targets, 1):
            wait = 1.0 / rps - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.time()
            try:
                works = fetch_works(t["orcid"], args.k, args.min_year, oa)
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}/{len(targets)}] {t['name']:<24} ERROR {str(e)[:50]}")
                continue

            added = 0
            skipped_title_mismatch = 0
            for w in works:
                doi = canon_doi(w.get("doi") or "")
                if not doi or doi in existing_dois or doi in seen:
                    continue
                try:
                    title_ok, cr_title = title_matches_crossref(doi, w.get("title") or "")
                except LookupUnavailable as e:
                    print(f"    {doi} Crossref verify unreachable, skipping: {e}")
                    continue
                if not title_ok:
                    skipped_title_mismatch += 1
                    print(f"    SKIP {doi}: OpenAlex/Crossref title mismatch "
                          f"(openalex={w.get('title')!r} crossref={cr_title!r})")
                    continue
                seen.add(doi)
                added += 1
                oaid = w.get("id") or ""
                src = (w.get("primary_location") or {}).get("source") or {}
                pmid = ""
                pm = (w.get("ids") or {}).get("pmid")
                if pm:
                    m = re.search(r"\d+", pm)
                    pmid = m.group(0) if m else ""
                papers.append({"doi": doi, "title": w.get("title") or "", "oaid": oaid,
                               "pmid": pmid, "pub_date": w.get("publication_date"),
                               "cited_by_count": w.get("cited_by_count", 0)})
                if src.get("display_name"):
                    pub_in.append({"doi": doi, "journal": src["display_name"],
                                   "publisher": src.get("host_organization_name") or ""})
                try:
                    reconciled, changes = reconcile_authors_with_crossref(doi, parse_authors(w))
                except LookupUnavailable as e:
                    print(f"    {doi} Crossref author-verify unreachable, using OpenAlex authors as-is: {e}")
                    reconciled, changes = parse_authors(w), []
                for c in changes:
                    print(f"    {doi}: {c}")
                for j, a in enumerate(reconciled):
                    iid = f"{doi}::{j}"
                    instances.append({"iid": iid, "name": a["name"],
                                      "name_key": name_key(a["name"]), "orcid": a["orcid"],
                                      "has_orcid": bool(a["orcid"]),
                                      "orcid_source": "openalex" if a["orcid"] else None})
                    wrote.append({"iid": iid, "doi": doi, "position": a["position"] or None,
                                  "is_corr": a["is_corresponding"],
                                  "affiliation": a["affiliation"] or None})
                    for inst in a["institutions"]:
                        affil.append({"iid": iid, "inst": inst["id"],
                                      "inst_name": inst["name"], "country": inst["country"]})
                for ref in (w.get("referenced_works") or []):
                    if ref in existing_oaids:      # only edges to papers we already have
                        cites.append({"doi": doi, "ref": ref})
            print(f"  [{i}/{len(targets)}] {t['name']:<24} +{added} new papers "
                  f"({skipped_title_mismatch} title-mismatch skips)")

        write(s, papers, instances, wrote, affil, pub_in, cites)
        report(s, len(targets), papers, instances, cites)
    driver.close()


def write(s, papers, instances, wrote, affil, pub_in, cites) -> None:
    def run(q, rows):
        for i in range(0, len(rows), BATCH):
            s.run(q, rows=rows[i:i + BATCH])

    run("""UNWIND $rows AS r MERGE (p:Paper {doi:r.doi})
           ON CREATE SET p.is_retracted=false, p.source='expansion'
           SET p.title=r.title, p.openalex_id=r.oaid, p.pmid=r.pmid,
               p.cited_by_count=r.cited_by_count,
               p.published_date = CASE WHEN r.pub_date IS NULL THEN null ELSE date(r.pub_date) END""",
        papers)
    run("""UNWIND $rows AS r MATCH (p:Paper {doi:r.doi})
           MERGE (j:Journal {name:r.journal})
           ON CREATE SET j.publisher = CASE WHEN r.publisher='' THEN null ELSE r.publisher END
           MERGE (p)-[:PUBLISHED_IN]->(j)""", pub_in)
    run("""UNWIND $rows AS r MERGE (a:AuthorInstance {instance_id:r.iid})
           SET a.name=r.name, a.name_key=r.name_key, a.has_orcid=r.has_orcid,
               a.orcid_source=r.orcid_source, a.orcid=r.orcid, a.source='expansion'""", instances)
    run("""UNWIND $rows AS r MATCH (a:AuthorInstance {instance_id:r.iid}), (p:Paper {doi:r.doi})
           MERGE (a)-[w:WROTE]->(p)
           SET w.author_position=r.position, w.is_corresponding=r.is_corr, w.affiliation=r.affiliation""",
        wrote)
    run("""UNWIND $rows AS r MERGE (i:Institution {institution_id:r.inst})
           ON CREATE SET i.name=r.inst_name, i.country = CASE WHEN r.country='' THEN null ELSE r.country END
           WITH i, r MATCH (a:AuthorInstance {instance_id:r.iid})
           MERGE (a)-[:AFFILIATED_WITH]->(i)""", affil)
    run("""UNWIND $rows AS r MATCH (c:Paper {doi:r.doi}), (ref:Paper {openalex_id:r.ref})
           MERGE (c)-[:CITES]->(ref)""", cites)


def report(s, n_targets, papers, instances, cites) -> None:
    to_retracted = s.run(
        "MATCH (:Paper {source:'expansion'})-[:CITES]->(r:Paper {is_retracted:true}) "
        "RETURN count(*) AS n").single()["n"]
    print("\n=== targeted expansion ===")
    print(f"  targets expanded        : {n_targets}")
    print(f"  new candidate papers    : {len(papers)}  (is_retracted=false, source='expansion')")
    print(f"  new authorship instances: {len(instances)}")
    print(f"  CITES edges added       : {len(cites)}  (to papers already in graph)")
    print(f"  -- of which cite a RETRACTED paper: {to_retracted}  <- high-value signal")
    print("\n  next: re-run link_instances -> cluster_instances -> mark_adjudication")


if __name__ == "__main__":
    main()
