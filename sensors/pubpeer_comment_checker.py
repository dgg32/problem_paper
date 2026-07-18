#!/usr/bin/env python3
"""
pubpeer_comment_checker.py — Phase 4 sensor #3 (plan.md).

plan.md's own framing: "Comments there are the single highest-yield human
signal." That framing turns out to be load-bearing, not just color — PubPeer
has no public, keyless, structured API, and its robots.txt explicitly
disallows automated /search access for all bots (and separately names and
blocks ClaudeBot specifically):

    User-agent: *
    Crawl-delay: 10
    Disallow: /search

The only documented programmatic path is a keyed API (api.pubpeer.com,
`?devkey=...`) obtained by directly contacting PubPeer (pubpeer.com/contact).
No key is configured for this project. Confirmed 2026-07-18: no working
keyless endpoint exists that returns structured per-DOI comment data.

So this sensor is deliberately a **router, not a scraper**: for each
candidate paper it emits a pointer (the direct pubpeer.com/search?q=<doi>
URL) for a human reviewer to open themselves — exactly matching Phase 5's
"reviewer decision" workflow. It never fetches pubpeer.com content.

If a devkey is later obtained (add it to .env.yaml under `pubpeer.devkey`),
`check_doi_via_api()` is a stub to fill in against PubPeer's real documented
schema at that time — do not guess at the schema without their docs in hand.

Severity is deliberately not comparable to the other sensors: every
not-yet-retracted candidate gets a manual_check_required pointer (there is
no automated basis to discriminate between papers), so this must never be
summed into a weighted score the way retracted_citation/reference_integrity/
journal_integrity/ai_text_tell are (see graph_processing/tier_a_scoring.py
WEIGHTS) -- it would inflate every candidate's score identically. Kept out
of WEIGHTS on purpose.

Usage:
  python sensors/pubpeer_comment_checker.py                  # all candidates -> report
  python sensors/pubpeer_comment_checker.py --doi 10.xxx/xxx  # single paper, stdout
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote

import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

CONFIG_PATH = REPO_ROOT / ".env.yaml"
REPORT_JSON = REPO_ROOT / "data" / "flags" / "pubpeer_flags.json"

cfg = yaml.safe_load(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
PUBPEER = cfg.get("pubpeer", {}) or {}


def search_url(doi: str) -> str:
    return f"https://pubpeer.com/search?q={quote(doi)}"


def check_doi_via_api(doi: str) -> dict | None:
    """
    Stub for the keyed PubPeer API path. Not implemented: PubPeer's schema for
    this endpoint is not publicly documented; the only confirmed shape found
    (api.pubpeer.com/v1/publications/dump/{page}?devkey=...) is a paginated
    dump of ALL commented publications, not a per-DOI lookup, which is
    impractical to page through blindly. Fill this in against PubPeer's own
    docs once `pubpeer.devkey` is set in .env.yaml and they've confirmed the
    real per-DOI (or filterable) endpoint shape.
    """
    raise NotImplementedError(
        "PubPeer devkey configured but check_doi_via_api() is unimplemented — "
        "confirm the real endpoint schema with PubPeer before wiring this up."
    )


def check_paper(doi: str, title: str | None = None) -> dict:
    """
    Assess a single paper. Returns a router record (never a fetched verdict)
    unless a devkey is configured and the API stub above has been filled in.
    """
    if PUBPEER.get("devkey"):
        try:
            result = check_doi_via_api(doi)
            if result is not None:
                return result
        except NotImplementedError as exc:
            print(f"  WARNING: {exc}", file=sys.stderr)

    return {
        "flag": "pubpeer_manual_check",
        "status": "manual_check_required",
        "severity": "info",  # not a fraud signal by itself -- a routing pointer
        "reason": (
            "PubPeer has no public keyless API and disallows automated /search "
            "access via robots.txt; a human reviewer should open the link and "
            "note whether comments exist."
        ),
        "paper_doi": doi,
        "paper_title": title,
        "check_url": search_url(doi),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", help="check a single paper by DOI (stdout only)")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    if args.doi:
        with driver.session(database=conn["database"]) as s:
            row = s.run(
                "MATCH (p:Paper {doi: $doi}) RETURN p.title AS title",
                doi=args.doi,
            ).single()
        driver.close()
        title = row["title"] if row else None
        result = check_paper(args.doi, title)
        print(json.dumps(result, indent=2))
        return

    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(
            "MATCH (p:Paper {is_retracted:false}) RETURN p.doi AS doi, p.title AS title"
        )]
    driver.close()

    all_flags = [check_paper(r["doi"], r["title"]) for r in rows]

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(all_flags, indent=2))

    print("\n=== pubpeer-comment-checker ===")
    print(f"  papers routed for manual check : {len(all_flags)}")
    print(f"  report written -> {REPORT_JSON.relative_to(REPO_ROOT)}")
    print(
        "\n  NOTE: this sensor never auto-verdicts a paper (see module docstring "
        "for why). Each record is a pubpeer.com/search link for a human reviewer."
    )


if __name__ == "__main__":
    main()
