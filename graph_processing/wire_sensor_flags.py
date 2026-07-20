#!/usr/bin/env python3
"""
wire_sensor_flags.py — import Phase 4 sensor flags into the graph as Paper properties.

Reads JSON reports from data/flags/ and writes, for each matching Paper node:
  - retracted_citation_flag_count      : int
  - external_retracted_citation_flag_count : int
  - reference_integrity_flag_count     : int
  - tortured_phrase_flag_count         : int
  - retracted_citation_flags         : JSON string (list of flag records)
  - external_retracted_citation_flags  : JSON string (list of flag records)
  - reference_integrity_flags          : JSON string (list of flag records)
  - tortured_phrase_flags              : JSON string (list of flag records)

Flag records are kept as JSON so the review UI can surface evidence and source URLs.
Papers with no flags get explicit zero counts (empty JSON list) so scoring queries can
use simple numeric properties.

pubpeer is handled separately, below the main SENSOR_CONFIG loop, and deliberately
NOT as count_prop/json_prop: PubPeer comment counts measure community attention,
not misconduct (sound papers attract comments; fraudulent ones can have none), so
they must never feed tier_a_scoring.py's WEIGHTS. Since the 2026-07-18 sensor
rework the records carry real per-paper data, so in addition to
pubpeer_check_status/pubpeer_check_url it also writes the factual triage fields
pubpeer_comments_total / pubpeer_has_author_response / pubpeer_last_commented --
for the review UI only, still never as a weighted count.

This is a one-way enrichment step; it is safe to re-run after refreshing sensor reports.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

FLAGS_DIR = REPO_ROOT / "data" / "flags"
PUBPEER_FLAGS = FLAGS_DIR / "pubpeer_flags.json"

SENSOR_CONFIG = {
    "retracted_citation": {
        "file": FLAGS_DIR / "retracted_citation_flags.json",
        "doi_key": "citing_paper_doi",
        "count_prop": "retracted_citation_flag_count",
        "json_prop": "retracted_citation_flags",
    },
    "external_retracted_citation": {
        "file": FLAGS_DIR / "external_retracted_citation_flags.json",
        "doi_key": "citing_paper_doi",
        "count_prop": "external_retracted_citation_flag_count",
        "json_prop": "external_retracted_citation_flags",
    },
    "reference_integrity": {
        "file": FLAGS_DIR / "reference_integrity_flags.json",
        "doi_key": "citing_paper_doi",
        "count_prop": "reference_integrity_flag_count",
        "json_prop": "reference_integrity_flags",
    },
    "tortured_phrase": {
        "file": FLAGS_DIR / "tortured_phrases_flags.json",
        "doi_key": "paper_doi",
        "count_prop": "tortured_phrase_flag_count",
        "json_prop": "tortured_phrase_flags",
    },
    "ai_text_tell": {
        "file": FLAGS_DIR / "ai_text_tell_flags.json",
        "doi_key": "paper_doi",
        "count_prop": "ai_text_tell_flag_count",
        "json_prop": "ai_text_tell_flags",
    },
    "journal_integrity": {
        "file": FLAGS_DIR / "journal_integrity_flags.json",
        "doi_key": "paper_doi",
        "count_prop": "journal_integrity_flag_count",
        "json_prop": "journal_integrity_flags",
    },
    "p_value_hacking": {
        "file": FLAGS_DIR / "p_value_hacking_flags.json",
        "doi_key": "paper_doi",
        "count_prop": "p_value_hacking_flag_count",
        "json_prop": "p_value_hacking_flags",
    },
}


def canon_doi(doi: str) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


def load_flags(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"WARN: could not read {path}: {e}")
        return []


def main() -> None:
    # Load and group flags by DOI for each sensor
    sensor_groups: dict[str, dict[str, list[dict]]] = {}
    for sensor, cfg in SENSOR_CONFIG.items():
        flags = load_flags(cfg["file"])
        doi_key = cfg["doi_key"]
        groups: dict[str, list[dict]] = {}
        for flag in flags:
            doi = canon_doi(flag.get(doi_key, ""))
            if not doi:
                continue
            groups.setdefault(doi, []).append(flag)
        sensor_groups[sensor] = groups
        print(f"  {sensor}: {len(flags)} flag records -> {len(groups)} distinct papers")

    # Collect all DOIs that appear in any sensor report
    all_dois = set()
    for groups in sensor_groups.values():
        all_dois.update(groups.keys())
    print(f"\n  total distinct papers with any sensor flag: {len(all_dois)}")

    if not all_dois:
        print("  nothing to write; exiting.")
        return

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    written = 0
    with driver.session(database=conn["database"]) as s:
        for doi in sorted(all_dois):
            params: dict = {"doi": canon_doi(doi)}
            set_clauses = []
            for sensor, cfg in SENSOR_CONFIG.items():
                flags = sensor_groups[sensor].get(doi, [])
                params[cfg["count_prop"]] = len(flags)
                params[cfg["json_prop"]] = json.dumps(flags)
                set_clauses.append(
                    f"p.{cfg['count_prop']} = ${cfg['count_prop']}, "
                    f"p.{cfg['json_prop']} = ${cfg['json_prop']}"
                )
            cypher = (
                "MATCH (p:Paper {doi: $doi})\n"
                "SET " + ",\n    ".join(set_clauses)
            )
            result = s.run(cypher, **params)
            summary = result.consume()
            written += summary.counters.properties_set

    driver.close()
    print(f"\n  wrote sensor properties for {len(all_dois)} papers "
          f"({written} property updates)")

    # pubpeer: routing pointer, not a weighted flag count (see module docstring).
    pubpeer_records = load_flags(PUBPEER_FLAGS)
    if pubpeer_records:
        driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
        pp_written = 0
        with driver.session(database=conn["database"]) as s:
            for rec in pubpeer_records:
                doi = canon_doi(rec.get("paper_doi", ""))
                if not doi:
                    continue
                result = s.run(
                    """
                    MATCH (p:Paper {doi: $doi})
                    SET p.pubpeer_check_status = $status,
                        p.pubpeer_check_url = $url,
                        p.pubpeer_comments_total = $comments_total,
                        p.pubpeer_has_author_response = $has_author_response,
                        p.pubpeer_last_commented = $last_commented
                    """,
                    doi=doi,
                    status=rec.get("status", "manual_check_required"),
                    url=rec.get("check_url", ""),
                    comments_total=rec.get("comments_total") or 0,
                    has_author_response=(rec.get("author_responses") or 0) > 0,
                    last_commented=rec.get("last_comment_at"),
                )
                pp_written += result.consume().counters.properties_set
        driver.close()
        print(f"\n  pubpeer: wrote check_status/check_url/comments_total for "
              f"{len(pubpeer_records)} papers ({pp_written} property updates)")


if __name__ == "__main__":
    main()
