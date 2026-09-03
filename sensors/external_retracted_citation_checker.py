#!/usr/bin/env python3
"""
external_retracted_citation_checker.py — Phase 4 sensor (plan.md).

Closes a coverage gap in retracted_citation_checker.py: that sensor can only
detect "cites a retracted paper" when the retracted paper HAPPENS TO ALREADY
be a Paper node in this graph -- expand_targets.py only materializes CITES
edges to papers "already in the graph" (see its docstring, `if ref in
existing_oaids`). Most retracted papers anywhere are NOT in our ~800-paper
Retraction-Watch-seeded corpus, so that check is structurally blind to the
common case: citing a retracted paper this graph has never heard of.

This sensor closes the gap with a live, per-reference check against OpenAlex,
which exposes `is_retracted` directly on every work record via a boolean
field -- no bibliographic search needed (unlike reference_integrity_checker.py's
no-DOI fallback, which was that sensor's actual false-positive source). For
every not-yet-retracted candidate:
  1. Read its Crossref-deposited reference DOIs (same source
     reference_integrity_checker.py uses).
  2. Skip DOIs that already correspond to a Paper node in this graph --
     that's retracted_citation_checker.py's job, with richer evidence
     (timing, RetractionWatch reason code, self-citation via cluster_id)
     than OpenAlex alone offers.
  3. Batch the remaining ("external") DOIs to OpenAlex via the
     `filter=doi:a|b|c...` OR-filter (up to 50 DOIs/call -- measured
     ~0.9s per call) and flag any hit where is_retracted=true.

Deliberately lighter-weight than retracted_citation_checker.py: OpenAlex
gives no retraction reason or date comparable to RetractionWatch, so there is
no timing/misconduct-reason tiering here, and no self-citation check (would
need fuzzy author-name matching without cluster_id, which this project avoids
on precision grounds -- see reference_integrity_checker.py's Route-2
false-positive lesson, 2026-07-20). Single severity: "medium" -- a confirmed
OpenAlex-flagged retraction, but without RetractionWatch's richer context.

NOT wired into tier_a_scoring.py's WEIGHTS by default -- inspect the first
real run's precision before deciding a weight (same measure-then-score
discipline applied to reference_integrity_checker.py this session).

Usage:
  python sensors/external_retracted_citation_checker.py                  # all candidates -> report
  python sensors/external_retracted_citation_checker.py --doi 10.xxx/xxx  # single paper, stdout only
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

CONFIG_PATH = REPO_ROOT / ".env.yaml"
REPORT_JSON = REPO_ROOT / "data" / "flags" / "external_retracted_citation_flags.json"

cfg = yaml.safe_load(CONFIG_PATH.read_text())
CROSSREF = cfg.get("crossref", {})
OPENALEX = cfg.get("openalex", {})

# Crossref's documented polite-pool concurrency cap (see reference_integrity_checker.py).
CROSSREF_CONCURRENCY = 3
# OpenAlex's documented max DOIs per OR-filter batch.
OPENALEX_BATCH = 50
# Conservative concurrency for the OpenAlex batch-lookup phase (measured ~0.9s/call
# for a 10-DOI batch; .env.yaml already allows 10 req/s for OpenAlex).
OPENALEX_CONCURRENCY = 3


def canon_doi(doi: str) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


def fetch_reference_dois(doi: str) -> tuple[list[str], bool]:
    """Crossref-deposited reference list for one paper -> (DOIs only, ok).
    A reference with no DOI can't be checked here (that's reference_integrity_
    checker.py's job, and its no-DOI route is the one we deliberately do NOT
    repeat, for precision reasons). `ok=False` means the fetch itself failed
    (timeout/5xx/429) -- distinct from a paper that genuinely has no
    references, so callers can tell "found nothing" from "couldn't check"
    (BUG.md R3-1)."""
    try:
        url = f"{CROSSREF['base_url']}/works/{canon_doi(doi)}"
        r = requests.get(url, params={"mailto": CROSSREF["mailto"]}, timeout=10)
        r.raise_for_status()
        work = r.json().get("message", {})
    except requests.RequestException:
        return [], False
    return [canon_doi(ref["DOI"]) for ref in work.get("reference", []) if ref.get("DOI")], True


def openalex_retracted_batch(dois: list[str]) -> tuple[dict[str, dict], bool]:
    """One OpenAlex call for up to OPENALEX_BATCH DOIs -> ({doi: {is_retracted,
    title}}, ok). `ok=False` means the call itself failed -- see
    fetch_reference_dois's docstring (BUG.md R3-1)."""
    if not dois:
        return {}, True
    filt = "|".join(dois)
    try:
        r = requests.get(f"{OPENALEX['base_url']}/works", params={
            "filter": f"doi:{filt}",
            "mailto": OPENALEX.get("mailto", ""),
            "per-page": OPENALEX_BATCH,
            "select": "doi,is_retracted,title",
        }, timeout=20)
        r.raise_for_status()
        results = r.json().get("results", [])
    except (requests.RequestException, ValueError):
        return {}, False
    out = {}
    for w in results:
        d = canon_doi(w.get("doi") or "")
        out[d] = {"is_retracted": bool(w.get("is_retracted")), "title": w.get("title")}
    return out, True


