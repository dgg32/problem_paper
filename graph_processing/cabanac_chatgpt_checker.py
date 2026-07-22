#!/usr/bin/env python3
"""
cabanac_chatgpt_checker.py — flags papers matching Guillaume Cabanac's
Problematic Paper Screener (PPS) "Suspect Fingerprints" detector, filtered to
the cases the PPS itself categorizes as ChatGPT-generated text
(skill_worth_exploring.md 2026-07-22, "Cabanac ChatGPT-writing list").

Source: https://dbrech.irit.fr/pls/apex/f?p=9999:25::::RIR:IRC_TORTUREDPHRASES:ChatGPT
(PPS "Suspect" detector page, server-side filtered to the "ChatGPT" fingerprint
category). Manually pulled 2026-07-22 via browser automation -- the report is
an Oracle APEX interactive report rendered client-side via AJAX, with no plain
GET-able export, so it was captured by loading the page, expanding to all rows,
and reading the rendered DOI/evidence/PubPeer-link cells. Frozen as
data/cabanac_chatgpt.csv (109 DOIs), matching the project's "pull it once,
freeze it, don't silently auto-refresh" discipline already applied to
retraction_watch.csv and the Hijacked Journal Checker list.

MATCHING: DOI-to-DOI, no name-matching involved at all -- even safer than the
known-miller/author-rate sensors, since there is no person-identity ambiguity
here whatsoever.

SCORING: unscored review context, NOT fed into the score. Two reasons:
  1. Purpose stated in skill_worth_exploring.md is to CALIBRATE the existing
     ai_text_tell_flag_count sensor (give it confirmed-positive cases to check
     its own hit rate against), not to be an independent scored signal.
  2. This is the same underlying phenomenon (AI-generation text tells) that
     ai_text_tell_flag_count already scores directly from full text. Scoring
     both would double-weight the same category of evidence differently
     depending on which of two sources happened to catch it first, rather
     than by whether the tell exists.
Unlike institution/journal/co-author signals, this IS direct (not ecological)
evidence about this specific paper's own text -- so if a future review
decides to score it, it belongs as a variant of ai_text_tell, not a new
ecological-evidence caveat.

Fields written (unscored context, same bucket as journal_hijack_flag /
known_miller_coauthor):
  Paper.cabanac_chatgpt_flag        : true/false
  Paper.cabanac_chatgpt_fingerprint : the matched fingerprint phrase(s), as text
  Paper.cabanac_chatgpt_pubpeer_url : PubPeer thread for this paper (may be empty --
                                       2/109 rows have no PubPeer link yet)

Usage:
  python graph_processing/cabanac_chatgpt_checker.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

CABANAC_CSV = REPO_ROOT / "data" / "cabanac_chatgpt.csv"


def load_entries() -> list[dict]:
    with CABANAC_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    entries = load_entries()
    print(f"[cabanac_chatgpt_checker] {len(entries)} PPS-confirmed ChatGPT-text DOIs loaded "
          f"from {CABANAC_CSV.relative_to(REPO_ROOT)}", file=sys.stderr)

    with driver.session(database=conn["database"]) as s:
        s.run(
            """
            MATCH (p:Paper)
            SET p.cabanac_chatgpt_flag = null,
                p.cabanac_chatgpt_fingerprint = null,
                p.cabanac_chatgpt_pubpeer_url = null
            """
        ).consume()

        hits = []
        for e in entries:
            rows = s.run(
                """
                MATCH (p:Paper) WHERE toLower(p.doi) = toLower($doi)
                SET p.cabanac_chatgpt_flag = true,
                    p.cabanac_chatgpt_fingerprint = $fingerprint,
                    p.cabanac_chatgpt_pubpeer_url = $pubpeer_url
                RETURN p.doi AS doi
                """,
                doi=e["doi"], fingerprint=e["fingerprint_evidence"], pubpeer_url=e["pubpeer_url"],
            ).data()
            hits.extend(rows)

        dist = s.run(
            """
            MATCH (p:Paper {is_retracted:false})
            RETURN count(*) AS n, sum(CASE WHEN p.cabanac_chatgpt_flag THEN 1 ELSE 0 END) AS with_flag
            """
        ).single()

    driver.close()

    print(f"\n=== Cabanac ChatGPT-list checker — verification ===", file=sys.stderr)
    print(f"Matched in this graph: {len(hits)} / {len(entries)} -- {[h['doi'] for h in hits]}", file=sys.stderr)
    print(f"Not-yet-retracted candidates with cabanac_chatgpt_flag: {dist['with_flag']} / {dist['n']}",
          file=sys.stderr)
    print(
        "\nNOTE: unscored review context, never part of the score -- calibrates ai_text_tell_flag_count "
        "rather than adding a second scored signal for the same phenomenon.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
