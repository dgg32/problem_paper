#!/usr/bin/env python3
"""
publisher_retraction_rate.py — Tier-A graph feature: per-publisher retraction
rate from EXTERNAL, unbiased sources (2026-07-22, replacing the graph-internal
journal_retr_rate/institution_retr_rate as the "denominator problem" is fixed
here, not there -- see skill_worth_exploring.md's arxiv-2602.19197 discussion).

THE BIAS THIS FIXES: journal_retr_rate and institution_retr_rate (gds_node_
classification.py / institution_retraction_rate.py) both compute a rate using
ONLY the papers already in our own graph -- which is itself a Retraction-Watch
-seeded, microbiology-scoped subset (.env.yaml data.seed_subset). A journal or
institution connected only to seed-retracted papers can show a ~100% "rate"
that says nothing about its true retraction rate in the wild. This sensor
instead uses:
  - NUMERATOR: the FULL retraction_watch.csv (71,106 records, ALL subjects --
    NOT filtered to data.seed_subset like extract_enrich.py's ingest is),
    grouped by its own Publisher column. This is a real, external, complete
    count of retractions attributed to this publisher, independent of what
    happens to be in our graph.
  - DENOMINATOR: Crossref's public Members API (`GET /members?query=<name>`),
    which returns each publisher's `counts.total-dois` -- the total number of
    DOIs that publisher has ever deposited with Crossref. Free, keyless
    (mailto for the polite pool only), and confirmed live 2026-07-22 (e.g.
    Elsevier BV: total-dois 25,112,281).

rate = (RW retractions attributed to this publisher) / (Crossref total-dois
for the matched member) -- an actual base-rate estimate, not a graph-relative
one.

NAME MATCHING (the real engineering problem here): Journal.publisher in our
graph holds Crossref/OpenAlex-canonical legal names ("Elsevier BV", "Springer
Science+Business Media"), but retraction_watch.csv's own Publisher column is
free text, often a short brand nickname or a "Parent - Imprint" compound
("Springer - Nature Publishing Group", "Elsevier - Cell Press"). Matching is
done by normalizing both sides (strip parentheticals/punctuation, lowercase)
and testing bidirectional substring containment, plus a tiny hand-checked
alias table for bare acronyms Crossref doesn't recognize AS acronyms (PLoS,
BMC, MDPI). This correctly separates Springer's sub-brands in practice (see
inline comments in match_publisher()) but is a best-effort heuristic, not a
verified join -- every match is stored (publisher_crossref_name,
publisher_rw_match_count) so a reviewer can audit it, and any graph publisher
with ZERO matched RW rows is left with publisher_retr_rate = null (not 0.0)
and logged, per plan.md §0: absence of a confident match must never quietly
read as "verified clean."

Idempotent: recomputes both Journal and Paper properties from scratch every
run. Crossref Members lookups are cached in-process per distinct publisher
(one call per DISTINCT Journal.publisher in the graph, not per paper/journal).

Usage:
  python graph_processing/publisher_retraction_rate.py
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
MIN_MATCH_LEN = 4  # guard against trivial short-string false-positive substring matches

# Bare acronyms/nicknames Retraction Watch uses standalone that won't
# substring-match our graph's fuller Crossref-canonical names. Hand-checked
# against RW's own top publisher strings 2026-07-22 -- NOT exhaustive, just
# the highest-volume bare-acronym cases; anything else still gets a fair shot
# via plain substring matching (which already correctly separates e.g.
# "Springer - Nature Publishing Group" from bare "Springer" and from
# "Springer - Biomed Central (BMC)" without needing an alias).
RW_ALIASES = {
    "plos": "public library of science",
    "bmc": "biomed central",
    "mdpi": "multidisciplinary digital publishing institute",
}


def normalize(name: str) -> str:
    if not name:
        return ""
    n = _PAREN_RE.sub(" ", name)
    n = n.replace("&", " and ")
    n = _PUNCT_RE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip().lower()
    return RW_ALIASES.get(n, n)


def load_rw_publisher_counts() -> dict[str, int]:
    """{normalized RW Publisher string: count of retraction records}, from the
    FULL csv -- deliberately NOT filtered to data.seed_subset (see docstring:
    the whole point is an unbiased, non-graph-scoped numerator)."""
    counts: dict[str, int] = {}
    with RW_CSV_PATH.open(encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            key = normalize(row.get("Publisher", ""))
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def match_publisher(graph_publisher: str, rw_counts: dict[str, int]) -> tuple[int, list[str]]:
    """Sum RW counts for every normalized RW key that bidirectionally
    substring-matches this graph publisher's normalized name. Returns
    (total_count, matched_rw_keys) for audit."""
    g = normalize(graph_publisher)
    if len(g) < MIN_MATCH_LEN:
        return 0, []
    total = 0
    matched = []
    for rw_key, n in rw_counts.items():
        if len(rw_key) < MIN_MATCH_LEN:
            continue
        if rw_key in g or g in rw_key:
            total += n
            matched.append(rw_key)
    return total, matched


# Crossref's Members API query search is inconsistent for real multi-word
# publisher names -- e.g. "Springer Nature" and "Taylor and Francis" return
# ZERO results even though both publishers obviously have Crossref
# registrations, while "Springer" and "SAGE" alone find them fine (checked
# live 2026-07-22). It also frequently ranks an unrelated same-named small
# member ABOVE the real publisher, and a loose "shares any one word" test is
# not enough to reject those false positives -- e.g. "BioMed Central" shares
# the word "biomed" with an unrelated "BioMed Research Publishers" (1,932
# total-dois) that ranks above the real ~huge BioMed Central registration
# search can't find at all; a naive top-match would have reported a fake 47%
# retraction rate. Fixed by requiring every one of the QUERY's significant
# tokens to appear in the candidate's tokens (a strict subset test, not mere
# intersection) -- "biomed" alone is not enough, "biomed"+"central" both
# must be present. No hard-coded rate ceiling is used instead of this,
# deliberately: Hindawi's real, correctly-matched rate is already ~8% (a
# well-documented 2023-2024 mass-retraction event), so an arbitrary "reject
# anything above N%" rule would have silently discarded a true positive.
# QUERY_ALIASES covers the couple of cases (so far: MDPI) where even the
# retry-with-first-token strategy can't find the right member because the
# real Crossref registration uses an acronym our graph's full institutional
# name doesn't literally contain.
MIN_TOTAL_DOIS = 1000
LOW_CONFIDENCE_TOTAL_DOIS = 10_000
_STOPWORDS = {
    # Deliberately NOT "press" or "media" -- both are pure legal-entity noise
    # in some names (Frontiers Media) but the ONE distinguishing word in
    # others ("Cell Press" vs "Cell Physiol Biochem Press", "Portland Press",
    # "Cold Spring Harbor Laboratory Press") -- stripping them caused a real
    # false-positive match (Cell Press -> an unrelated "Japan Society for
    # Cell Biology" via the shared token "cell" alone). Keep them as real
    # tokens; the cost is a few otherwise-correct matches whose Crossref
    # member name happens to drop the word becoming "no confident match"
    # instead -- an acceptable trade given plan.md §0 (missing > wrong).
    "the", "and", "of", "group", "publishing", "publishers", "publications",
    "ltd", "limited", "inc", "llc", "corporation", "corp", "co",
    "company", "gmbh", "kg", "bv", "sa", "ag", "plc", "technologies",
    "international", "science", "sciences", "institute",
}
QUERY_ALIASES = {
    "multidisciplinary digital publishing institute": "MDPI",
}


def _tokens(name: str) -> set[str]:
    return {t for t in normalize(name).split() if t not in _STOPWORDS and len(t) >= 4}


def _crossref_query(query: str) -> list[dict]:
    r = requests.get(
        f"{CROSSREF['base_url']}/members",
        params={"query": query, "rows": 10, "mailto": CROSSREF.get("mailto", "")},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("message", {}).get("items", [])


def lookup_crossref_member(name: str) -> dict | None:
    qn = normalize(name)

    # A QUERY_ALIASES hit means the original name and the real Crossref
    # registration share NO tokens by design (it's an acronym expansion, e.g.
    # "Multidisciplinary Digital Publishing Institute" -> "MDPI"), so the
    # token-subset test below would always reject it -- trust the hand-
    # curated alias directly instead, picking its largest-total-dois result.
    alias = QUERY_ALIASES.get(qn)
    if alias:
        try:
            alias_items = _crossref_query(alias)
        except (requests.RequestException, ValueError):
            alias_items = []
        if alias_items:
            alias_items.sort(key=lambda it: it.get("counts", {}).get("total-dois", 0), reverse=True)
            return alias_items[0]

    try:
        items = list(_crossref_query(name))
        toks = [t for t in name.replace("&", " and ").split() if t.lower().strip(".,") not in _STOPWORDS]
        if toks:
            items += _crossref_query(toks[0])
    except (requests.RequestException, ValueError):
        return None
    if not items:
        return None

    for it in items:
        if normalize(it.get("primary-name", "")) == qn:
            return it

    qtok = _tokens(name)
    if not qtok:
        return None
    candidates = [it for it in items if qtok <= _tokens(it.get("primary-name", ""))]
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

    print("[publisher_retraction_rate] loading full retraction_watch.csv (unfiltered)...", file=sys.stderr)
    rw_counts = load_rw_publisher_counts()
    print(f"  {sum(rw_counts.values())} retraction records across {len(rw_counts)} distinct publisher strings",
          file=sys.stderr)

    with driver.session(database=conn["database"]) as s:
        graph_publishers = [
            r["publisher"] for r in s.run(
                "MATCH (j:Journal) WHERE j.publisher IS NOT NULL AND j.publisher <> '' "
                "RETURN DISTINCT j.publisher AS publisher"
            )
        ]
    print(f"[publisher_retraction_rate] {len(graph_publishers)} distinct publishers in our graph", file=sys.stderr)

    rps = CROSSREF.get("requests_per_second", 10)
    min_interval = 1.0 / rps
    last_call = 0.0

    today = str(date.today())
    results = []
    unmatched = []
    for i, pub in enumerate(graph_publishers, 1):
        rw_count, matched_keys = match_publisher(pub, rw_counts)

        wait = min_interval - (time.time() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.time()
        member = lookup_crossref_member(pub)

        if member is None or rw_count == 0:
            unmatched.append((pub, rw_count, member.get("primary-name") if member else None))
            results.append({
                "publisher": pub, "rate": None, "rw_count": rw_count,
                "total_dois": None, "crossref_name": None, "crossref_id": None,
                "low_confidence": None,
            })
            continue

        total_dois = member.get("counts", {}).get("total-dois", 0)
        rate = (rw_count / total_dois) if total_dois else None
        # A rate > 1.0 is mathematically impossible (can't retract more
        # papers than a publisher has ever deposited) -- always a matching
        # artifact (wrong/tiny denominator), never a real signal. Discard
        # rather than store an impossible number (plan.md §0).
        if rate is not None and rate > 1.0:
            unmatched.append((pub, rw_count, f"{member.get('primary-name')} (discarded: implausible rate {rate:.1%})"))
            results.append({
                "publisher": pub, "rate": None, "rw_count": rw_count,
                "total_dois": total_dois, "crossref_name": None, "crossref_id": None,
                "low_confidence": None,
            })
            continue
        results.append({
            "publisher": pub,
            "rate": rate,
            "rw_count": rw_count,
            "total_dois": total_dois,
            "crossref_name": member.get("primary-name"),
            "crossref_id": member.get("id"),
            "low_confidence": total_dois < LOW_CONFIDENCE_TOTAL_DOIS,
        })
        if i % 20 == 0:
            print(f"  [{i}/{len(graph_publishers)}]", file=sys.stderr)

    with driver.session(database=conn["database"]) as s:
        for r in results:
            s.run(
                """
                MATCH (j:Journal {publisher: $publisher})
                SET j.publisher_retr_rate = $rate,
                    j.publisher_retr_count = $rw_count,
                    j.publisher_total_dois = $total_dois,
                    j.publisher_crossref_name = $crossref_name,
                    j.publisher_crossref_id = $crossref_id,
                    j.publisher_low_confidence = $low_confidence,
                    j.publisher_rate_checked_date = $today
                """,
                publisher=r["publisher"], rate=r["rate"], rw_count=r["rw_count"],
                total_dois=r["total_dois"], crossref_name=r["crossref_name"],
                crossref_id=r["crossref_id"], low_confidence=r["low_confidence"], today=today,
            )

        print("[publisher_retraction_rate] per-paper copy from paper's own journal...", file=sys.stderr)
        s.run(
            """
            MATCH (p:Paper)
            SET p.publisher_retr_rate = null,
                p.publisher_retr_rate_name = null,
                p.publisher_retr_rate_n = null,
                p.publisher_retr_rate_low_confidence = null
            """
        ).consume()
        s.run(
            """
            MATCH (p:Paper)-[:PUBLISHED_IN]->(j:Journal)
            WHERE j.publisher_retr_rate IS NOT NULL
            WITH p, j ORDER BY j.publisher_retr_rate DESC
            WITH p, collect({name: j.publisher, rate: j.publisher_retr_rate, n: j.publisher_retr_count,
                              low_conf: j.publisher_low_confidence})[0] AS top
            SET p.publisher_retr_rate = top.rate,
                p.publisher_retr_rate_name = top.name,
                p.publisher_retr_rate_n = top.n,
                p.publisher_retr_rate_low_confidence = top.low_conf
            """
        ).consume()

        dist = s.run(
            """
            MATCH (p:Paper {is_retracted:false})
            RETURN count(*) AS n,
                   sum(CASE WHEN p.publisher_retr_rate IS NOT NULL THEN 1 ELSE 0 END) AS with_signal,
                   avg(p.publisher_retr_rate) AS avg_rate
            """
        ).single()

    driver.close()

    print("\n=== Publisher retraction rate — verification ===", file=sys.stderr)
    print(f"Not-yet-retracted candidates with a publisher_retr_rate: "
          f"{dist['with_signal']} / {dist['n']}"
          + (f"  (avg {dist['avg_rate']:.4f})" if dist['avg_rate'] is not None else ""),
          file=sys.stderr)

    matched = [r for r in results if r["rate"] is not None]
    matched.sort(key=lambda r: r["rate"], reverse=True)
    print(f"\nMatched {len(matched)} / {len(results)} graph publishers to a Crossref member + RW count.", file=sys.stderr)
    print("\nTop 15 publishers by external retraction rate:", file=sys.stderr)
    for r in matched[:15]:
        print(f"  {r['rate']:.2%}  rw={r['rw_count']:<5} total_dois={r['total_dois']:<10} "
              f"{r['publisher']} -> {r['crossref_name']}", file=sys.stderr)

    if unmatched:
        print(f"\n{len(unmatched)} publisher(s) left WITHOUT a rate (no RW match and/or no confident "
              f"Crossref match -- publisher_retr_rate left null, not silently 0):", file=sys.stderr)
        for pub, rw_count, crossref_name in unmatched[:20]:
            print(f"  {pub}  (rw_count={rw_count}, crossref_match={crossref_name})", file=sys.stderr)


if __name__ == "__main__":
    main()
