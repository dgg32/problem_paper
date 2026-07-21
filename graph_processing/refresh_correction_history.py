#!/usr/bin/env python3
"""
refresh_correction_history.py — Crossref-sourced correction/errata history for
every not-yet-retracted candidate (skill_worth_exploring.md 2026-07-21:
"Corrections/errata-history", metadata-only, build-now).

Our graph currently has zero visibility into corrections short of a full
retraction: is_retracted only tracks the terminal state (Retraction Watch +
refresh_retraction_status.py), and refresh_editorial_notices.py covers
PubMed's Expression-of-Concern/Erratum notices. Crossref carries a third,
independent fact: a correction NOTICE's own deposited `update-to` field,
which points BACKWARD at the DOI it corrects.

IMPORTANT (confirmed empirically 2026-07-21, do not "fix" this back): the
obvious-looking approach -- querying the ORIGINAL paper's own Crossref record
(`/works/{doi}`) for a forward `relation.is-corrected-by` link -- looks
correct in Crossref's docs but is a dead end in practice. Spot-checked
against real PLOS ONE correction chains (e.g. 10.1371/journal.pone.0292583
correcting 10.1371/journal.pone.0271005): `relation` was `None` on every
original article record tested, while `update-to` was populated on every
correction notice's OWN record. Publishers deposit the backward link, not
the forward one. So the only reliable way to find "does paper X have a
correction" is Crossref's reverse-lookup search filter,
`GET /works?filter=updates:{doi}`, which returns every record that lists X
in its own `update-to` -- confirmed live to return the correcting DOI with
`update-to[].type == "correction"`, `.DOI` == the original, and
`.source in {"publisher", "retraction-watch"}`.

`update-to[].type` is a broader controlled vocabulary than just corrections
(also covers erratum, expression_of_concern, retraction, removal,
withdrawal, partial_retraction, ...). Only {correction, corrigendum,
erratum, addendum} are counted here -- retraction/expression_of_concern
overlap with signals other sensors already own (is_retracted,
refresh_editorial_notices.py) and are deliberately excluded to avoid
double-counting a different fact under this sensor's name.

Coverage caveat: this only sees what Crossref's `update-to` index has
ingested (publisher deposits + Crossref's own Retraction Watch feed). A
correction not deposited this way -- or deposited only as a same-DOI content
update with no typed relation -- won't show up. Absence of a hit is NOT
proof a paper was never corrected. Where a hit DOES exist it's a real,
dated, sourced fact, but corrections are routinely benign (typo/affiliation
fixes), so this is scored LOW (see tier_a_scoring.py) -- explainable
context, not an accusation (plan.md §0).

On a Crossref lookup failure (transport error, not a genuine empty result),
the paper is left untouched rather than silently recorded as "no
correction" -- per plan.md §0, a transient error must never quietly become
a false clean bill of health.

Fields written (facts, not verdicts -- plan.md §0):
  crossref_correction_count : number of distinct correction-notice DOIs
                              found via the updates:{doi} reverse lookup
  has_correction            : bool, correction_count > 0
  crossref_correction_dois  : JSON list of the correction notice DOIs
  crossref_checked_date     : provenance

Idempotent: overwrites with a fresh Crossref check every run.

Usage:
  python graph_processing/refresh_correction_history.py             # check + apply
  python graph_processing/refresh_correction_history.py --dry-run    # check only
"""
from __future__ import annotations

import argparse
import json
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
from _crossref_verify import LookupUnavailable  # noqa: E402

CONFIG_PATH = REPO_ROOT / ".env.yaml"
cfg = yaml.safe_load(CONFIG_PATH.read_text())
CROSSREF = cfg.get("crossref", {})

# Only true corrections/errata -- see module docstring for why retraction/
# expression_of_concern/removal/withdrawal/partial_retraction are excluded.
CORRECTION_TYPES = {"correction", "corrigendum", "erratum", "addendum"}


def _canon_doi(doi: str) -> str:
    d = doi.strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


def fetch_correction_dois(doi: str) -> list[str]:
    """Reverse lookup: correction-notice DOIs whose own update-to links back
    to `doi` with a correction/errata-type relation. See module docstring --
    this is the only direction that actually has data in practice."""
    canon = _canon_doi(doi)
    if not canon:
        return []
    try:
        r = requests.get(
            f"{CROSSREF['base_url']}/works",
            params={"filter": f"updates:{canon}", "rows": 20,
                    "mailto": CROSSREF.get("mailto", "")},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
    except (requests.RequestException, ValueError) as e:
        raise LookupUnavailable(f"Crossref updates-filter lookup failed for {doi}: {e}") from e

    out: list[str] = []
    for it in items:
        for u in it.get("update-to", []):
            if (u.get("DOI") or "").lower() == canon and u.get("type") in CORRECTION_TYPES:
                cdoi = (it.get("DOI") or "").lower()
                if cdoi and cdoi not in out:
                    out.append(cdoi)
                break
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="check only, do not write to the graph")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    with driver.session(database=conn["database"]) as s:
        rows = [dict(r) for r in s.run(
            "MATCH (p:Paper {is_retracted:false}) WHERE p.doi IS NOT NULL AND p.doi <> '' "
            "RETURN p.doi AS doi"
        )]
    print(f"  checking {len(rows)} not-yet-retracted candidates against Crossref")

    rps = CROSSREF.get("requests_per_second", 10)
    min_interval = 1.0 / rps
    last_call = 0.0

    updates = []
    checked = 0
    failed = 0
    for i, r in enumerate(rows, 1):
        wait = min_interval - (time.time() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.time()
        try:
            dois = fetch_correction_dois(r["doi"])
        except LookupUnavailable as e:
            failed += 1
            print(f"  ! skipping {r['doi']} ({e})", file=sys.stderr)
            continue
        checked += 1
        if dois:
            updates.append({"doi": r["doi"], "correction_dois": dois})
        if i % 50 == 0:
            print(f"  [{i}/{len(rows)}]", file=sys.stderr)

    print(f"\n  checked               : {checked}")
    print(f"  lookup failures       : {failed} (left untouched -- see module docstring)")
    print(f"  with a correction     : {len(updates)}")

    if args.dry_run:
        print("\n  --dry-run: no changes written.")
        driver.close()
        return

    today = str(date.today())
    with driver.session(database=conn["database"]) as s:
        for u in updates:
            s.run(
                """
                MATCH (p:Paper {doi: $doi})
                SET p.crossref_correction_count = $count,
                    p.has_correction            = true,
                    p.crossref_correction_dois  = $dois_json,
                    p.crossref_checked_date     = $today
                """,
                doi=u["doi"],
                count=len(u["correction_dois"]),
                dois_json=json.dumps(u["correction_dois"]),
                today=today,
            )
    driver.close()
    print(f"\n  wrote correction-history fields for {len(updates)} paper(s)")


if __name__ == "__main__":
    main()
