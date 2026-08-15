# BUG.md

## Round 2 — bug hunt, 2026-08-15

Findings #1-#11 below (the 2026-07-19 review) were all verified **already fixed** in the
current tree before this round started: `normalize_authors.py` is now a re-export shim over
`_conn.py`, `reference_integrity_checker.py` has `_first_or_str()` and scans all
`is_retracted:false` papers, `tortured_phrases_detector.py`'s severity ternary is real
(`high`/`medium`), `Counter` moved to module top, and `WEIGHTS`/`minmax_contribution()` are
imported from `tier_a_scoring.py` in both consumers. They are kept below as history.

### R2-1. Stale sensor flags survive a re-run — `graph_processing/wire_sensor_flags.py` (MEDIUM, correctness) — FIXED

The script only ever wrote to DOIs present in the *current* `data/flags/*.json` reports, so a
paper that was flagged by an earlier run and is **absent from the current report** kept its old
`*_flag_count` forever. Those counts are scored in `tier_a_scoring.py` (e.g.
`retracted_citation_flag_count` at weight 3.0), so a withdrawn flag kept inflating a paper's
triage score indefinitely. The worst path: if every report came back empty, the old code hit
`if not all_dois: print("nothing to write; exiting.")` and returned **before touching the
graph at all**, leaving every prior flag in place. This also contradicted the module
docstring's claim that "papers with no flags get explicit zero counts."

**Root cause:** the write set was derived from the reports (`all_dois`) rather than from the
graph, so "no longer flagged" and "not mentioned in this report" were indistinguishable.

**Fix:** reset each sensor's `count_prop`/`json_prop` to `0`/`'[]'` across all `:Paper` nodes
before rewriting that sensor's current report. Scoped to `present_sensors` (report file
actually exists on disk) in both the reset and the per-DOI write, so a **missing** report file
means "this sensor didn't run here" and leaves its properties untouched — important on a
machine restored from `full_graph_snapshot.jsonl`, where `data/flags/` may be absent. An
empty-but-present report now correctly zeroes the sensor instead of exiting early.
Matches the reset-then-recompute idiom already used by `coauthor_retraction_severity.py`
and `author_retraction_rate_external.py`.

**Verified live (2026-08-15, 2078-paper graph):**
1. The reset statement was run for all 7 sensors inside a rolled-back transaction — valid
   Cypher, touch counts matching the graph's nonzero populations exactly.
2. A real `wire_sensor_flags.py` run was diffed per-paper × per-property (2078 × 14) against
   a pre-run backup: **byte-identical**, i.e. the added reset is exactly idempotent when the
   reports on disk already match the graph.
3. A synthetic stale flag (`10.1001/jama.2014.7247` forced to
   `retracted_citation_flag_count = 99`, a DOI confirmed absent from
   `retracted_citation_flags.json`, worth a spurious +297 at weight 3.0) survived the old
   write-only-what's-in-the-report path and was **correctly cleared to `0`/`[]`** by the fix
   (reset count rose 49 → 50 for that run). Graph restored to its exact pre-test state
   afterwards.

Note `data/flags/ai_text_tell_flags.json` is present-but-empty (`[]`) — exactly the case this
fix reclassifies from "skip" to "reset". Its graph population is already 0, so the corrected
semantics are a no-op today, but they now hold if that sensor ever retracts a flag.

### R2-2. `ZeroDivisionError` on an empty comment set — `graph_processing/categorize_pubpeer_comments.py:163` (LOW, crash) — FIXED

`uncategorized_pct = 100 * counts.get("uncategorized", 0) / len(categorized)` ran
unconditionally. With zero PubPeer comments (`categorized == []`) it crashed *after* having
already written `pubpeer_comment_categories.json` — so the output file was correct but the
stage exited nonzero, which `review/pipeline_app.py` renders as a failed stage and, in a
batch run, **stops the whole pipeline** (`_run_batch` returns on the first failure). The
per-category loop above it was safe only incidentally (an empty `counts` never iterates).

**Fix:** return early with an explicit "no PubPeer comments to categorize" message when
`categorized` is empty. Verified by running `main()` against a synthetic zero-comment input.

### R2-3. Dead imports / no-op f-strings across 15 files (TRIVIAL) — FIXED

`ruff --select F401,F541 --fix`: removed unused `json` (`tier_a_scoring.py`), `yaml`
(`build_review_page.py`), `re` and `canon_doi` (`ai_text_tell_detector.py`), `sys`/`time`
(`full_text_fetcher.py`), assorted `typing`/`pathlib` imports in `sensors/image_forensics/`,
and dropped the `f` prefix from 8 f-strings with no placeholders. No behavioural change;
`compileall` and an import smoke-test of the five main modules pass.

### Not fixed (deliberate)

- **`review/pipeline_app.py` check-then-act races** — `run_stage`/`run_all` test
  `STATE[...].status` and `BATCH_STATE["running"]` outside their locks, so two fast clicks can
  start a stage twice. Left alone: the module docstring explicitly scopes this to "plain
  threads — fine for a single local operator, not for concurrent multi-user use," and a real
  fix belongs with the task queue that increment already defers.
