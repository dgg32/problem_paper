#!/usr/bin/env python3
"""
author_retraction_rate_external.py — Tier-A graph feature: a FIRST or LAST
author's own personal retraction rate, from EXTERNAL, unbiased sources
(2026-07-22, same "denominator problem" family as publisher_retraction_
rate.py / country_retraction_rate.py / journal_retraction_rate_external.py --
see that docstring for the shared background).

WHY FIRST/LAST ONLY, NOT EVERY CO-AUTHOR: the user's own framing for this
request -- the first author typically did the hands-on work, the last
author is conventionally the senior figure who supervised/vouches for it;
a middle author's role is far more variable and less attributable.
WROTE.author_position ('first'/'last'/'middle', from extract_enrich.py's
parse of each paper's author list order) already carries this. Restricting
scope here is a deliberate choice to keep this closer to "direct
responsibility" evidence and away from coauthor_other_misconduct's already-
covered "shares a cluster with SOMEONE who..." ecological territory.

WHY THIS NEEDS A DIFFERENT RECIPE THAN JOURNAL/PUBLISHER/COUNTRY: those three
match a NAME (journal title, publisher name, country) against
retraction_watch.csv's own free-text columns -- workable because there are
only a few hundred/thousand distinct venues, each fairly unambiguous. A
person's name has none of that safety: retraction_watch.csv's Author column
is bare semicolon-separated free text with NO identifier at all (no ORCID),
so matching e.g. "Wei Li" against it would silently pool an unknown number
of different, unrelated people who happen to share a common name -- a much
higher-stakes mistake than mismatching a journal, since it wrongly
implicates one specific named individual. That risk is why this sensor is
scoped ONLY to authors who have an ORCID on file (AuthorInstance.orcid /
.has_orcid -- already used elsewhere in this pipeline, e.g. orcid_client.py's
ORCID-vs-OpenAlex mis-assignment cross-check) and resolves BOTH numerator and
denominator by DOI, never by name:
  - DENOMINATOR: the ORCID Public API's own /works endpoint for that exact
    ORCID (orcid_client.py's get_claimed_dois()) -- the person's own
    self-declared, globally-unique list of DOIs.
  - NUMERATOR: of those claimed DOIs, how many appear in the FULL
    retraction_watch.csv's OriginalPaperDOI column (71,106 records, ALL
    subjects, NOT filtered to data.seed_subset) -- a DOI-to-DOI set
    intersection, not a name match, so it inherits none of the
    journal/publisher-style ambiguity. As a side effect the rate can never
    exceed 100% by construction (the numerator is a strict subset of the
    denominator) -- unlike the journal/publisher sensors, no ">1.0
    implausible rate" discard logic is needed here.

Authors WITHOUT an orcid on file are left with a null rate -- never guessed
via name matching, never silently 0 (plan.md §0). Measured 2026-07-22: of
this graph's first/last-author instances, 1,359/2,067 (66%) first authors
and 1,620/1,994 (81%) last authors carry an orcid; 1,994 distinct ORCIDs
across both positions.

FLOOR: an author with only 1-2 total claimed works produces a meaningless,
noisy rate (one retraction out of one paper = "100%"), so a rate is only
computed once the ORCID record claims >= MIN_CLAIMED_WORKS DOIs; below
LOW_CONFIDENCE_CLAIMED_WORKS the rate is still shown but flagged
low-confidence (same idiom as publisher/journal's LOW_CONFIDENCE_TOTAL_DOIS).

NOT capped, unlike publisher/country/journal_retr_rate_external: those three
needed a hard ceiling because their rates are ~0.01%-1% typical vs 8%-26%
extreme-tail, forcing a weight multiplier (100x-1000x) that would otherwise
let the tail explode. An author's personal rate is already on the same
[0,1] percentage SCALE as institution_retr_rate (typical single digits to
tens of percent) -- no multiplier is needed, so no cap is needed either; see
tier_a_scoring.py's WEIGHTS comment for why it sits in that "graph feature"
tier instead. Still ecological-ish, not direct, per plan.md §0 (this
person's OTHER papers ≠ this paper's guilt) -- same caveat family as
coauthor_other_misconduct -- so it is kept at a comparably modest weight,
not the top tier (ori_finding_flag / paperconan_confirmed).

Idempotent: recomputes AuthorInstance and Paper properties from scratch
every run. One ORCID API call per DISTINCT first/last-author ORCID in the
graph, not per paper.

Usage:
  python graph_processing/author_retraction_rate_external.py
"""
from __future__ import annotations

import csv
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
from orcid_client import OrcidClient, canon_doi  # noqa: E402

CONFIG_PATH = REPO_ROOT / ".env.yaml"
cfg = yaml.safe_load(CONFIG_PATH.read_text())
ORCID_CFG = cfg.get("orcid", {})
RW_CSV_PATH = (REPO_ROOT / cfg["data"]["retraction_watch_csv"]).resolve()

MIN_CLAIMED_WORKS = 3
LOW_CONFIDENCE_CLAIMED_WORKS = 10


