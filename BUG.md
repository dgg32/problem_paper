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

---

# Round 3 — bug hunt, 2026-09-03

Scope: full-repo sweep of `graph_processing/`, `sensors/` (incl. `image_forensics/`),
`review/pipeline_app.py`, and `config/weights.yaml`. Every finding below was verified against
source in a second pass, and graph-dependent claims were checked with live Cypher counts
(not noted inline where the check mattered). Round 1 items #1-#11 and Round 2 items R2-1..R2-3
are treated as fixed and are not re-reported; Round 2's three "deliberate" non-fixes are
likewise excluded.

**Update, 2026-09-03 (same day):** all 15 findings below (R3-1..R3-15) were fixed and verified
in a follow-up pass -- syntax-compiled, and where a live check was practical, run against the
real Neo4j instance (a direct sensor invocation, an isolated `EXPLAIN` of the new Cypher, or a
throwaway-node transaction smoke test). See each finding's **Fix** paragraph for what changed and
how it was checked. Left as originally reported (not attempted): retry-with-backoff for R3-1's
two fetch helpers -- out of scope for stopping the silent corpus-wide wipe, which the fail-closed
change already does.

### R3-1. Transient API failure silently wipes a scored signal corpus-wide — `sensors/external_retracted_citation_checker.py` + `graph_processing/wire_sensor_flags.py` (HIGH, correctness) — FIXED

`fetch_reference_dois` returns `[]` on ANY `requests.RequestException` (no retry, 429 included,
lines 96-97) and `openalex_retracted_batch` returns `{}` likewise (lines 115-116). `main()` then
unconditionally writes the report (lines 213-214). `wire_sensor_flags.py` correctly treats a
present-but-empty report as "sensor ran, found nothing" and resets
`external_retracted_citation_flag_count` to 0 across all papers (lines 160-166). Composite
failure: one Crossref/OpenAlex outage during a routine run produces an empty report, and the next
`wire_sensor_flags.py` run silently zeroes a weight-2.0 scored signal for the whole corpus until
a fully successful re-run. A partial variant is harder to notice: sporadic 429s at concurrency 4
silently drop individual papers' reference lists from this run's report, and the reset then
erases their previously-earned flags too (both fetch pools run at concurrency 3,
external_retracted_citation_checker.py:68,73). The same empty-report-on-failure shape exists in
the other sensors; this one is the highest-impact instance because it is scored and on the
routine path.

**Fix (2026-09-03):** `fetch_reference_dois`/`openalex_retracted_batch` now return `(result, ok)`
instead of silently coercing a transport failure to an empty result. `main()` counts failures
across both phases and, if any occurred, prints an explicit warning and `sys.exit(1)` **without
writing `REPORT_JSON`** — the existing report (and hence the existing graph flags, via
`wire_sensor_flags.py`'s reset-then-rewrite) is left untouched until a clean run succeeds.
Retry-with-backoff was left out of scope (a separate enhancement, not needed to stop the silent
wipe). Live-verified: `--doi` mode ran clean against a real DOI with no regression (23 external
references checked, 0 false aborts against healthy Crossref/OpenAlex).

### R3-2. Paper-level external institution rate defaults to 0.0 (missing == zero) — `graph_processing/institution_retraction_rate.py:251` (MEDIUM, correctness) — FIXED

The per-paper reset writes `p.institution_retr_rate_external = 0.0` (not null, line 251), and the
copy step (lines 279-289) only overwrites papers whose institutions carry a non-null
`retraction_rate_external`. A paper whose institution has `global_retraction_count > 0` but an
unresolvable ROR (`no_ror_hit` path, lines 220-222, also hit by any transient OpenAlex request
failure) therefore shows a clean 0.000% on a SCORED (minmax) signal. This contradicts the
module's own "missing != zero" rule (docstring lines 87-89) and misleads the review card.
`tier_a_scoring.py` compounds it: `coalesce(p.institution_retr_rate_external, 0.0)` (QUERY line
459) erases the null/unknown distinction at scoring time even if the reset were fixed. The
sibling scripts reset journal/country paper-level rates to null; institution is the inconsistent
one. Live check 2026-09-03: the no_ror_hit population is currently 0, so this is a correctness
landmine rather than a live scoring error.

**Fix (2026-09-03):** the paper-level reset now sets `institution_retr_rate` and
`institution_retr_rate_external` to `null` instead of `0.0`, matching the sibling journal/country
scripts' semantics. `tier_a_scoring.py` already `coalesce()`s both to `0.0` at scoring time (QUERY
lines 456/459, unchanged), so live scoring is unaffected — this fixes the graph's own "missing !=
zero" honesty for the review card, not the score.