- **`author_retraction_rate_external.py` last-writer-wins on `first`/`last` positions** — if a
  paper ever had two `WROTE` edges with `author_position='first'`, the Cypher `SET` would keep
  whichever row Neo4j processed last. Not reachable today (OpenAlex emits exactly one `first`
  and one `last` per work), so this is a latent hazard, not a live bug — flagged here rather
  than fixed speculatively.
- **`normalize_authors.py` shim** — 39 modules still import `resolve_connection` from it vs. 5
  from `_conn`. Harmless indirection; a mass rewrite is churn without a correctness payoff.

---

# Round 1 — improvement findings (code review, 2026-07-19)

Review scope: `graph_processing/` (24 scripts), `sensors/` (11 sensors + `image_forensics/`),
`runs/run_paperconan.py`, `plan.md`, `AGENTS.md`. The project's scoring discipline (hard
facts scored, soft signals labelled-only) is correctly enforced and the code is unusually
well-documented. Findings below are concrete bugs, dead code, and consistency risks, ordered
by impact.

> NOTE: this is a research-integrity POC whose governing principle (plan.md §0) is "every
> output is a hypothesis for human review, never an accusation." Fixes that change flag
> generation must preserve that framing — especially #1 and #8, which touch attribution.

---

## 1. Orphaned / conflicting identity script — `graph_processing/normalize_authors.py` (HIGH)

`normalize_authors.py` writes `:SAME_AS` edges using its own blocking-key strategy (first+last
name token + shared institution). But the **live** identity layer is `graph_processing/link_instances.py`,
which writes `:PROBABLY_SAME_AS` edges — the type the entire rest of the system actually reads:
`cluster_instances.py`, `tier_a_scoring.py` (`coauthor_other_misconduct` feature), `import_cypher.txt`,
and `plan.md §2.1b`.

- Nothing in the pipeline or any script imports `normalize_authors` for its identity logic — only
  its `resolve_connection` helper is reused (by ~22 modules). The `SAME_AS` edge type is **never read anywhere**.
- Running it would create a second, divergent, unnamed identity layer in the graph — exactly the
  merge/attribution hazard `plan.md §0` warns against.

**Fix:** delete `normalize_authors.py` after extracting `resolve_connection` into a shared
`graph_processing/_conn.py` (or top-level `db.py`); OR, if it is meant as a candidate generator,
rename its edge type to `PROBABLY_SAME_AS` and wire it into the documented pipeline in `plan.md:180`.

---

## 2. Dead / buggy expression — `sensors/reference_integrity_checker.py:221` (MEDIUM)

```python
flag["citing_paper_title"] = work.get("title") or [""][0] if isinstance(work.get("title"), list) else work.get("title", "")
```

`work.get("title") or [""][0]` — the `[""][0]` always evaluates to `""`, so the left side of the
`or` is meaningless and the branch is dead/confusing. The intent was presumably `work.get("title") or [""]`
(a list to index) so the `isinstance(..., list)` branch extracts element 0. As written, when `title`
is a list the code takes the `else` branch and assigns the **list object** (not a string) as the title.

**Fix:** extract the title once with a small helper, matching the pattern already used at line 149:
```python
def _first_or_str(title) -> str:
    if isinstance(title, list):
        return title[0] if title else ""
    return title or ""
```

---

## 3. No-op conditional — `sensors/tortured_phrases_detector.py:107` (LOW)

```python
severity = "high" if obvious else "high"  # curated list -> high by default
```

The `OBVIOUS_PHRASES` set is computed (line 106) and the `obvious` variable is used only to pick
between two identical values. Either drop the `OBVIOUS_PHRASES`/`obvious` machinery entirely, or
actually use it (e.g. `severity = "high" if obvious else "medium"`). Right now it is misleading
dead code that implies a distinction that does not exist.

---

## 4. Duplicated `canon_doi` in 6 places (LOW, quality / drift risk)

Identical implementations live in:
- `sensors/full_text_fetcher.py`
- `sensors/reference_integrity_checker.py`
- `graph_processing/wire_sensor_flags.py`
- `graph_processing/refresh_ori_findings.py`
- `graph_processing/load_pdf_manifest.py`
- `graph_processing/orcid_client.py`

They are currently byte-identical, but divergence is a real risk: DOIs arriving with/without
`doi.org` prefixes would break the flag↔paper joins in `wire_sensor_flags.py` silently.

**Fix:** promote one canonical `canon_doi` into a shared module (e.g. `graph_processing/_doi.py`)
and import it everywhere.

---

## 5. Inconsistent candidate scope — `sensors/reference_integrity_checker.py:246` (MEDIUM, correctness)

```python
"MATCH (p:Paper {is_retracted:false, source:'expansion'}) RETURN p.doi AS doi "
```

`reference_integrity_checker.py` filters to `source:'expansion'` papers, while **every other sensor**
(`retracted_citation_checker`, `journal_integrity_check`, `ai_text_tell_detector`,
`p_value_hacking_detector`, `tortured_phrases_detector`, `pubpeer_comment_checker`) scans all
`is_retracted:false` papers. So reference-integrity flags are only ever produced for expansion
papers — `wire_sensor_flags.py`'s `reference_integrity_flag_count` will be systematically missing
for seed papers. This looks like a copy-paste / testing artifact.