def load_rw_doi_set() -> set[str]:
    """Every OriginalPaperDOI in the FULL retraction_watch.csv, canonicalized
    -- deliberately NOT filtered to data.seed_subset (unbiased, same
    reasoning as publisher_retraction_rate.py). DOI-to-DOI matching only --
    no name matching anywhere in this sensor."""
    dois: set[str] = set()
    with RW_CSV_PATH.open(encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            d = canon_doi(row.get("OriginalPaperDOI", ""))
            if d:
                dois.add(d)
    return dois


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    print("[author_retraction_rate_external] loading full retraction_watch.csv (unfiltered)...", file=sys.stderr)
    rw_dois = load_rw_doi_set()
    print(f"  {len(rw_dois)} distinct retracted DOIs", file=sys.stderr)

    with driver.session(database=conn["database"]) as s:
        orcids = [
            r["orcid"] for r in s.run(
                """
                MATCH (a:AuthorInstance)-[w:WROTE]->(:Paper)
                WHERE w.author_position IN ['first','last'] AND a.has_orcid = true
                  AND a.orcid IS NOT NULL AND a.orcid <> ''
                RETURN DISTINCT a.orcid AS orcid
                """
            )
        ]
    print(f"[author_retraction_rate_external] {len(orcids)} distinct ORCIDs among first/last authors", file=sys.stderr)

    client = OrcidClient(cfg)
    rps = ORCID_CFG.get("requests_per_second", 8)
    min_interval = 1.0 / rps
    last_call = 0.0

    today = str(date.today())
    results = []
    errors = 0
    for i, orcid in enumerate(orcids, 1):
        wait = min_interval - (time.time() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.time()
        try:
            claimed = client.get_claimed_dois(orcid)
        except (requests.RequestException, ValueError, KeyError) as e:
            errors += 1
            results.append({"orcid": orcid, "rate": None, "n_retracted": 0, "n_total": None, "low_confidence": None})
            if errors <= 10:
                print(f"  [{i}/{len(orcids)}] ORCID {orcid}: lookup failed ({e})", file=sys.stderr)
            continue

        total = len(claimed)
        if total < MIN_CLAIMED_WORKS:
            results.append({"orcid": orcid, "rate": None, "n_retracted": 0, "n_total": total, "low_confidence": None})
            continue

        retracted = [d for d in claimed if d in rw_dois]
        rate = len(retracted) / total
        results.append({
            "orcid": orcid, "rate": rate, "n_retracted": len(retracted), "n_total": total,
            "low_confidence": total < LOW_CONFIDENCE_CLAIMED_WORKS,
        })
        if i % 50 == 0:
            print(f"  [{i}/{len(orcids)}]", file=sys.stderr)

    with driver.session(database=conn["database"]) as s:
        for r in results:
            s.run(
                """
                MATCH (a:AuthorInstance {orcid: $orcid})
                SET a.author_retr_rate_external = $rate,
                    a.author_retr_count_external = $n_retracted,
                    a.author_total_claimed_works = $n_total,
                    a.author_low_confidence = $low_confidence,
                    a.author_rate_checked_date = $today
                """,
                orcid=r["orcid"], rate=r["rate"], n_retracted=r["n_retracted"],
                n_total=r["n_total"], low_confidence=r["low_confidence"], today=today,
            )

        print("[author_retraction_rate_external] per-paper max across first/last authors...", file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)
            SET p.author_retr_rate_external = null,
                p.author_retr_rate_external_n = null,
                p.author_retr_rate_external_total = null,
                p.author_retr_rate_external_name = null,
                p.author_retr_rate_external_position = null,
                p.author_retr_rate_external_low_confidence = null
            """
        ).consume()
        s.run(
            """
            MATCH (a:AuthorInstance)-[w:WROTE]->(p:Paper)
            WHERE w.author_position IN ['first','last']
              AND a.author_retr_rate_external IS NOT NULL
            WITH p, a, w ORDER BY a.author_retr_rate_external DESC
            WITH p, collect({rate: a.author_retr_rate_external, n: a.author_retr_count_external,
                              total: a.author_total_claimed_works, name: a.name,
                              position: w.author_position,
                              low_conf: a.author_low_confidence})[0] AS top
            SET p.author_retr_rate_external = top.rate,
                p.author_retr_rate_external_n = top.n,
                p.author_retr_rate_external_total = top.total,
                p.author_retr_rate_external_name = top.name,
                p.author_retr_rate_external_position = top.position,
                p.author_retr_rate_external_low_confidence = top.low_conf
            """
        ).consume()

        dist = s.run(
            """
            MATCH (p:Paper {is_retracted:false})
            RETURN count(*) AS n,
                   sum(CASE WHEN p.author_retr_rate_external IS NOT NULL THEN 1 ELSE 0 END) AS with_signal,
                   avg(p.author_retr_rate_external) AS avg_rate
            """
        ).single()

    driver.close()

    print("\n=== Author retraction rate (external, first/last only) — verification ===", file=sys.stderr)
    print(f"Not-yet-retracted candidates with an author_retr_rate_external: "
          f"{dist['with_signal']} / {dist['n']}"
          + (f"  (avg {dist['avg_rate']:.4f})" if dist['avg_rate'] is not None else ""),
          file=sys.stderr)

    matched = [r for r in results if r["rate"] is not None]
    matched.sort(key=lambda r: r["rate"], reverse=True)
    skipped_floor = sum(1 for r in results if r["rate"] is None and r["n_total"] is not None and r["n_total"] < MIN_CLAIMED_WORKS)
    print(f"\n{len(matched)} / {len(results)} ORCIDs produced a rate "
          f"({skipped_floor} below the {MIN_CLAIMED_WORKS}-claimed-works floor, {errors} lookup errors).",
          file=sys.stderr)
    print("\nTop 15 authors by personal external retraction rate:", file=sys.stderr)
    for r in matched[:15]:
        print(f"  {r['rate']:.1%}  {r['n_retracted']}/{r['n_total']} claimed works retracted"
              f"{' (low confidence)' if r['low_confidence'] else ''}  orcid={r['orcid']}", file=sys.stderr)


if __name__ == "__main__":
    main()