### R3-3. Institution-level external rate is never cleared — same script, lines 229-236 (MEDIUM, idempotency) — FIXED

`retraction_rate_external` is written only for institutions that resolved this run; there is no
reset statement for it. An institution that resolved in run N but hits `no_ror_hit` in run N+1
keeps its stale rate, which then propagates to paper level via the copy step. Contradicts the
docstring's "Idempotent: recomputes both Institution and Paper properties from scratch every
run."

**Fix (2026-09-03):** added a reset statement (`MATCH (i:Institution) SET
i.retraction_rate_external/_n/_total = null`) right before the external-rate lookup loop,
mirroring the journal/country pattern. Cypher syntax validated live via `EXPLAIN` against the
real schema.

### R3-4. DEFAULT_WEIGHTS diverges from the audited weights.yaml on `country_retr_rate_minmax_target` — `graph_processing/tier_a_scoring.py:327` vs `config/weights.yaml:188` (MEDIUM, drift) — FIXED

`DEFAULT_WEIGHTS` hardcodes `country_retr_rate_minmax_target: 10.0`, but the audited config (the
documented 2026-07-22 user decision, with the full Saudi-Arabia granularity rationale) is `2.5`,
and weights.yaml's own header promises the fallback is "same values as below". Today the YAML
overlays the default, so live scoring is correct; but if `config/weights.yaml` is ever missing or
unparseable, the fallback silently re-raises the coarsest ecological signal 4x beyond its
documented, reasoned value. Every other one of the 23 remaining keys (24 total) matches.

**Fix (2026-09-03):** `DEFAULT_WEIGHTS["country_retr_rate_minmax_target"]` updated 10.0 -> 2.5 to
match `config/weights.yaml`, with a comment pointing at the YAML's own rationale. Live-verified via
`tier_a_scoring.py --top 5`: still reads `2.5` (from the YAML overlay, unchanged) and runs clean.

### R3-5. Editorial-notice refresh is not idempotent for the "now-clear" case — `graph_processing/refresh_editorial_notices.py:179` (MEDIUM, correctness) — FIXED

Papers classified `none` hit `continue` before the write step, so `pubmed_eoc_status` /
`pubmed_eoc_date` / `pubmed_eoc_source_doi` written by an earlier run survive after PubMed stops
reporting the notice. Since `tier_a_scoring.py` keys `pubmed_eoc_flag` off
`pubmed_eoc_status = "expression_of_concern"` (weight 10.0, the largest single weight in the
system), a stale status keeps contributing +10 indefinitely. Verified: the script contains
exactly one write site for `pubmed_eoc_status` (line 206) and no clearing statement anywhere.

**Fix (2026-09-03):** the `none` branch now appends an explicit clearing update
(`status="none"`, `date`/`source_doi`/`source_citation` all `None`) instead of `continue`-skipping,
so a paper whose EoC/Erratum notice no longer shows up in PubMed's CommentsCorrectionsList gets
its stale fields nulled on the very next run, same as every other candidate. Also matches the
module's own docstring, which already documented `pubmed_eoc_status: "none"` as a real written
value — the old code never actually wrote it.

### R3-6. ORI matching is case-sensitive against the graph — `graph_processing/refresh_ori_findings.py:123,142,151,170` (MEDIUM, latent) — FIXED

`doi_to_finding` keys are `canon_doi(...)` (lowercased) but `graph_dois` are raw `p.doi` values,
and both the reporting and write MATCHes compare exact case. Live check 2026-09-03: 0 uppercase
DOIs among graph papers, so nothing is lost today; but Elsevier-style uppercase-suffix DOIs
(e.g. `10.1016/S0895-4356(00)00298-4`) are common in publisher deposits, and one entering the
graph would silently drop the system's strongest signal (`ori_finding_flag`, weight 4.0) with no
warning.

