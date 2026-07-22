#!/usr/bin/env python3
"""
country_retraction_rate.py — Tier-A graph feature: per-country retraction
rate from EXTERNAL, unbiased sources (2026-07-22; see publisher_retraction_
rate.py's docstring for the shared "denominator problem" this family of
sensors fixes -- skill_worth_exploring.md's arxiv-2602.19197 discussion).

Same numerator/denominator split as publisher_retraction_rate.py, at country
granularity instead of publisher granularity:
  - NUMERATOR: the FULL retraction_watch.csv (71,106 records, ALL subjects,
    NOT filtered to data.seed_subset), split on its own semicolon-separated
    Country column (same "deduplicated pool per paper" shape as the CSV's
    Institution column -- see extract_enrich.py) and counted per country.
  - DENOMINATOR: OpenAlex's `works?filter=institutions.country_code:XX`
    (meta.count), i.e. total works with at least one institution in that
    country, all fields, all time.

Deliberately did NOT split this into "RW numerator + Crossref denominator"
(mirroring publisher) or "OpenAlex both sides" purity -- OpenAlex has no
per-country total-DOI endpoint as clean as Crossref's Members API, but it
does have a reliable per-country works-count filter, so it is used only for
that half. Retraction Watch remains the numerator for both sensors in this
family since it is the authoritative, hand-curated retraction record this
whole pipeline already trusts everywhere else -- more reliable than relying
on OpenAlex's own is_retracted flag, which lags/undercounts vs RW (see
sensors/external_retracted_citation_checker.py's docstring on this exact
gap).

NAME MATCHING: retraction_watch.csv's Country column uses free-text English
country names ("China", "South Korea", "Ivory Coast"); Institution.country
in our graph (and OpenAlex's country_code filter) both use ISO 3166-1 alpha-2
codes. Resolved via pycountry.countries.search_fuzzy(), which correctly
handles common-vs-official names (South Korea -> KR, Russia -> RU, Vietnam ->
VN) for the large majority of RW's 184 distinct country strings; a small
hand-checked alias table below covers the ~16 strings search_fuzzy fails on
(mostly RW's own parenthetical annotations like "Myanmar (formerly Burma)",
plus "Turkey" specifically, which pycountry's fuzzy search does not resolve
post its Türkiye rename). "Unknown" and unresolvable strings are dropped, not
guessed.

A paper's institutions can span several countries (rel_involves.tsv /
Institution.country, one row per institution). Same MAX-not-average choice
as institution_retraction_rate.py: one high-external-rate country is a real
signal even if a paper's other countries are unremarkable.

Idempotent: recomputes both Institution and Paper properties from scratch
every run. One OpenAlex call per DISTINCT Institution.country code actually
present in the graph (not per paper/institution).

Usage:
  python graph_processing/country_retraction_rate.py
"""
from __future__ import annotations

import csv
import sys
import time
from datetime import date
from functools import lru_cache
from pathlib import Path

import pycountry
import requests
import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

CONFIG_PATH = REPO_ROOT / ".env.yaml"
cfg = yaml.safe_load(CONFIG_PATH.read_text())
OPENALEX = cfg.get("openalex", {})
RW_CSV_PATH = (REPO_ROOT / cfg["data"]["retraction_watch_csv"]).resolve()

# RW country strings pycountry.search_fuzzy() cannot resolve (checked
# 2026-07-22 against all 184 distinct strings in the current csv). "Unknown"
# is deliberately absent -- it has no country and must stay unmatched.
RW_COUNTRY_ALIASES = {
    "turkey": "TR",
    "democratic republic of the congo": "CD",
    "macau": "MO",
    "brunei (brunei darussalam)": "BN",
    "myanmar (formerly burma)": "MM",
    "bosnia & herzegovina": "BA",
    "trinidad & tobago": "TT",
    "republic of the congo (congo-brazzaville)": "CG",
    "north macedonia (formerly macedonia)": "MK",
    "eswatini (formerly swaziland)": "SZ",
    "réunion island": "RE",
    "saint vincent & the grenadines": "VC",
    "st. kitts & nevis": "KN",
    "east timor": "TL",
    "gaza strip": "PS",  # approximate -- no distinct ISO code for Gaza specifically
}


@lru_cache(maxsize=None)
def rw_name_to_iso2(name: str) -> str | None:
    """Cached: only ~184 distinct country strings appear in the csv, but this
    is called once per (row, country) occurrence (~90k times) -- without
    memoizing, pycountry.countries.search_fuzzy()'s linear scan over every
    call made a full run take tens of minutes."""
    name = (name or "").strip()
    if not name or name.lower() == "unknown":
        return None
    alias = RW_COUNTRY_ALIASES.get(name.lower())
    if alias:
        return alias
    try:
        return pycountry.countries.search_fuzzy(name)[0].alpha_2
    except LookupError:
        return None