def chunked(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def build_flag(citing_doi: str, citing_title: str, cited_doi: str, cited_title: str) -> dict:
    return {
        "flag": "cites_retracted_paper_external",
        "severity": "medium",
        "citing_paper_doi": citing_doi,
        "citing_paper_title": citing_title,
        "cited_retracted_paper_doi": cited_doi,
        "cited_retracted_paper_title": cited_title,
        "evidence": (
            f'"{citing_title}" ({citing_doi}) cites "{cited_title}" ({cited_doi}), '
            f'which OpenAlex marks as retracted. This paper is outside our own '
            f'Retraction-Watch-seeded corpus, so no RetractionWatch reason/date/'
            f'self-citation context is available -- confirmed retraction status only.'
        ),
        "source_url": f"https://doi.org/{cited_doi}",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", help="check a single citing paper by DOI (stdout only, no report write)")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        candidates = [dict(r) for r in s.run(
            "MATCH (p:Paper {is_retracted:false}) "
            "WHERE $doi IS NULL OR p.doi = $doi "
            "RETURN p.doi AS doi, p.title AS title", doi=args.doi)]
        # Every DOI already in the graph -- retracted_citation_checker.py's territory.
        in_graph = {canon_doi(r["doi"]) for r in s.run("MATCH (p:Paper) RETURN p.doi AS doi") if r["doi"]}
    driver.close()

    if not candidates:
        print(f"no candidate found for {args.doi}" if args.doi else "no candidates")
        return

    print(f"  fetching reference lists for {len(candidates)} candidate(s) "
          f"(Crossref, concurrency={CROSSREF_CONCURRENCY})...")
    with ThreadPoolExecutor(max_workers=CROSSREF_CONCURRENCY) as pool:
        ref_results = list(pool.map(lambda c: fetch_reference_dois(c["doi"]), candidates))
    ref_fetch_failures = sum(1 for _, ok in ref_results if not ok)

    # citing_doi -> set of its EXTERNAL (not-in-graph) reference DOIs
    external_refs: dict[str, set[str]] = {}
    all_external: set[str] = set()
    for c, (refs, _ok) in zip(candidates, ref_results):
        ext = {r for r in refs if r and r not in in_graph}
        if ext:
            external_refs[c["doi"]] = ext
            all_external.update(ext)

    print(f"  {len(all_external)} distinct external reference DOI(s) to check "
          f"({OPENALEX_BATCH}/batch, concurrency={OPENALEX_CONCURRENCY})...")
    batches = list(chunked(sorted(all_external), OPENALEX_BATCH))
    with ThreadPoolExecutor(max_workers=OPENALEX_CONCURRENCY) as pool:
        batch_results = list(pool.map(openalex_retracted_batch, batches))
    batch_failures = sum(1 for _, ok in batch_results if not ok)
    doi_info: dict[str, dict] = {}
    for br, _ok in batch_results:
        doi_info.update(br)

    if ref_fetch_failures or batch_failures:
        # A partial-failure report is indistinguishable from "checked, found
        # nothing" once written -- and wire_sensor_flags.py resets this sensor's
        # scored flag_count to 0 for every paper before rewriting from whatever
        # report is on disk (R2-1's reset-then-recompute semantics), so a Crossref/
        # OpenAlex outage during a routine run would otherwise silently erase
        # every paper's previously-earned external_retracted_citation flag corpus-
        # wide. Refuse to write instead: leave the existing report (and hence the
        # existing graph flags) exactly as they were until a clean run succeeds
        # (BUG.md R3-1).
        print(f"\n  ABORTING without writing a report: {ref_fetch_failures}/{len(candidates)} "
              f"Crossref reference-list fetch(es) and {batch_failures}/{len(batches)} OpenAlex "
              f"batch lookup(s) failed (network/rate-limit). A partial report would look "
              f"identical to 'checked, found nothing' to wire_sensor_flags.py and would wipe "
              f"every paper's existing external_retracted_citation flag. Re-run when upstream "
              f"is healthy.", file=sys.stderr)
        sys.exit(1)

    retracted_dois = {d for d, info in doi_info.items() if info.get("is_retracted")}
    print(f"  {len(retracted_dois)} external DOI(s) confirmed retracted by OpenAlex")

    flags = []
    doi_title = {c["doi"]: c["title"] for c in candidates}
    for citing_doi, ext in external_refs.items():
        for cited_doi in sorted(ext & retracted_dois):
            flags.append(build_flag(
                citing_doi, doi_title.get(citing_doi, ""),
                cited_doi, doi_info.get(cited_doi, {}).get("title") or "",
            ))

    if args.doi:
        if not flags:
            print(f"no external-retracted-citation flags for {args.doi}")
        for f in flags:
            print(json.dumps(f, indent=2))
        return

    by_paper: dict[str, list[dict]] = {}
    for f in flags:
        by_paper.setdefault(f["citing_paper_doi"], []).append(f)

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(flags, indent=2))

    print("\n=== external-retracted-citation-checker ===")
    print(f"  candidate papers flagged : {len(by_paper)}")
    print(f"  total flag records       : {len(flags)}")
    print(f"\n  report written -> {REPORT_JSON.relative_to(REPO_ROOT)}")

    top = sorted(by_paper.items(), key=lambda kv: -len(kv[1]))[:5]
    if top:
        print("\n  top candidates by flag count:")
        for doi, fl in top:
            print(f"    [{len(fl)}] {fl[0]['citing_paper_title'][:80]}  ({doi})")


if __name__ == "__main__":
    main()