**Fix (2026-09-03):** `doi_to_finding` (already lowercased via `canon_doi`) is now compared
against a `{lowercased: original-cased}` map of the graph's own DOIs, and `matches` is keyed by
the graph's ORIGINAL casing -- so the write `MATCH (p:Paper {doi:$doi})` (exact equality) actually
finds the node even for an uppercase-suffix DOI. Unit-tested in isolation with a synthetic
Elsevier-style uppercase DOI: match found, original casing preserved for the write step.

### R3-7. GDS prep labels are additive only; retraction-state flips leave stale labels — `graph_processing/gds_node_classification.py:86-106` (MEDIUM, idempotency) — FIXED

`:LabeledPaper` and `:CandidatePaper` are only ever SET, never REMOVEd. Retraction Watch records
160 reinstatements, so "retracted -> not retracted" flips are plausible: a reinstated paper keeps
`:LabeledPaper` while `prep` overwrites its `misconduct_label` to -1, putting an invalid class
value into a binary training target (`targetNodeLabels: ['LabeledPaper']`) — at best a training
error, at worst silent mis-training. The reverse flip (newly retracted) leaves the paper in
`:CandidatePaper`, so it gets re-predicted and can appear in the "TOP 15 CANDIDATES" printout.
Contradicts the docstring's "Pipeline is fully idempotent".

**Fix (2026-09-03):** `prep()` now opens with exactly that statement, `MATCH (p:Paper) REMOVE
p:LabeledPaper, p:CandidatePaper`, before re-assigning either label. Cypher syntax validated live
via `EXPLAIN` against the real schema.

### R3-8. Duplicate entry in the AI-tell pattern list double-counts — `sensors/ai_text_tell_detector.py:80-81` (MEDIUM, trivial fix) — FIXED

`"as an ai, i cannot"` appears twice in `MEDIUM_CONFIDENCE_PATTERNS`; the per-pattern scan loop
emits one flag per matching entry, so a single occurrence yields two identical flags, inflating
`ai_text_tell_flag_count` by 1 (weight 2.0 per flag) and duplicating the finding on the review
page.

**Fix (2026-09-03):** deleted the duplicate `"as an ai, i cannot"` line.

### R3-9. Network errors become permanent "suppl_not_downloadable" facts — `sensors/suppl_data_check.py:82-83,102` (MEDIUM, correctness) — FIXED

