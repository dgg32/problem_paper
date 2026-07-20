#!/usr/bin/env python3
"""
add_manual_target.py — one-off graph expansion for a specific, manually
supplied ORCID (extends plan §2.1's targeted expansion).

expand_targets.py only surfaces authors who already have a *retracted* paper
in the graph (TARGETS_QUERY walks cluster -> retraction history). That misses
someone flagged by other credible signals before any retraction lands — e.g.
a live journal Editor's Note plus a university revoking contracts over the
same paper. This script reuses expand_targets.py's fetch/parse/write
machinery for one manually supplied ORCID instead of deriving targets from
retraction history. Not part of the routine pipeline — run by hand when a
specific person needs to be added.

Run:  python graph_processing/add_manual_target.py \
        --orcid 0000-0001-8477-0528 --name "Ping Wang" --k 100
Then: link_instances.py -> cluster_instances.py -> mark_adjudication.py
(same as expand_targets.py's own post-run instructions)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conn import resolve_connection                          # noqa: E402
from expand_targets import (                                   # noqa: E402
    canon_orcid, fetch_works, parse_authors, write, report,
)
from orcid_client import canon_doi                              # noqa: E402
from _crossref_verify import (                                   # noqa: E402
    title_matches_crossref, reconcile_authors_with_crossref, LookupUnavailable,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".env.yaml"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orcid", required=True)
    ap.add_argument("--name", default="", help="for logging only")
    ap.add_argument("--k", type=int, default=100, help="max works to pull")
    ap.add_argument("--min-year", type=int, default=None)
    args = ap.parse_args()

    orcid = canon_orcid(args.orcid)
    if not orcid:
        raise SystemExit(f"not a valid ORCID: {args.orcid}")

    oa = yaml.safe_load(CONFIG_PATH.read_text())["openalex"]
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    papers, instances, wrote, affil, pub_in, cites = [], [], [], [], [], []
    with driver.session(database=conn["database"]) as s:
        existing_dois = {r["doi"] for r in s.run("MATCH (p:Paper) RETURN p.doi AS doi")}
        existing_oaids = {r["o"] for r in
                          s.run("MATCH (p:Paper) WHERE p.openalex_id<>'' RETURN p.openalex_id AS o")}

        works = fetch_works(orcid, args.k, args.min_year, oa)
        seen: set[str] = set()
        added = 0
        skipped_title_mismatch = 0
        for w in works:
            doi = canon_doi(w.get("doi") or "")
            if not doi or doi in existing_dois or doi in seen:
                continue
            try:
                title_ok, cr_title = title_matches_crossref(doi, w.get("title") or "")
            except LookupUnavailable as e:
                print(f"  {doi} Crossref verify unreachable, skipping: {e}")
                continue
            if not title_ok:
                skipped_title_mismatch += 1
                print(f"  SKIP {doi}: OpenAlex/Crossref title mismatch "
                      f"(openalex={w.get('title')!r} crossref={cr_title!r})")
                continue
            seen.add(doi)
            added += 1
            oaid = w.get("id") or ""
            src = (w.get("primary_location") or {}).get("source") or {}
            pmid = ""
            pm = (w.get("ids") or {}).get("pmid")
            if pm:
                import re
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
                print(f"  {doi} Crossref author-verify unreachable, using OpenAlex authors as-is: {e}")
                reconciled, changes = parse_authors(w), []
            for c in changes:
                print(f"  {doi}: {c}")
            for j, a in enumerate(reconciled):
                iid = f"{doi}::{j}"
                from build_instances import name_key
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
                if ref in existing_oaids:
                    cites.append({"doi": doi, "ref": ref})

        write(s, papers, instances, wrote, affil, pub_in, cites)
        report(s, 1, papers, instances, cites)
    driver.close()
    label = args.name or orcid
    print(f"\nmanual target: {label} ({orcid}) -> {added} new papers added "
          f"(of {len(works)} fetched, rest already in graph)")
    print(f"  skipped (title mismatch vs Crossref): {skipped_title_mismatch}")


if __name__ == "__main__":
    main()
