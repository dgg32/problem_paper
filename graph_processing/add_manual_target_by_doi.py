#!/usr/bin/env python3
"""
add_manual_target_by_doi.py — one-off graph expansion for a manually
curated DOI list (sibling of add_manual_target.py).

Some authors can't be safely targeted by ORCID: OpenAlex's own author-entity
disambiguation can merge multiple real people sharing a common name under
one ORCID lookup (confirmed live for a "Ping Wang" whose OpenAlex author
entity mixed cancer-biology topics with methane-hydrate and finance papers
from clearly different people). When that happens, resolve the person's
real paper list some other way (e.g. PubMed affiliation-string search) and
feed the verified DOIs here — each is looked up individually by DOI, which
sidesteps OpenAlex's author-entity clustering entirely.

Run:  python graph_processing/add_manual_target_by_doi.py --doi-file dois.txt
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
from _conn import resolve_connection            # noqa: E402
from expand_targets import parse_authors, write, report  # noqa: E402
from build_instances import name_key             # noqa: E402
from orcid_client import canon_doi                # noqa: E402
from _crossref_verify import (                    # noqa: E402
    title_matches_crossref, reconcile_authors_with_crossref, LookupUnavailable,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".env.yaml"


def fetch_by_doi(doi: str, oa: dict) -> dict | None:
    r = requests.get(f"{oa['base_url'].rstrip('/')}/works/doi:{doi}",
                      params={"mailto": oa.get("mailto", "")}, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi-file", required=True, help="one DOI per line")
    ap.add_argument("--label", default="", help="for logging only")
    args = ap.parse_args()

    dois_in = [ln.strip() for ln in Path(args.doi_file).read_text().splitlines() if ln.strip()]

    oa = yaml.safe_load(CONFIG_PATH.read_text())["openalex"]
    rps = oa.get("requests_per_second", 10)
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    papers, instances, wrote, affil, pub_in, cites = [], [], [], [], [], []
    with driver.session(database=conn["database"]) as s:
        existing_dois = {r["doi"] for r in s.run("MATCH (p:Paper) RETURN p.doi AS doi")}
        existing_oaids = {r["o"] for r in
                          s.run("MATCH (p:Paper) WHERE p.openalex_id<>'' RETURN p.openalex_id AS o")}

        added = 0
        skipped_existing = 0
        skipped_retracted = 0
        skipped_notfound = 0
        skipped_title_mismatch = 0
        seen: set[str] = set()
        last = 0.0
        for i, raw_doi in enumerate(dois_in, 1):
            cdoi = canon_doi(raw_doi)
            if not cdoi or cdoi in existing_dois or cdoi in seen:
                skipped_existing += 1
                continue
            wait = 1.0 / rps - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.time()
            try:
                w = fetch_by_doi(cdoi, oa)
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}/{len(dois_in)}] {cdoi} ERROR {str(e)[:60]}")
                continue
            if w is None:
                skipped_notfound += 1
                continue
            if w.get("is_retracted"):
                skipped_retracted += 1
                continue
            doi = canon_doi(w.get("doi") or "") or cdoi
            try:
                title_ok, cr_title = title_matches_crossref(doi, w.get("title") or "")
            except LookupUnavailable as e:
                print(f"  [{i}/{len(dois_in)}] {doi} Crossref verify unreachable, skipping: {e}")
                continue
            if not title_ok:
                skipped_title_mismatch += 1
                print(f"  [{i}/{len(dois_in)}] SKIP {doi}: OpenAlex/Crossref title mismatch "
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
                print(f"  [{i}/{len(dois_in)}] {doi} Crossref author-verify unreachable, "
                      f"using OpenAlex authors as-is: {e}")
                reconciled, changes = parse_authors(w), []
            for c in changes:
                print(f"  [{i}/{len(dois_in)}] {doi}: {c}")
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
                if ref in existing_oaids:
                    cites.append({"doi": doi, "ref": ref})
            if i % 20 == 0:
                print(f"  [{i}/{len(dois_in)}] +{added} so far")

        write(s, papers, instances, wrote, affil, pub_in, cites)
        report(s, 1, papers, instances, cites)
    driver.close()
    print(f"\nmanual DOI-list target: {args.label or args.doi_file}")
    print(f"  input DOIs           : {len(dois_in)}")
    print(f"  added                : {added}")
    print(f"  skipped (existing)   : {skipped_existing}")
    print(f"  skipped (retracted)  : {skipped_retracted}")
    print(f"  skipped (not found)  : {skipped_notfound}")
    print(f"  skipped (title mismatch vs Crossref): {skipped_title_mismatch}")


if __name__ == "__main__":
    main()