`zip_available` returns False on any `requests.RequestException` (timeout, reset, 5xx), and
`classify` routes that False to the definitive `"suppl_not_downloadable"` state (docstring: "ZIP
endpoint 404s"). A transient Europe PMC hiccup is therefore persisted as a graph fact that gates
the downstream forensic image/data-table sensors, with no retry path.

**Fix (2026-09-03):** `zip_available` now returns a tri-state (`True`/`False`/`None`) instead of
coercing a `RequestException` to `False`; `classify()` routes the new `None` case to a distinct
`suppl_check_failed` status, reserving `suppl_not_downloadable` for a confirmed non-200 response.
Live-verified via `--dry-run --limit 5`: one of the five candidates checked
(`10.3389/fmicb.2021.786233`) hit a real transient HEAD-request failure during the test run and
was correctly classified `suppl_check_failed` rather than being recorded as a permanent
`suppl_not_downloadable` -- caught the exact failure mode live, not just in theory.

### R3-10. PubPeer review fields have no stale-state reset — `graph_processing/wire_sensor_flags.py:196-226` (LOW-MEDIUM, consistency) — FIXED

The scored sensors got reset-then-rewrite semantics in R2-1, but the pubpeer block still only
writes DOIs present in the current report and does nothing when the report is empty-but-present.
A paper that drops out of `pubpeer_flags.json` keeps its old `pubpeer_check_status` /
`pubpeer_comments_total` / `pubpeer_has_author_response` / `pubpeer_last_commented` forever on
the review page. Unscored, so no score impact; purely stale-evidence risk.

**Fix (2026-09-03):** the block now gates on `PUBPEER_FLAGS.exists()` (not truthiness of the parsed
records, so a present-but-empty report is no longer silently skipped) and resets all five pubpeer
properties across every Paper that has any before rewriting from the current report -- same
reset-then-rewrite idiom R2-1 already gave the scored sensors. Cypher syntax validated live via
`EXPLAIN` against the real schema.

### R3-11. Duplicate input paths produce self-comparison HIGH image-reuse findings — `sensors/image_forensics/integrity_common.py` `iter_files` + `image_similarity_screen.py:46-58` (LOW) — FIXED

`iter_files` appends every matching file per supplied path with no dedup by resolved path, and
the similarity screen compares `files[i]` against `files[i+1:]`. Passing overlapping paths (the
same directory twice, or a dir plus its parent) compares a file against itself: hamming 0,
flagged HIGH "Potential image reuse". Unscored today, but the findings land in paperconan
evidence bundles.

**Fix (2026-09-03):** `iter_files` now dedupes on `path.resolve()` via a `seen` set before
appending, for both the direct-file and directory-walk branches.

### R3-12. reference_integrity_checker: dead `year` parameter; `--sample` is not random — `sensors/reference_integrity_checker.py:135-145,319-321` (LOW) — FIXED

(a) `crossref_search(title, author, year)` never uses `year` in the Crossref query, so the
documented "year off by 1" check cannot work: a reference whose only error is the year can still
match well enough to be judged OK (score > 50 -> None). (b) `--sample`'s help says "random"
papers but the query is `ORDER BY p.cited_by_count DESC LIMIT $lim` — deterministic top-cited,
biasing spot-checks.

**Fix (2026-09-03):** (a) `crossref_search` now appends `year` to the bibliographic query string
when present (same free-text pattern already used for `author`), so a year mismatch actually
affects Crossref's ranking instead of being silently dropped. (b) `--sample N` now runs a genuinely
separate `ORDER BY rand()` query; the no-flag default keeps the original deterministic top-cited
query (that default's determinism looked intentional, unrelated to `--sample`'s "random" promise).
Live-verified: `--doi` ran clean with the year now included in the query (produced a real,
correctly-unresolved flag for a genuinely unfindable reference).

### R3-13. Institution external rate has no >1.0 sanity guard — `graph_processing/institution_retraction_rate.py:225` (LOW) — FIXED

`rate = n / works_count` is stored unchecked; the sibling scripts
(`journal_retraction_rate_external.py`, `publisher_retraction_rate.py`) discard rates > 1.0. A
tiny OpenAlex works_count against a conservative exact-match numerator can produce a >100% rate
that then anchors the minmax scale for the whole category (see tier_a_scoring's own warning
about single-outlier minmax anchors).

**Fix (2026-09-03):** added the sibling scripts' `if rate > 1.0: skip` guard (with a log line
naming the institution, n, and works_count) right where `rate` is computed.

### R3-14. Snapshot round-trip turns Neo4j Dates into Strings — `graph_processing/export_graph_snapshot.py` / `import_graph_snapshot.py:144,154` (LOW, latent) — FIXED

`expand_targets.py:207` and `refresh_retraction_status.py:218` store `published_date` /
`retraction_date` as Neo4j `date()` values; APOC JSON export serializes them as ISO strings and
the import's `SET n += r.props` restores them as String properties. No pipeline code consumes
these temporally today (grep-verified), so impact is limited to the snapshot's "restore exactly
what the pipeline would have produced" guarantee being structurally untrue, plus any future
Cypher that expects a temporal type on a restored graph.

**Fix (2026-09-03):** added a `DATE_PROPS` table (documented next to `NATURAL_KEY`, currently
`Paper.published_date`/`Paper.retraction_date`) and convert those string values back to
`neo4j.time.Date` objects (via `Date.from_iso_format`) at parse time, before they're sent as
Cypher parameters -- the driver serializes a native `Date` object as a real temporal type, not a
string. Confirmed `neo4j.time.Date.from_iso_format` works against this project's installed driver
version.

### R3-15. Identity layer rewrite is not atomic — `graph_processing/link_instances.py:203-215` (LOW, operational) — FIXED

`MATCH ()-[r:PROBABLY_SAME_AS]->() DELETE r` runs as one auto-commit, then edge writes follow in
separate auto-committed UNWIND batches. A crash between the delete and the last batch leaves a
partial identity layer, and a subsequent `cluster_instances.py` run would cluster over the
truncated edges. Re-running `link_instances.py` repairs it, but nothing detects the state.