def load_rw_country_counts() -> dict[str, int]:
    """{ISO2: count of retraction records naming this country}, from the FULL
    csv -- deliberately NOT filtered to data.seed_subset (unbiased numerator,
    same reasoning as publisher_retraction_rate.py)."""
    counts: dict[str, int] = {}
    unresolved: dict[str, int] = {}
    with RW_CSV_PATH.open(encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            for raw in (row.get("Country", "") or "").split(";"):
                raw = raw.strip()
                if not raw:
                    continue
                iso2 = rw_name_to_iso2(raw)
                if iso2:
                    counts[iso2] = counts.get(iso2, 0) + 1
                elif raw.lower() != "unknown":
                    unresolved[raw] = unresolved.get(raw, 0) + 1
    if unresolved:
        print(f"  {sum(unresolved.values())} RW rows with an unresolved country name "
              f"(dropped, not guessed): {dict(sorted(unresolved.items(), key=lambda kv: -kv[1])[:10])}",
              file=sys.stderr)
    return counts


def openalex_country_total(iso2: str) -> int | None:
    try:
        r = requests.get(
            f"{OPENALEX['base_url']}/works",
            params={"filter": f"institutions.country_code:{iso2}", "per_page": 1,
                    "mailto": OPENALEX.get("mailto", "")},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("meta", {}).get("count")
    except (requests.RequestException, ValueError):
        return None


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    print("[country_retraction_rate] loading full retraction_watch.csv (unfiltered)...", file=sys.stderr)
    rw_counts = load_rw_country_counts()
    print(f"  {sum(rw_counts.values())} retraction-country records across {len(rw_counts)} countries",
          file=sys.stderr)

    with driver.session(database=conn["database"]) as s:
        graph_countries = [
            r["country"] for r in s.run(
                "MATCH (i:Institution) WHERE i.country IS NOT NULL AND i.country <> '' "
                "RETURN DISTINCT i.country AS country"
            )
        ]
    print(f"[country_retraction_rate] {len(graph_countries)} distinct countries in our graph", file=sys.stderr)

    rps = OPENALEX.get("requests_per_second", 10)
    min_interval = 1.0 / rps
    last_call = 0.0

    today = str(date.today())
    results = []
    for i, iso2 in enumerate(graph_countries, 1):
        rw_count = rw_counts.get(iso2, 0)

        wait = min_interval - (time.time() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.time()
        total_works = openalex_country_total(iso2)

        rate = (rw_count / total_works) if (total_works and rw_count) else None
        results.append({"country": iso2, "rate": rate, "rw_count": rw_count, "total_works": total_works})
        if i % 20 == 0:
            print(f"  [{i}/{len(graph_countries)}]", file=sys.stderr)

    with driver.session(database=conn["database"]) as s:
        for r in results:
            s.run(
                """
                MATCH (i:Institution {country: $country})
                SET i.country_retr_rate = $rate,
                    i.country_retr_count = $rw_count,
                    i.country_total_works = $total_works,
                    i.country_rate_checked_date = $today
                """,
                country=r["country"], rate=r["rate"], rw_count=r["rw_count"],
                total_works=r["total_works"], today=today,
            )

        print("[country_retraction_rate] per-paper max across involved institutions' countries...", file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)
            SET p.country_retr_rate = null,
                p.country_retr_rate_name = null,
                p.country_retr_rate_n = null
            """
        ).consume()
        s.run(
            """
            MATCH (p:Paper)-[:INVOLVES]->(i:Institution)
            WHERE i.country_retr_rate IS NOT NULL
            WITH p, i ORDER BY i.country_retr_rate DESC
            WITH p, collect({name: i.country, rate: i.country_retr_rate, n: i.country_retr_count})[0] AS top
            SET p.country_retr_rate = top.rate,
                p.country_retr_rate_name = top.name,
                p.country_retr_rate_n = top.n
            """
        ).consume()

        dist = s.run(
            """
            MATCH (p:Paper {is_retracted:false})
            RETURN count(*) AS n,
                   sum(CASE WHEN p.country_retr_rate IS NOT NULL THEN 1 ELSE 0 END) AS with_signal,
                   avg(p.country_retr_rate) AS avg_rate
            """
        ).single()

    driver.close()

    print("\n=== Country retraction rate — verification ===", file=sys.stderr)
    print(f"Not-yet-retracted candidates with a country_retr_rate: "
          f"{dist['with_signal']} / {dist['n']}"
          + (f"  (avg {dist['avg_rate']:.5f})" if dist['avg_rate'] is not None else ""),
          file=sys.stderr)

    matched = [r for r in results if r["rate"] is not None]
    matched.sort(key=lambda r: r["rate"], reverse=True)
    print(f"\nMatched {len(matched)} / {len(results)} graph countries to an OpenAlex total + RW count.",
          file=sys.stderr)
    print("\nAll countries by external retraction rate:", file=sys.stderr)
    for r in matched:
        print(f"  {r['rate']:.4%}  rw={r['rw_count']:<5} total_works={r['total_works']:<10} {r['country']}",
              file=sys.stderr)

    unmatched = [r for r in results if r["rate"] is None]
    if unmatched:
        print(f"\n{len(unmatched)} countries left WITHOUT a rate (no RW rows and/or OpenAlex lookup failed "
              f"-- country_retr_rate left null, not silently 0): "
              f"{[r['country'] for r in unmatched]}", file=sys.stderr)


if __name__ == "__main__":
    main()
