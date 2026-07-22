#!/usr/bin/env python3
"""
journal_retraction_rate_external.py — Tier-A graph feature: per-journal
retraction rate from EXTERNAL, unbiased sources, computed the SAME way as
publisher_retraction_rate.py (2026-07-22, same session, same request: "can we
calculate the Journal retraction rate the same way as publisher_retr_rate?").

THE BIAS THIS FIXES: the existing `journal_retr_rate` (computed inline in
gds_node_classification.py) uses ONLY the papers already in our own graph --
a Retraction-Watch-seeded, microbiology-scoped subset -- so a journal
connected mostly to seed-retracted papers can show a ~100% "rate" that says
nothing about its true real-world retraction rate. This sensor does NOT
replace that field (kept as-is, still useful as a graph-internal signal,
same as institution_retr_rate) -- it adds a second, external-scale one,
named `journal_retr_rate_external` to avoid any collision/confusion:
  - NUMERATOR: the FULL retraction_watch.csv (71,106 records, ALL subjects --
    NOT filtered to data.seed_subset), grouped by its own Journal column.
    Unlike the Publisher/Institution columns, Journal is a single value per
    row (one journal per paper), not semicolon-list-valued.
  - DENOMINATOR: Crossref's public Journals API (`GET /journals?query=<name>`
    or `GET /journals/{issn}`), which returns each journal's `counts.total-
    dois` -- confirmed live 2026-07-22 (e.g. Cell, ISSN 0092-8674: total-dois
    26,637). Free, keyless (mailto for the polite pool only).

NAME MATCHING: much better-behaved than publisher_retraction_rate.py's
problem, because journal titles are far more standardized across sources
than publisher legal names -- a direct normalized-exact-match against the
full csv's own Journal column already resolves 541/636 (85%) of this
graph's journals with zero fuzzy logic. The remaining ~15% fall back to the
same bidirectional-substring approach as publisher matching. Crossref's own
Journals API search has the same relevance-ranking quirks documented in
publisher_retraction_rate.py (a bare, very common title like "Cell" can fail
to surface the real journal in the top results at all, even though it exists
in Crossref under its own ISSN -- confirmed live) -- mitigated the same way:
retry-on-empty with a first-significant-token query, a STRICT token-subset
match (every significant word of the query must appear in the candidate's
title, not just one shared word), preferring the largest-total-dois
candidate among those that pass, and a floor + impossible-rate (>100%)
discard. See publisher_retraction_rate.py's docstring for the full
reasoning -- this file reuses the identical algorithm, just against the
Journals API instead of the Members API.

Absence of a confident match is stored as `null`, never a guessed `0.0`
(plan.md §0).

Idempotent: recomputes both Journal and Paper properties from scratch every
run. One Crossref call per DISTINCT Journal.name in the graph (not per
paper).

Usage:
  python graph_processing/journal_retraction_rate_external.py
"""
from __future__ import annotations

import csv
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

CONFIG_PATH = REPO_ROOT / ".env.yaml"
cfg = yaml.safe_load(CONFIG_PATH.read_text())
CROSSREF = cfg.get("crossref", {})
RW_CSV_PATH = (REPO_ROOT / cfg["data"]["retraction_watch_csv"]).resolve()

_PAREN_RE = re.compile(r"\([^)]*\)")
_PUNCT_RE = re.compile(r"[^\w\s]")
MIN_MATCH_LEN = 4
MIN_TOTAL_DOIS = 1000
LOW_CONFIDENCE_TOTAL_DOIS = 10_000
# See publisher_retraction_rate.py's docstring for why "press"/"media" are
# deliberately NOT stopwords (they can be the one distinguishing word).
_STOPWORDS = {
    "the", "and", "of", "group", "publishing", "publishers", "publications",
    "ltd", "limited", "inc", "llc", "corporation", "corp", "co",
    "company", "gmbh", "kg", "bv", "sa", "ag", "plc", "technologies",
    "international", "science", "sciences", "institute", "journal",
}


