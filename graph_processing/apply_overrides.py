#!/usr/bin/env python3
"""
apply_overrides.py — Step 1b: apply human identity SPLITS.

Reads data/graph/identity_overrides.yaml and neutralizes mis-assigned ORCIDs so
the affected AuthorInstance no longer joins that ORCID's group in Step 2. The
original ORCID is preserved in `orcid_raw`; `identity_override='split'` records
that a human overruled OpenAlex. Idempotent: re-running applies the same result,
and a fresh build_instances.py + this step reproduces the correction from source.

MERGES are NOT applied here — they become forced edges in link_instances.py
(Step 2), since PROBABLY_SAME_AS does not exist yet at this point.

Run (after build_instances.py, before link_instances.py):
  python graph_processing/apply_overrides.py
"""
from __future__ import annotations

from pathlib import Path
import sys

import yaml
from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_authors import resolve_connection  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES = REPO_ROOT / "data" / "graph" / "identity_overrides.yaml"

SPLIT_QUERY = """
MATCH (a:AuthorInstance)-[:WROTE]->(p:Paper {doi:$doi})
WHERE a.name = $name
  AND ($orcid IS NULL OR coalesce(a.orcid_raw, a.orcid) = $orcid)
SET a.orcid_raw        = coalesce(a.orcid_raw, a.orcid),
    a.identity_override = 'split',
    a.override_reason   = $reason,
    a.has_orcid         = false
REMOVE a.orcid
RETURN count(a) AS n
"""


def main() -> None:
    if not OVERRIDES.exists():
        print(f"no override file at {OVERRIDES} — nothing to apply")
        return
    cfg = yaml.safe_load(OVERRIDES.read_text()) or {}
    splits = cfg.get("splits") or []
    merges = cfg.get("merges") or []

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    applied = matched = 0
    with driver.session(database=conn["database"]) as s:
        for sp in splits:
            doi, name = sp.get("doi"), sp.get("name")
            if not doi or not name:
                print(f"  ! skipping malformed split (need doi+name): {sp}")
                continue
            n = s.run(SPLIT_QUERY, doi=doi, name=name,
                      orcid=sp.get("orcid"), reason=sp.get("reason", "")).single()["n"]
            applied += 1
            matched += n
            flag = "" if n else "  <-- MATCHED 0 INSTANCES (check doi/name/orcid)"
            print(f"  split: {name} @ {doi} -> {n} instance(s) neutralized{flag}")

    driver.close()
    print(f"applied {applied} split rule(s), {matched} instance(s) affected; "
          f"{len(merges)} merge(s) deferred to link_instances.py")


if __name__ == "__main__":
    main()