**Fix:** confirm intent; if it should match the other sensors, drop the `source:'expansion'`
predicate (or document why reference-integrity is expansion-only).

---

## 6. `resolve_connection` long import path / sys.path hacks (LOW, quality)

~22 modules do:
```python
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402
```

This couples every script to a helper that lives in an unrelated-named module
(`normalize_authors.py`) and relies on `sys.path` manipulation + `# noqa: E402`.
(See #1 — extracting `resolve_connection` to `graph_processing/_conn.py` resolves both issues.)

**Fix:** extract `resolve_connection` to a dedicated module and add a `pyproject.toml` / `src`
layout so imports are normal. Low priority but it is the single most-repeated smell in the repo.

---

## 7. Three copies of `WEIGHTS` — `tier_a_scoring.py` / `build_review_page.py` / `flag_evidence_report.py` (LOW, drift risk)

`plan.md:238` notes `build_review_page.py` predates the 2026-07-19 EoC + PubPeer additions that
`flag_evidence_report.py` predates. Both claim to mirror `tier_a_scoring.py`'s WEIGHTS, and the
weights also live a third time inline in `build_review_page.py` (line 90: "Must match
tier_a_scoring.py WEIGHTS exactly"). If a weight changes, all three must be hand-synced or they
drift apart silently.

**Fix:** import `WEIGHTS` from `tier_a_scoring` in both consumers (`tier_a_scoring` only touches
the DB inside `main()`, so the module is safe to import).

---

## 8. Broad `except Exception` swallows real errors into HIGH-severity flags (MEDIUM)

In `sensors/reference_integrity_checker.py`, `crossref_work_by_doi`, `crossref_search`,
`pubmed_search`, and `check_paper` all `return None` on **any** `Exception`. A network timeout and
a genuine "DOI not found" are indistinguishable, so a transient Crossref/PubMed failure is silently
recorded as "reference fabricated/garbled" — a **HIGH**-severity flag (lines 183-189). For a tool
whose premise is "high precision, never falsely accuse," swallowing transport errors into
HIGH-severity flags is a real risk.

**Fix:** distinguish `404`/empty results from transport errors; log or re-raise on the latter so
it surfaces in the report (or is skipped) rather than becoming a fabricated-misconduct flag.

---

## 9. `p_value_hacking_detector.py` — `from collections import Counter` inside a function (TRIVIAL)

Line 177 imports `Counter` inside `assess_paper`. Harmless, but it belongs at module top with the
other imports.

---

## 10. `image_forensics/` not in the scored path — intentional, but worth a guardrail (INFO)

The `sensors/image_forensics/*.py` scripts are standalone CLI tools that read local files and emit
JSON; they are only invoked by `runs/run_paperconan.py` (the paperconan wrapper), and their findings
are explicitly **never scored** (per `NOTICE.md` / `run_paperconan.py` docstrings). Consistent with
`plan.md §0`. The one gap: nothing prevents a future editor from adding an `image_*_flag_count` to
`WEIGHTS` and silently scoring unweighted signal.

**Fix:** add a one-line comment in `tier_a_scoring.py` (like the existing PubPeer note) locking that
intent.

---

## 11. No pipeline orchestrator (INFO)

`plan.md:180` documents the canonical pipeline order, but there is **no executable orchestrator**
(no `Makefile` / `run_all.sh` / `justfile` / `pyproject` script). Every stage is run by hand in the
right order. Given the strict ordering (`expand_targets` must precede the 2nd `apply_overrides`;
`refresh_*` after expansion; `wire_sensor_flags` after sensors), a small orchestrator would prevent
the easy foot-gun of running a stage out of order. Optional but high-value for a "re-run everything"
workflow.

---

## Priority summary

| # | Issue | Impact | Effort |
|---|-------|--------|--------|
| 1 | Orphan `normalize_authors.py` writes unused `SAME_AS` | High (silent graph pollution / attribution hazard) | S (delete/move) |
| 2 | `reference_integrity_checker:221` assigns a list, not a string | Med (bad titles in flags) | XS |
| 5 | `reference_integrity_checker` only scans `source:'expansion'` | Med (missing flags for seed) | XS |
| 8 | broad `except` -> HIGH "fabricated" flags on network errors | Med (false accusations) | S |
| 3 | no-op `obvious` ternary | Low | XS |
| 4,6 | duplicated `canon_doi` / `resolve_connection` | Low (drift risk) | M |
| 7 | 3 copies of `WEIGHTS` | Low (drift) | S |
| 9 | `Counter` imported inside function | Trivial | XS |
| 10 | `image_forensics` scoring guardrail | Info | XS |
| 11 | no pipeline orchestrator | Info | S |

**Fix first:** #1, #2, #5. #1 is a latent correctness/ethics hazard for a project whose entire
framing is "never mis-attribute"; #2 and #5 are concrete data-quality bugs in flag generation.
