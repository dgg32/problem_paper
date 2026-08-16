#!/usr/bin/env python3
"""
author_retraction_rate_external.py — Tier-A graph feature: a FIRST or LAST
author's own retraction history, from EXTERNAL, unbiased sources (2026-07-22,
same "denominator problem" family as publisher_retraction_rate.py /
country_retraction_rate.py / journal_retraction_rate_external.py -- see that
docstring for the shared background).

MERGED 2026-07-22 (user request) with what used to be a separate signal,
coauthor_other_misconduct: that field is graph-internal, cluster-matched
(fuzzy "probable person," not ORCID), and counts ANY co-author regardless of
position. This sensor is the ORCID-strict, first/last-only, EXTERNAL half of
the merged model; the middle-author, fuzzy-cluster half now lives in
coauthor_retraction_severity.py. The two are combined only at scoring time
(tier_a_scoring.py's MINMAX_KEYS: fl_any_count/fl_misconduct_count here,
mid_any_count/mid_misconduct_count from the other script) -- kept as separate
sensors because they use fundamentally different matching disciplines and
scopes, not because the underlying "did this person co-author other
retracted work" question is different.

WHY FIRST/LAST ONLY, NOT EVERY CO-AUTHOR: the first author typically did the
hands-on work, the last author is conventionally the senior figure who
supervised/vouches for it; a middle author's role is far more variable and
less attributable -- and ORCID-strict matching for every middle author would
also multiply the ORCID API call volume several-fold for comparatively little
gain in confidence over the fuzzy-cluster approach already used for them.
WROTE.author_position ('first'/'last'/'middle', from extract_enrich.py's
parse of each paper's author list order) already carries this.

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
  - SEVERITY SPLIT (new): of those retracted DOIs, how many carry a
    MISCONDUCT-coded Reason (same MISCONDUCT_REASONS list used everywhere
    else in this pipeline -- retraction_watch.csv's own semicolon-separated
    "Reason" column, string-matched, not re-derived). A retraction with a
    misconduct reason is scored higher than one without (fabrication/paper
    mill/ORI finding vs. an honest error or duplication).

Authors WITHOUT an orcid on file are left with null fields -- never guessed
via name matching, never silently 0 (plan.md §0). Measured 2026-07-22: of
this graph's first/last-author instances, 1,359/2,067 (66%) first authors
and 1,620/1,994 (81%) last authors carry an orcid; 1,994 distinct ORCIDs
across both positions.

FLOOR: an author with only 1-2 total claimed works produces a meaningless,
noisy rate (one retraction out of one paper = "100%"), so a rate is only
computed once the ORCID record claims >= MIN_CLAIMED_WORKS DOIs; below
LOW_CONFIDENCE_CLAIMED_WORKS the rate is still shown but flagged
low-confidence (same idiom as publisher/journal's LOW_CONFIDENCE_TOTAL_DOIS).

PER-POSITION, NOT COLLAPSED TO ONE "WORST" (changed 2026-07-22): a paper's
first AND last author are tracked and scored independently now (fl_any_count/
fl_misconduct_count on Paper can be 0, 1, or 2), instead of the previous
"keep only whichever of the two has the higher rate" collapse -- needed so
"each" in the user's scoring request (one point value per QUALIFYING position,
not just the single worst one) is literal. See tier_a_scoring.py's
MINMAX_KEYS / minmax_contribution() for how fl_any_count/fl_misconduct_count
turn into a score: each is minmax-scaled against its own worst-in-corpus
COUNT (naturally 0-2, since there are only two first/last slots per paper),
not against a raw uncapped per-person retraction tally -- checked live before
shipping that this avoids the single-author-domination failure mode a flat
"+N points per retracted work" scheme would hit (one prolific author in this
corpus has 248 ORCID-claimed works flagged retracted; scoring that literally
would have made one signal 40-80x bigger than everything else combined).

VOLUME, added as a SEPARATE minmax-scaled layer (2026-08-16, user request):
fl_any_count/fl_misconduct_count above deliberately answer only "is this
position's history tainted at all" (0/1 per position) -- by design, a first/
last author with 1 qualifying retraction and one with 17 contribute
identically. That flattening was the 2026-07-22 fix for the domination
failure mode above, but it also erases a real severity signal: verified live
2026-08-16 (10.1016/j.envres.2024.119440, retracted, last author Arivalagan
Pugazhendhi has 17 other retracted works -- scored no differently than 1
would have). fl_any_volume/fl_misconduct_volume below restore that severity
signal WITHOUT reopening the domination risk, because they use the exact same
minmax-against-corpus-worst mechanism already proven safe for the entity-rate
signals (institution/publisher/country/journal): the single worst-in-corpus
author (checked live: Pierre-Edouard Fournier, n=134 any-reason, appearing as
a qualifying first/last author on 17 of this corpus's candidate papers) scores
a fixed target no matter how far ahead of everyone else he is -- adding a
future, even-worse outlier cannot make this signal blow up the way a flat
per-work weight would. This is ADDITIVE to, not a replacement for,
fl_any_count/fl_misconduct_count -- the presence question ("tainted at all?")
and the severity question ("how much?") are both real signals, so both are
scored, at half-weight for volume (config/weights.yaml) so presence stays
dominant. SUM across positions when both qualify (55 papers in this corpus do,
e.g. first=20 + last=134=154) -- matches the existing convention that
fl_any_count already sums 0/1/2 across positions rather than keeping only the
worse one; checked live this doesn't distort the corpus max (154 vs a
single-author max of 134).

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

# Same list used by coauthor_retraction_severity.py / gds_node_classification.py
# -- kept as a local copy (established convention in this pipeline: each
# sensor duplicates this constant rather than sharing a module) but MUST stay
# in sync with both.
MISCONDUCT_REASONS = {
    "Misconduct - Official Investigation(s) and/or Finding(s)",
    "Investigation by ORI",
    "Paper Mill",
    "Falsification/Fabrication of Data",
    "Falsification/Fabrication of Image",
    "Falsification/Fabrication of Results",
    "Manipulation of Images",
    "Manipulation of Results",
    "Euphemisms for Misconduct",
    "Misconduct by Author",
}


def load_rw_reason_map() -> dict[str, bool]:
    """{OriginalPaperDOI: is_misconduct} for every retracted DOI in the FULL
    retraction_watch.csv, canonicalized -- deliberately NOT filtered to
    data.seed_subset (unbiased, same reasoning as publisher_retraction_
    rate.py). DOI-to-DOI matching only -- no name matching anywhere in this
    sensor. is_misconduct is True iff ANY of the semicolon-separated Reason
    codes for that DOI is in MISCONDUCT_REASONS."""
    reasons: dict[str, bool] = {}
    with RW_CSV_PATH.open(encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            d = canon_doi(row.get("OriginalPaperDOI", ""))
            if not d:
                continue
            codes = {c.strip() for c in (row.get("Reason", "") or "").split(";") if c.strip()}
            is_misconduct = bool(codes & MISCONDUCT_REASONS)
            # A DOI can appear more than once (multi-part retraction notices) --
            # OR the misconduct flags together rather than overwrite.
            reasons[d] = reasons.get(d, False) or is_misconduct
    return reasons


def main() -> None:
    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    print("[author_retraction_rate_external] loading full retraction_watch.csv (unfiltered)...", file=sys.stderr)
    rw_reasons = load_rw_reason_map()
    print(f"  {len(rw_reasons)} distinct retracted DOIs "
          f"({sum(rw_reasons.values())} misconduct-coded)", file=sys.stderr)

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
            results.append({"orcid": orcid, "rate": None, "n_retracted": 0, "n_misconduct": 0,
                             "n_total": None, "low_confidence": None})
            if errors <= 10:
                print(f"  [{i}/{len(orcids)}] ORCID {orcid}: lookup failed ({e})", file=sys.stderr)
            continue

        total = len(claimed)
        if total < MIN_CLAIMED_WORKS:
            results.append({"orcid": orcid, "rate": None, "n_retracted": 0, "n_misconduct": 0,
                             "n_total": total, "low_confidence": None})
            continue

        retracted = [d for d in claimed if d in rw_reasons]
        n_misconduct = sum(1 for d in retracted if rw_reasons[d])
        rate = len(retracted) / total
        results.append({
            "orcid": orcid, "rate": rate, "n_retracted": len(retracted), "n_misconduct": n_misconduct,
            "n_total": total, "low_confidence": total < LOW_CONFIDENCE_CLAIMED_WORKS,
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
                    a.author_retr_misconduct_count_external = $n_misconduct,
                    a.author_total_claimed_works = $n_total,
                    a.author_low_confidence = $low_confidence,
                    a.author_rate_checked_date = $today
                """,
                orcid=r["orcid"], rate=r["rate"], n_retracted=r["n_retracted"],
                n_misconduct=r["n_misconduct"], n_total=r["n_total"],
                low_confidence=r["low_confidence"], today=today,
            )

        print("[author_retraction_rate_external] per-paper first/last positions (independent, not collapsed)...",
              file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)
            SET p.first_author_name = null, p.first_author_retr_rate = null,
                p.first_author_retr_n = null, p.first_author_retr_misconduct_n = null,
                p.first_author_retr_total = null, p.first_author_low_confidence = null,
                p.last_author_name = null, p.last_author_retr_rate = null,
                p.last_author_retr_n = null, p.last_author_retr_misconduct_n = null,
                p.last_author_retr_total = null, p.last_author_low_confidence = null,
                p.fl_any_count = 0, p.fl_misconduct_count = 0,
                p.fl_any_volume = 0, p.fl_misconduct_volume = 0
            """
        ).consume()
        s.run(
            """
            MATCH (a:AuthorInstance)-[w:WROTE]->(p:Paper)
            WHERE w.author_position = 'first' AND a.author_retr_rate_external IS NOT NULL
            SET p.first_author_name = a.name, p.first_author_retr_rate = a.author_retr_rate_external,
                p.first_author_retr_n = a.author_retr_count_external,
                p.first_author_retr_misconduct_n = a.author_retr_misconduct_count_external,
                p.first_author_retr_total = a.author_total_claimed_works,
                p.first_author_low_confidence = a.author_low_confidence
            """
        ).consume()
        s.run(
            """
            MATCH (a:AuthorInstance)-[w:WROTE]->(p:Paper)
            WHERE w.author_position = 'last' AND a.author_retr_rate_external IS NOT NULL
            SET p.last_author_name = a.name, p.last_author_retr_rate = a.author_retr_rate_external,
                p.last_author_retr_n = a.author_retr_count_external,
                p.last_author_retr_misconduct_n = a.author_retr_misconduct_count_external,
                p.last_author_retr_total = a.author_total_claimed_works,
                p.last_author_low_confidence = a.author_low_confidence
            """
        ).consume()
        # fl_any_count/fl_misconduct_count: mutually exclusive per position --
        # a position with >=1 misconduct-reason retraction counts ONLY in
        # fl_misconduct_count, never double-counted in fl_any_count too.
        s.run(
            """
            MATCH (p:Paper)
            SET p.fl_misconduct_count =
                    (CASE WHEN coalesce(p.first_author_retr_misconduct_n, 0) > 0 THEN 1 ELSE 0 END) +
                    (CASE WHEN coalesce(p.last_author_retr_misconduct_n, 0) > 0 THEN 1 ELSE 0 END),
                p.fl_any_count =
                    (CASE WHEN coalesce(p.first_author_retr_n, 0) > 0 AND coalesce(p.first_author_retr_misconduct_n, 0) = 0
                          THEN 1 ELSE 0 END) +
                    (CASE WHEN coalesce(p.last_author_retr_n, 0) > 0 AND coalesce(p.last_author_retr_misconduct_n, 0) = 0
                          THEN 1 ELSE 0 END)
            """
        ).consume()
        # fl_any_volume/fl_misconduct_volume: SUM (not max) of the underlying
        # retraction count across qualifying positions -- see module docstring
        # "VOLUME" note for why this is additive to, not a replacement for,
        # fl_any_count/fl_misconduct_count above, and why sum over max.
        s.run(
            """
            MATCH (p:Paper)
            SET p.fl_misconduct_volume =
                    (CASE WHEN coalesce(p.first_author_retr_misconduct_n, 0) > 0 THEN p.first_author_retr_misconduct_n ELSE 0 END) +
                    (CASE WHEN coalesce(p.last_author_retr_misconduct_n, 0) > 0 THEN p.last_author_retr_misconduct_n ELSE 0 END),
                p.fl_any_volume =
                    (CASE WHEN coalesce(p.first_author_retr_n, 0) > 0 AND coalesce(p.first_author_retr_misconduct_n, 0) = 0
                          THEN p.first_author_retr_n ELSE 0 END) +
                    (CASE WHEN coalesce(p.last_author_retr_n, 0) > 0 AND coalesce(p.last_author_retr_misconduct_n, 0) = 0
                          THEN p.last_author_retr_n ELSE 0 END)
            """
        ).consume()

        dist = s.run(
            """
            MATCH (p:Paper {is_retracted:false})
            RETURN count(*) AS n,
                   sum(CASE WHEN p.fl_any_count > 0 THEN 1 ELSE 0 END) AS with_any,
                   sum(CASE WHEN p.fl_misconduct_count > 0 THEN 1 ELSE 0 END) AS with_misconduct,
                   max(p.fl_any_volume) AS max_any_volume,
                   max(p.fl_misconduct_volume) AS max_misconduct_volume
            """
        ).single()

    driver.close()

    print("\n=== Author retraction rate (external, first/last only) — verification ===", file=sys.stderr)
    print(f"Not-yet-retracted candidates with a qualifying first/last author: "
          f"{dist['with_any']} any-reason, {dist['with_misconduct']} misconduct-reason (of {dist['n']})",
          file=sys.stderr)
    print(f"Max fl_any_volume in corpus: {dist['max_any_volume']}  |  "
          f"max fl_misconduct_volume: {dist['max_misconduct_volume']}  "
          f"(minmax-scored, see tier_a_scoring.py)", file=sys.stderr)

    matched = [r for r in results if r["rate"] is not None]
    matched.sort(key=lambda r: r["rate"], reverse=True)
    skipped_floor = sum(1 for r in results if r["rate"] is None and r["n_total"] is not None and r["n_total"] < MIN_CLAIMED_WORKS)
    print(f"\n{len(matched)} / {len(results)} ORCIDs produced a rate "
          f"({skipped_floor} below the {MIN_CLAIMED_WORKS}-claimed-works floor, {errors} lookup errors).",
          file=sys.stderr)
    print("\nTop 15 authors by personal external retraction rate:", file=sys.stderr)
    for r in matched[:15]:
        print(f"  {r['rate']:.1%}  {r['n_retracted']}/{r['n_total']} claimed works retracted "
              f"({r['n_misconduct']} misconduct-coded)"
              f"{' (low confidence)' if r['low_confidence'] else ''}  orcid={r['orcid']}", file=sys.stderr)


if __name__ == "__main__":
    main()
