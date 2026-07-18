#!/usr/bin/env python3
"""
pdf_to_markdown.py — convert downloaded PDFs to Markdown via PyMuPDF4LLM.

Source of truth is the graph, not the directory listing: reads every Paper
node that already has pdf_local_path set (i.e. has been through
load_pdf_manifest.py), converts data/pdfs/<pdf_local_path> to Markdown, and
writes the result to data/pdfs/markdown/<same stem>.md. Writes the resulting
path back onto the Paper node as pdf_markdown_path (+ pdf_markdown_date), same
provenance pattern as load_pdf_manifest.py's pdf_* fields.

Idempotent: skips a PDF whose markdown file is already newer than the PDF
(re-run safely after adding new manifest entries; use --force to reconvert
everything, e.g. after a PyMuPDF4LLM upgrade).

Text-only for now (write_images=False) — figures/tables are not extracted as
images, only their text layer. Revisit if a later sensor needs figures.

Usage:
  python graph_processing/pdf_to_markdown.py
  python graph_processing/pdf_to_markdown.py --force
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pymupdf4llm
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

PDFS_ROOT = REPO_ROOT / "data" / "pdfs"
MARKDOWN_DIR = PDFS_ROOT / "markdown"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force", action="store_true",
        help="reconvert even if the markdown file is already up to date",
    )
    args = parser.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    with driver.session(database=conn["database"]) as s:
        papers = s.run(
            """
            MATCH (p:Paper)
            WHERE p.pdf_local_path IS NOT NULL AND p.pdf_local_path <> ""
            RETURN p.doi AS doi, p.pdf_local_path AS pdf_local_path
            ORDER BY doi
            """
        ).data()

        converted, skipped, errors = 0, 0, []
        for row in papers:
            doi, pdf_rel = row["doi"], row["pdf_local_path"]
            pdf_path = PDFS_ROOT / pdf_rel
            if not pdf_path.exists():
                errors.append(f"{doi}: PDF file missing at {pdf_path}")
                continue

            md_path = MARKDOWN_DIR / (Path(pdf_rel).stem + ".md")
            if not args.force and md_path.exists() and md_path.stat().st_mtime >= pdf_path.stat().st_mtime:
                skipped += 1
                continue

            try:
                md_text = pymupdf4llm.to_markdown(str(pdf_path), write_images=False)
            except Exception as exc:  # noqa: BLE001 - report and continue, one bad PDF shouldn't kill the batch
                errors.append(f"{doi}: conversion failed ({exc})")
                continue

            MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
            md_path.write_text(md_text)

            md_rel = str(md_path.relative_to(PDFS_ROOT))
            s.run(
                """
                MATCH (p:Paper {doi: $doi})
                SET p.pdf_markdown_path = $md_path,
                    p.pdf_markdown_date = $today
                """,
                doi=doi,
                md_path=md_rel,
                today=str(date.today()),
            )
            converted += 1
            print(f"  converted {doi} -> {md_rel} ({len(md_text)} chars)")

    driver.close()

    print(f"\nConverted {converted}, skipped {skipped} (up to date), {len(errors)} error(s).")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  - {e}")


if __name__ == "__main__":
    main()
