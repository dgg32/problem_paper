#!/usr/bin/env python3
"""
refresh_retraction_status.py — re-check is_retracted:false candidates against
OpenAlex's live data and correct any that are now stale.

Found 2026-07-19: 21 of 834 not-yet-retracted candidates already carry
"RETRACTED" in their own title while our graph still says is_retracted=false
-- confirmed against live OpenAlex (/works/doi:<doi>) that all sampled cases
are genuinely retracted now (OpenAlex updated_date May-June 2026, before our
data pull). Root cause not fully pinned down (plausibly OpenAlex's bulk
search-filter index lagging its individual-record data at the time
expand_targets.py ran), but the actionable fact is clear: some candidates in
the triage pool are already-resolved retractions, wasting reviewer attention
and inflating tier_a_scoring.py's active pool. Title-text matching alone is
an undercount -- not every publisher prepends "RETRACTED:" to the title, so
this checks ALL is_retracted:false candidates against OpenAlex live, not just
the title-flagged 21.

What this does NOT do: invent a retraction_date or retraction_nature. Neither
OpenAlex nor Crossref exposes a reliable retraction date for these (checked
live: OpenAlex has no dedicated field, Crossref's update-to/relation were
empty on every sampled DOI) -- publishers frequently retract without
registering a formal Crossref update-to notice. So corrected papers get
is_retracted=true (a hard fact, OpenAlex-confirmed) and an honest
retraction_nature note explaining the date is unknown, rather than a
fabricated date. This intentionally leaves these papers WITHOUT a
RETRACTED_FOR/Reason edge (unlike the Retraction Watch seed papers) since we
have no controlled-vocabulary reason for them either -- they are excluded
from active-candidate scoring by is_retracted alone, same mechanism.

Idempotent: safe to re-run any time; only touches papers whose OpenAlex
is_retracted flips true relative to what the graph currently has.

Usage:
  python graph_processing/refresh_retraction_status.py             # check + apply
  python graph_processing/refresh_retraction_status.py --dry-run    # check only, no writes
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path
from threading import Lock

import requests
import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

CONFIG_PATH = REPO_ROOT / ".env.yaml"
cfg = yaml.safe_load(CONFIG_PATH.read_text())
OPENALEX = cfg.get("openalex", {})


class RateLimiter:
    def __init__(self, per_second: float):
        self.min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._last = 0.0
        self._lock = Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self.min_interval - (now - self._last)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last = time.monotonic()


def check_openalex_retracted(session, limiter, doi: str) -> bool | None:
    """Returns True/False for is_retracted, or None if the lookup failed."""
    for attempt in range(4):
        limiter.wait()
        try:
            r = session.get(
                f"{OPENALEX.get('base_url', 'https://api.openalex.org')}/works/doi:{doi}",
                params={"mailto": OPENALEX.get("mailto", "")},
                timeout=20,
            )
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json().get("is_retracted")
        if r.status_code == 404:
            return None  # not found -- leave untouched, don't guess
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        return None
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="check only, do not write to the graph")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        candidates = [dict(r) for r in s.run(
            "MATCH (p:Paper {is_retracted:false}) RETURN p.doi AS doi, p.title AS title ORDER BY p.doi"
        )]

    print(f"  checking {len(candidates)} not-yet-retracted candidates against live OpenAlex ...")

    limiter = RateLimiter(OPENALEX.get("requests_per_second", 10))
    session = requests.Session()

    stale = []
    lookup_failures = 0
    for i, cand in enumerate(candidates, 1):
        result = check_openalex_retracted(session, limiter, cand["doi"])
        if result is None:
            lookup_failures += 1
        elif result is True:
            stale.append(cand)
        if i % 100 == 0:
            print(f"  [{i}/{len(candidates)}] ({len(stale)} stale so far)", file=sys.stderr)

    print(f"\n  {len(stale)} candidate(s) are actually already retracted (OpenAlex-confirmed)")
    print(f"  {lookup_failures} DOI(s) failed to resolve against OpenAlex (left untouched)")

    if stale:
        print("\n  stale candidates:")
        for c in stale:
            print(f"    {c['doi']}  {c['title'][:80]}")

    if args.dry_run or not stale:
        driver.close()
        if args.dry_run:
            print("\n  --dry-run: no changes written.")
        return

    today = str(date.today())
    with driver.session(database=conn["database"]) as s:
        for c in stale:
            s.run(
                """
                MATCH (p:Paper {doi: $doi})
                SET p.is_retracted = true,
                    p.retraction_nature = "Retraction (date/reason unavailable from OpenAlex or Crossref)",
                    p.retraction_status_source = "openalex_recheck",
                    p.retraction_status_checked_date = $today
                """,
                doi=c["doi"],
                today=today,
            )
    driver.close()
    print(f"\n  corrected {len(stale)} Paper node(s): is_retracted=true "
          f"(retraction_date intentionally left unset -- see module docstring)")


if __name__ == "__main__":
    main()
