# Agent guide — problem_paper

Research-integrity paper-scanner POC: a Neo4j graph + Python sensors that triage
biology/microbiology papers for review, plus a private local review frontend.
Read `plan.md` for the full design; `§0` is the governing principle — every
output is a **hypothesis for human review, not an accusation**; never assert
fraud, and "same person ≠ same responsibility."

## Conventions any agent must follow here

### Running paperconan (numeric-forensics on paper data tables)

**Always use the wrapper, never the raw CLI:**

```bash
python runs/run_paperconan.py <doi>
```

It enforces conventions the bare `paperconan` CLI doesn't know about: output to
`runs/<doi-with-__>/` (DOI `/` → `__`), **cache-first** (reuse
`runs/<doi>/data/` if the files are already there, only fetch when absent), and
a `meta.yaml` provenance draft that never overwrites an adjudicated one. After a
run, hand-write `CONCLUSION.md` + the real `conclusion:` in `meta.yaml` —
paperconan output is signal, not verdict. See `runs/README.md`.

### File-path / DOI naming

Paper-scoped files are keyed by **DOI with `/` replaced by `__`** (e.g.
`10.3389/fimmu.2018.00063` → `10.3389__fimmu.2018.00063`). Used by `runs/`,
`data/pdfs/`, and `data/full_text_cache/`. See `data/pdfs/README.md`.

### Scoring discipline

`graph_processing/tier_a_scoring.py` holds the weighted triage score. Only
**hard, sourced facts** are weighted in (sensor flags, Expression of Concern,
ORI findings). Community/opinion/forensic signals — PubPeer comment counts, the
GDS learned prior, paperconan results — stay **labelled but NOT scored**. Keep
that separation; don't fold a soft signal into the weighted score.

### Privacy

The review page (`review/*.html`, built by
`graph_processing/build_review_page.py`) names real researchers. It is
git-ignored and **must never be published or shared** (plan.md §7) — keep it a
local file.

### Reproducing the graph on a new machine

Before running the full pipeline from scratch (`build_instances.py → ... →
mark_adjudication.py → Phase-4 sensors → gds_node_classification.py →
tier_a_scoring.py` — a lengthy, API-bound chain), check for
`data/graph/full_graph_snapshot.jsonl`. If present, run
`python graph_processing/import_graph_snapshot.py` instead against a fresh
Neo4j instance — it restores every node/relationship/property the whole
pipeline would have produced, in a couple of minutes, no OpenAlex/Crossref/
PubMed calls needed. `data/` is git-ignored and carried between machines by
hand (same as `.env.yaml` and `retraction_watch.csv`), so this file travels
with the rest of `data/graph/` — no separate copy step.

After any run that meaningfully changes the graph (a new sensor, a fresh
Phase-4 scan, expanded targets), re-run
`python graph_processing/export_graph_snapshot.py` so the snapshot stays
current for the next machine. See both scripts' docstrings for how the
export/import mechanism works (streamed APOC JSON export, matched on each
label's natural key on import — never Neo4j's internal id).

### New automated data sources

Before adding any script that auto-fetches from an external site, check its
`robots.txt` and flag any named-bot block to the user for sign-off rather than
deciding unilaterally.
