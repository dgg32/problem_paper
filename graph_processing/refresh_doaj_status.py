#!/usr/bin/env python3
"""
refresh_doaj_status.py — replace journal_integrity_check.py's static, hand-
curated PREDATORY_PUBLISHERS guess-list with live DOAJ index membership.

Why this is a real improvement, not just "more data": the old list flagged
EVERY journal from a handful of publisher names (MDPI, Frontiers, PLOS ONE,
...) with a blanket "known for compromised peer review" severity -- unfair to
the many properly-vetted journals those publishers run, and blind to genuinely
bad journals from publishers not on the static list. DOAJ membership is a
per-journal, continuously-maintained fact (23,075 journals as of this
snapshot, each individually vetted against DOAJ's editorial/ethical criteria),
so checking the SPECIFIC journal replaces a publisher-name guess with a real
per-journal signal.

Design: DOAJ absence is only meaningful for journals that are actually
open-access (a subscription journal is correctly absent from DOAJ and that
means nothing). So the flag fires only for journals published by known
primarily-OA publishers (the same set the old PREDATORY_PUBLISHERS constant
targeted) that are NOT found in DOAJ's active index -- i.e. "this OA-only
publisher's journal isn't even properly indexed," which is a real, narrower,
more defensible signal than "this journal is from MDPI, therefore suspect."

Data source: DOAJ's official public Journal CSV data dump
(https://doaj.org/csv, https://doaj.org/docs/public-data-dump/), explicitly
published by DOAJ for third-party reuse, updated weekly. NOTE (compliance,
2026-07-19): doaj.org's robots.txt names and blocks ClaudeBot site-wide with
no path exception for this designated bulk-download file -- flagged to the
user before fetching; explicit go-ahead given to fetch it anyway, reasoning
that a one-time designated-for-reuse bulk download differs from the live-site
crawling the block more plausibly targets. Re-download manually
(`curl -sL https://doaj.org/csv -o data/doaj_journals.csv`) rather than having
this script re-fetch it automatically, so that judgment call is never made
silently on a future run.

ISSN matching: our Journal nodes carry no ISSN (checked live: 0/634). ISSNs
are pulled from OpenAlex's Sources API (keyless, title search, one call per
journal) since that's the same enrichment source already used throughout this
project.

Fields written (facts, not verdicts -- plan.md §0):
  doaj_indexed        : bool -- is this exact journal (by ISSN or exact
                         normalized title) in DOAJ's current active index
  doaj_issn            : the matched ISSN, if any
  doaj_checked_date    : provenance

journal_integrity_check.py is updated separately to use doaj_indexed instead
of the static PREDATORY_PUBLISHERS title-substring guess.

Usage:
  python graph_processing/refresh_doaj_status.py --doaj-csv data/doaj_journals.csv
"""
from __future__ import annotations

import argparse
import csv
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


def canon_title(t: str) -> str:
    return " ".join((t or "").lower().split())


def canon_issn(i: str) -> str:
    return (i or "").strip().upper()


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


def load_doaj_index(csv_path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """Returns (by_issn, by_title)."""
    by_issn: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            title = row.get("Journal title", "")
            print_issn = canon_issn(row.get("Journal ISSN (print version)", ""))
            eissn = canon_issn(row.get("Journal EISSN (online version)", ""))
            rec = {"title": title, "print_issn": print_issn, "eissn": eissn}
            if print_issn:
                by_issn[print_issn] = rec
            if eissn:
                by_issn[eissn] = rec
            if title:
                by_title[canon_title(title)] = rec
    return by_issn, by_title


def fetch_openalex_issn(session, limiter, journal_name: str) -> str | None:
    limiter.wait()
    try:
        r = session.get(
            "https://api.openalex.org/sources",
            params={"search": journal_name, "mailto": OPENALEX.get("mailto", ""), "per-page": 1},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        results = r.json().get("results", [])
        if not results:
            return None
        return results[0].get("issn_l")
    except requests.RequestException:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doaj-csv", required=True, help="path to DOAJ's downloaded Journal CSV")
    args = ap.parse_args()

    doaj_by_issn, doaj_by_title = load_doaj_index(Path(args.doaj_csv))
    print(f"  loaded {len(doaj_by_title)} DOAJ journals (title index), {len(doaj_by_issn)} ISSN entries")

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        journals = [dict(r) for r in s.run("MATCH (j:Journal) RETURN j.name AS name")]
    print(f"  checking {len(journals)} journals in our graph")

    session = requests.Session()
    limiter = RateLimiter(OPENALEX.get("requests_per_second", 10))

    results = []
    for i, j in enumerate(journals, 1):
        name = j["name"]
        if not name:
            continue
        title_hit = doaj_by_title.get(canon_title(name))
        if title_hit:
            results.append({"name": name, "indexed": True, "issn": title_hit["print_issn"] or title_hit["eissn"]})
        else:
            issn = fetch_openalex_issn(session, limiter, name)
            issn_hit = doaj_by_issn.get(canon_issn(issn)) if issn else None
            results.append({"name": name, "indexed": bool(issn_hit), "issn": issn})
        if i % 100 == 0:
            print(f"  [{i}/{len(journals)}]", file=sys.stderr)

    indexed_count = sum(1 for r in results if r["indexed"])
    print(f"\n  {indexed_count}/{len(results)} journals are DOAJ-indexed")

    today = str(date.today())
    with driver.session(database=conn["database"]) as s:
        for r in results:
            s.run(
                "MATCH (j:Journal {name:$name}) SET j.doaj_indexed=$indexed, j.doaj_issn=$issn, j.doaj_checked_date=$today",
                name=r["name"], indexed=r["indexed"], issn=r["issn"], today=today,
            )
    driver.close()
    print(f"  wrote doaj_indexed for {len(results)} journal(s)")


if __name__ == "__main__":
    main()