def normalize(name: str) -> str:
    if not name:
        return ""
    n = _PAREN_RE.sub(" ", name)
    n = n.replace("&", " and ")
    n = _PUNCT_RE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip().lower()
    return n


def load_rw_journal_counts() -> dict[str, int]:
    """{normalized RW Journal string: count of retraction records}, from the
    FULL csv -- deliberately NOT filtered to data.seed_subset."""
    counts: dict[str, int] = {}
    with RW_CSV_PATH.open(encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            key = normalize(row.get("Journal", ""))
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def match_journal(graph_journal: str, rw_counts: dict[str, int]) -> int:
    """Exact normalized match first (resolves ~85% directly); else sum RW
    counts for every normalized RW key that bidirectionally substring-
    matches this graph journal's normalized name."""
    g = normalize(graph_journal)
    if len(g) < MIN_MATCH_LEN:
        return 0
    if g in rw_counts:
        return rw_counts[g]
    total = 0
    for rw_key, n in rw_counts.items():
        if len(rw_key) < MIN_MATCH_LEN:
            continue
        if rw_key in g or g in rw_key:
            total += n
    return total


def _tokens(name: str) -> set[str]:
    return {t for t in normalize(name).split() if t not in _STOPWORDS and len(t) >= 4}


def _crossref_query(query: str) -> list[dict]:
    r = requests.get(
        f"{CROSSREF['base_url']}/journals",
        params={"query": query, "rows": 10, "mailto": CROSSREF.get("mailto", "")},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("message", {}).get("items", [])


def lookup_crossref_journal(name: str) -> dict | None:
    try:
        items = list(_crossref_query(name))
        toks = [t for t in name.replace("&", " and ").split() if t.lower().strip(".,") not in _STOPWORDS]
        if toks:
            items += _crossref_query(toks[0])
    except (requests.RequestException, ValueError):
        return None
    if not items:
        return None

    qn = normalize(name)
    for it in items:
        if normalize(it.get("title", "")) == qn:
            return it

    qtok = _tokens(name)
    if not qtok:
        return None
    candidates = [it for it in items if qtok <= _tokens(it.get("title", ""))]
    if not candidates:
        return None
    candidates.sort(key=lambda it: it.get("counts", {}).get("total-dois", 0), reverse=True)
    best = candidates[0]
    if best.get("counts", {}).get("total-dois", 0) < MIN_TOTAL_DOIS:
        return None
    return best


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    print("[journal_retraction_rate_external] loading full retraction_watch.csv (unfiltered)...", file=sys.stderr)
    rw_counts = load_rw_journal_counts()
    print(f"  {sum(rw_counts.values())} retraction records across {len(rw_counts)} distinct journal strings",
          file=sys.stderr)

    with driver.session(database=conn["database"]) as s:
        graph_journals = [
            r["name"] for r in s.run(
                "MATCH (j:Journal) WHERE j.name IS NOT NULL AND j.name <> '' RETURN DISTINCT j.name AS name"
            )
        ]
    print(f"[journal_retraction_rate_external] {len(graph_journals)} distinct journals in our graph", file=sys.stderr)

    rps = CROSSREF.get("requests_per_second", 10)
    min_interval = 1.0 / rps
    last_call = 0.0

    today = str(date.today())
    results = []
    unmatched = []
    for i, jname in enumerate(graph_journals, 1):
        rw_count = match_journal(jname, rw_counts)

        wait = min_interval - (time.time() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.time()
        member = lookup_crossref_journal(jname)

        if member is None or rw_count == 0:
            unmatched.append((jname, rw_count, member.get("title") if member else None))
            results.append({
                "journal": jname, "rate": None, "rw_count": rw_count,
                "total_dois": None, "crossref_title": None, "crossref_issn": None,
                "low_confidence": None,
            })
            continue

        total_dois = member.get("counts", {}).get("total-dois", 0)
        rate = (rw_count / total_dois) if total_dois else None
        if rate is not None and rate > 1.0:
            unmatched.append((jname, rw_count, f"{member.get('title')} (discarded: implausible rate {rate:.1%})"))
            results.append({
                "journal": jname, "rate": None, "rw_count": rw_count,
                "total_dois": total_dois, "crossref_title": None, "crossref_issn": None,
                "low_confidence": None,
            })
            continue
        issn = (member.get("ISSN") or [None])[0]
        results.append({
            "journal": jname,
            "rate": rate,
            "rw_count": rw_count,
            "total_dois": total_dois,
            "crossref_title": member.get("title"),
            "crossref_issn": issn,
            "low_confidence": total_dois < LOW_CONFIDENCE_TOTAL_DOIS,
        })
        if i % 50 == 0:
            print(f"  [{i}/{len(graph_journals)}]", file=sys.stderr)

    with driver.session(database=conn["database"]) as s:
        for r in results:
            s.run(
                """
                MATCH (j:Journal {name: $journal})
                SET j.journal_retr_rate_external = $rate,
                    j.journal_retr_count_external = $rw_count,
                    j.journal_total_dois = $total_dois,
                    j.journal_crossref_title = $crossref_title,
                    j.journal_crossref_issn = $crossref_issn,
                    j.journal_low_confidence = $low_confidence,
                    j.journal_rate_checked_date = $today
                """,
                journal=r["journal"], rate=r["rate"], rw_count=r["rw_count"],
                total_dois=r["total_dois"], crossref_title=r["crossref_title"],
                crossref_issn=r["crossref_issn"], low_confidence=r["low_confidence"], today=today,
            )

        print("[journal_retraction_rate_external] per-paper copy from paper's own journal...", file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)
            SET p.journal_retr_rate_external = null,
                p.journal_retr_rate_external_n = null,
                p.journal_retr_rate_external_low_confidence = null
            """
        ).consume()
        s.run(
            """
            MATCH (p:Paper)-[:PUBLISHED_IN]->(j:Journal)
            WHERE j.journal_retr_rate_external IS NOT NULL
            WITH p, j ORDER BY j.journal_retr_rate_external DESC
            WITH p, collect({rate: j.journal_retr_rate_external, n: j.journal_retr_count_external,
                              low_conf: j.journal_low_confidence})[0] AS top
            SET p.journal_retr_rate_external = top.rate,
                p.journal_retr_rate_external_n = top.n,
                p.journal_retr_rate_external_low_confidence = top.low_conf
            """
        ).consume()

        dist = s.run(
            """
            MATCH (p:Paper {is_retracted:false})
            RETURN count(*) AS n,
                   sum(CASE WHEN p.journal_retr_rate_external IS NOT NULL THEN 1 ELSE 0 END) AS with_signal,
                   avg(p.journal_retr_rate_external) AS avg_rate
            """
        ).single()

    driver.close()

    print("\n=== Journal retraction rate (external) — verification ===", file=sys.stderr)
    print(f"Not-yet-retracted candidates with a journal_retr_rate_external: "
          f"{dist['with_signal']} / {dist['n']}"
          + (f"  (avg {dist['avg_rate']:.4f})" if dist['avg_rate'] is not None else ""),
          file=sys.stderr)

    matched = [r for r in results if r["rate"] is not None]
    matched.sort(key=lambda r: r["rate"], reverse=True)
    print(f"\nMatched {len(matched)} / {len(results)} graph journals to a Crossref journal + RW count.", file=sys.stderr)
    print("\nTop 15 journals by external retraction rate:", file=sys.stderr)
    for r in matched[:15]:
        print(f"  {r['rate']:.2%}  rw={r['rw_count']:<5} total_dois={r['total_dois']:<10} "
              f"{r['journal']} -> {r['crossref_title']} ({r['crossref_issn']})", file=sys.stderr)

    if unmatched:
        print(f"\n{len(unmatched)} journal(s) left WITHOUT a rate (no RW match and/or no confident "
              f"Crossref match -- journal_retr_rate_external left null, not silently 0):", file=sys.stderr)
        for jname, rw_count, crossref_title in unmatched[:20]:
            print(f"  {jname}  (rw_count={rw_count}, crossref_match={crossref_title})", file=sys.stderr)


if __name__ == "__main__":
    main()
