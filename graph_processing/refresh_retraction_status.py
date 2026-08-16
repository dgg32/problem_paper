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

Retraction DATE (revised 2026-08-16 -- the original assumption no longer
holds). This script used to leave retraction_date unset unconditionally, on
the empirically-tested premise that "Crossref's update-to/relation were empty
on every sampled DOI". Re-tested 2026-08-16 against the two papers this run
corrected, that premise is now false for both: Crossref's `updates:{doi}`
REVERSE lookup returns a typed, publisher-deposited `retraction` notice with a
date for each (10.1038/s41586-024-08248-5 -> notice 10.1038/s41586-026-10942-5,
2026-07-29; 10.1016/j.envres.2024.119440 -> 2024-10-01). The original test
almost certainly queried the ORIGINAL paper's own record for a forward
`relation` link, which really is a dead end -- refresh_correction_history.py
documents that exact trap at length. The reverse filter is the direction with
data, and it is the same mechanism that module already relies on.

So: a corrected paper now gets a real retraction_date + retraction_notice_doi
when Crossref has a typed retraction notice for it, and falls back to the
original honest "date unavailable" note when it does not. A date is only ever
copied from a publisher deposit, never inferred or fabricated (plan.md §0).

Still NOT done: no RETRACTED_FOR/Reason edge is created (unlike the Retraction
Watch seed papers) -- Crossref's update-to carries no controlled-vocabulary
reason, and these papers are excluded from active-candidate scoring by
is_retracted alone, same mechanism.

Idempotent: safe to re-run any time. The OpenAlex pass only touches papers
whose is_retracted flips true relative to the graph; the Crossref date pass
independently backfills any paper this script has ever corrected that is still
missing a date (retraction_status_source='openalex_recheck' AND
retraction_date IS NULL), so a paper corrected by an earlier run before this
feature existed gets its date on the next run rather than staying undated
forever.

Usage:
  python graph_processing/refresh_retraction_status.py             # check + apply
  python graph_processing/refresh_retraction_status.py --dry-run    # check only, no writes
  python graph_processing/refresh_retraction_status.py --dates-only # skip the OpenAlex
                                                                    # pass, just backfill
                                                                    # missing dates
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
CROSSREF = cfg.get("crossref", {})

# update-to types that mean "this paper was retracted". Deliberately narrow:
# expression_of_concern/correction/erratum are other sensors' facts
# (refresh_editorial_notices.py / refresh_correction_history.py) and must not
# be read as a retraction date here.
RETRACTION_UPDATE_TYPES = {"retraction", "partial_retraction", "withdrawal", "removal"}


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


def _canon_doi(doi: str) -> str:
    d = (doi or "").strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