**Fix (2026-09-03):** the delete and every UNWIND write batch now run inside one explicit
`session.begin_transaction()` / `tx.commit()` block, so a crash partway through rolls back to the
pre-run state instead of leaving a truncated identity layer. Live-verified with an isolated smoke
test against the real database (throwaway `__BugFixSmokeTest` nodes/rel-type, never touching real
`AuthorInstance`/`PROBABLY_SAME_AS` data): the transaction committed correctly and cleanup left no
residue.

### Verified non-issues (checked this round, deliberately not reported)

- **paperconan scoring path:** all adjudicated `runs/*/meta.yaml` files carry the
  `adjudicated:` key tier_a_scoring reads (`needs_human` / `false_positive` / `benign`
  observed); the two runs with a TODO conclusion correctly score as None. Live-verified.
- **`cluster_misconduct_dois` (mark_adjudication.py:68-72):** the unfiltered
  `-[:WROTE]->(p:Paper)` collect looks like it would gather innocent papers, but the
  AuthorInstance-one-WROTE-edge invariant (one authorship per node) makes `p` the misconduct
  paper itself by construction. Live-verified: 0 instances with `on_misconduct_paper` have >1
  WROTE edge, and every stored list contains only retracted DOIs.
- **Uppercase DOIs:** 0 in the graph (live count), so R3-6 is latent, not live.
- **Sensor JSON contract:** all 7 `SENSOR_CONFIG` entries' `doi_key` match what the sensors
  actually write (`citing_paper_doi` vs `paper_doi`); no silently dropped flags.
- **Weights drift beyond R3-4:** every other DEFAULT_WEIGHTS key matches weights.yaml.
- **Review page escaping:** every externally-sourced string in `build_review_page.py` passes
  through `esc()`/`esc_title()`; no injection surface found.
- **HTTP hygiene in the enrichment family:** timeouts + `mailto` + 429/5xx retry present across
  the refresh_*/rate scripts; no missing-timeout hang risk found.
- **apply_overrides / expand_targets / add_paper_by_doi:** dedup and override precedence sound;
  no double-add or unbounded-loop risk.

### Priority summary

| # | Issue | Impact | Effort | Status |
|---|-------|--------|--------|--------|
| R3-1 | empty-report-on-outage wipes a scored signal corpus-wide | High (silent signal loss) | M | FIXED |
| R3-5 | stale pubmed_eoc_status keeps +10.0 scoring | High (scored, plausible) | XS | FIXED |
| R3-7 | stale GDS labels can poison training with class -1 | Med (Tier-B integrity) | XS | FIXED |
| R3-2 | institution external rate 0.0 default (missing == zero) | Med (scored, currently latent) | S | FIXED |
| R3-4 | country minmax target 10.0 fallback vs audited 2.5 | Med (silent re-weight on fallback) | XS | FIXED |
| R3-3 | institution external rate never cleared (stale) | Med | XS | FIXED |
| R3-9 | network error -> permanent suppl_not_downloadable | Med (gates forensics) | S | FIXED |
| R3-8 | duplicate AI-tell pattern double-counts | Low-Med (scored +2.0) | XS | FIXED |
| R3-10 | pubpeer review fields never reset | Low-Med (stale evidence) | S | FIXED |
| R3-6 | ORI DOI case sensitivity | Low (latent; strongest signal) | XS | FIXED |
| R3-13 | institution rate >1.0 unguarded | Low | XS | FIXED |
| R3-14 | snapshot Dates -> Strings | Low (round-trip guarantee) | S | FIXED |
| R3-15 | identity rewrite not atomic | Low (operational) | S | FIXED |
| R3-12 | dead year param; non-random --sample | Low | XS | FIXED |
| R3-11 | duplicate paths -> self-comparison HIGH | Low | XS | FIXED |

**Fixed first (as planned):** R3-1, R3-5, R3-7 were the top priority -- R3-1 was the only finding
that could silently corrupt the triage ranking at corpus scale from a routine run; R3-5 quietly
inflated the single largest weight in the system; R3-7 could crash or poison the Tier-B training
the next time a paper flips retraction state. All three, and the remaining 12, are now fixed (see
each finding's **Fix** paragraph above).
