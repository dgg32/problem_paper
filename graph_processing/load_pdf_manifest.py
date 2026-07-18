#!/usr/bin/env python3
"""
load_pdf_manifest.py — replay data/pdfs/manifest.yaml into the graph.

Writes onto each matching Paper node (matched by doi):
  pdf_local_path, pdf_source_url, pdf_downloaded_date, pdf_notes

Idempotent (MERGE-by-content pattern, same as identity_overrides.yaml):
safe to re-run any time the manifest gains new entries. Papers not yet in
the manifest are untouched; entries whose doi doesn't match any Paper node
are reported as warnings (typo guard).

Usage:
  python graph_processing/load_pdf_manifest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "data" / "pdfs" / "manifest.yaml"


def canon_doi(doi: str) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


def main() -> None:
    data = yaml.safe_load(MANIFEST_PATH.read_text()) or {}
    entries = data.get("pdfs") or []

    if not entries:
        print("No entries in manifest.yaml yet (or all commented out). Nothing to do.")
        return

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    matched, unmatched = 0, []
    with driver.session(database=conn["database"]) as s:
        for e in entries:
            doi = canon_doi(e.get("doi", ""))
            if not doi:
                continue
            result = s.run(
                """
                MATCH (p:Paper {doi: $doi})
                SET p.pdf_local_path = $local_path,
                    p.pdf_source_url = $source_url,
                    p.pdf_downloaded_date = $downloaded_date,
                    p.pdf_notes = $notes
                RETURN p.doi AS doi
                """,
                doi=doi,
                local_path=e.get("local_path", ""),
                source_url=e.get("source_url", ""),
                downloaded_date=str(e.get("downloaded_date", "")),
                notes=e.get("notes", ""),
            ).single()
            if result:
                matched += 1
            else:
                unmatched.append(doi)
    driver.close()

    print(f"Loaded {matched} PDF manifest entries onto Paper nodes.")
    if unmatched:
        print(f"WARNING: {len(unmatched)} doi(s) in manifest not found in graph:")
        for doi in unmatched:
            print(f"  - {doi}")


if __name__ == "__main__":
    main()