def crossref_retraction_notice(session, limiter, doi: str) -> tuple[str | None, str | None]:
    """(retraction_date, notice_doi) from Crossref's `updates:{doi}` REVERSE
    lookup, or (None, None) if there is no typed retraction notice / the
    lookup fails.

    Reverse, not forward: a correcting/retracting notice deposits `update-to`
    pointing BACK at the paper it retracts, while the original paper's own
    `relation` field is empty in practice. refresh_correction_history.py's
    docstring documents that asymmetry in detail -- same mechanism reused here,
    just filtered to retraction-type relations instead of correction-type.

    On any transport/parse failure this returns (None, None), which means the
    paper keeps the honest "date unavailable" note. A failure must never invent
    a date (plan.md §0)."""
    canon = _canon_doi(doi)
    if not canon:
        return None, None
    limiter.wait()
    try:
        r = session.get(
            f"{CROSSREF.get('base_url', 'https://api.crossref.org')}/works",
            params={"filter": f"updates:{canon}", "rows": 20,
                    "mailto": CROSSREF.get("mailto", "")},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
    except (requests.RequestException, ValueError):
        return None, None

    best_date: str | None = None
    best_doi: str | None = None
    for it in items:
        for u in it.get("update-to", []) or []:
            if (u.get("DOI") or "").lower() != canon:
                continue
            if u.get("type") not in RETRACTION_UPDATE_TYPES:
                continue
            stamp = (u.get("updated") or {}).get("date-time") or ""
            day = stamp[:10] or None
            if not day:
                continue
            # Earliest retraction-type notice wins: if a paper picked up more
            # than one (e.g. partial then full), the first is when the
            # retraction record actually begins.
            if best_date is None or day < best_date:
                best_date, best_doi = day, (it.get("DOI") or "").lower() or None
    return best_date, best_doi


def backfill_retraction_dates(driver, conn, limiter, session, dry_run: bool) -> None:
    """Give a real, sourced retraction_date to any paper this script has
    corrected that still lacks one. Independent of the OpenAlex pass above so
    papers corrected by earlier runs (before this existed) are picked up too."""
    with driver.session(database=conn["database"]) as s:
        undated = [dict(r) for r in s.run(
            "MATCH (p:Paper) WHERE p.retraction_status_source = 'openalex_recheck' "
            "AND p.retraction_date IS NULL AND p.doi IS NOT NULL AND p.doi <> '' "
            "RETURN p.doi AS doi, p.title AS title ORDER BY p.doi"
        )]
    if not undated:
        print("\n  retraction dates: every recheck-corrected paper already has one.")
        return

    print(f"\n  retraction dates: {len(undated)} recheck-corrected paper(s) still undated "
          f"-- checking Crossref updates:{{doi}} ...")
    found = []
    for c in undated:
        day, notice = crossref_retraction_notice(session, limiter, c["doi"])
        if day:
            found.append({"doi": c["doi"], "date": day, "notice": notice, "title": c["title"]})
            print(f"    {c['doi']}  -> {day}  (notice {notice})")
        else:
            print(f"    {c['doi']}  -> no typed retraction notice in Crossref (left undated)")

    if dry_run or not found:
        if dry_run:
            print("  --dry-run: no dates written.")
        return

    with driver.session(database=conn["database"]) as s:
        for f in found:
            s.run(
                """
                MATCH (p:Paper {doi: $doi})
                SET p.retraction_date = date($date),
                    p.retraction_notice_doi = $notice,
                    p.retraction_nature = "Retraction (Crossref publisher notice)",
                    p.retraction_date_source = "crossref_update_to"
                """,
                doi=f["doi"], date=f["date"], notice=f["notice"],
            )
    print(f"  wrote a sourced retraction_date for {len(found)}/{len(undated)} paper(s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="check only, do not write to the graph")
    ap.add_argument("--dates-only", action="store_true",
                    help="skip the OpenAlex recheck; only backfill missing retraction dates")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    # Crossref politeness is tracked separately from OpenAlex's -- two different
    # hosts, two different rate budgets.
    session = requests.Session()
    crossref_limiter = RateLimiter(CROSSREF.get("requests_per_second", 10))

    if args.dates_only:
        backfill_retraction_dates(driver, conn, crossref_limiter, session, args.dry_run)
        driver.close()
        return

    with driver.session(database=conn["database"]) as s:
        candidates = [dict(r) for r in s.run(
            "MATCH (p:Paper {is_retracted:false}) RETURN p.doi AS doi, p.title AS title ORDER BY p.doi"
        )]

    print(f"  checking {len(candidates)} not-yet-retracted candidates against live OpenAlex ...")

    limiter = RateLimiter(OPENALEX.get("requests_per_second", 10))

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
        if args.dry_run:
            print("\n  --dry-run: no changes written.")
        # The date backfill is independent of this run's OpenAlex result --
        # papers corrected by EARLIER runs may still be undated, so it runs
        # even when nothing new flipped.
        backfill_retraction_dates(driver, conn, crossref_limiter, session, args.dry_run)
        driver.close()
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
    print(f"\n  corrected {len(stale)} Paper node(s): is_retracted=true")

    # Now try to give each of them (and any still-undated paper from an earlier
    # run) a real, publisher-sourced date. Papers Crossref has no typed
    # retraction notice for keep the honest "date unavailable" note set above.
    backfill_retraction_dates(driver, conn, crossref_limiter, session, args.dry_run)
    driver.close()


if __name__ == "__main__":
    main()
