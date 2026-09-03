# problem_paper — Fraud Academic Paper Scanner (POC)

A research-integrity triage tool for biology/microbiology papers. It builds a
Neo4j knowledge graph from the Retraction Watch database, enriches it with
OpenAlex/PubMed, runs a set of independent "sensor" checks and numeric
forensics over each paper, and rolls everything into a **ranked review queue**
for a human to look at — never an automated verdict.

**Status:** proof of concept. The goal is a working end-to-end pipeline on a
biology/microbiology slice of the literature, not production coverage of all
fields.

## 0. Read this first — the guardrail every part of this repo follows

> A retraction is not proof of fraud. Every output of this system is a
> **hypothesis for human review**, not an accusation.

The Retraction Watch database mixes deliberate misconduct (paper mills,
fabrication, image manipulation) with honest error, publisher mistakes, and
expressions of concern that were later reinstated. Because of that:

- There is **no `known_fraud` boolean** anywhere in the schema. Nothing in
  this codebase asserts that a person committed fraud.
- Authors are **never merged**. Every authorship is its own node
  (`AuthorInstance`); "same person" is a reversible, confidence-scored
  hypothesis (`PROBABLY_SAME_AS`), because OpenAlex and PubMed both
  occasionally mis-attribute identity (mis-assigned ORCIDs, name collisions),
  and a silent merge would attach a real person to someone else's retraction.
- **Same person ≠ same responsibility.** A misconduct finding on one paper is
  never read onto a person's other papers — author role (1st/corresponding
  vs. one of 17 middle authors) and decades of career distance both matter,
  and any derived person-level flag must point at the *specific* DOI(s) that
  produced it.
- The output is a **triage queue**: "papers a human should look at first, and
  why," each flag linked to its evidence. No cell in the review UI ever
  states that a paper or a person is fraudulent.

Full rationale lives in [`plan.md`](plan.md) §0; every design decision in this
repo traces back to it. Read that section before touching scoring or identity
code.

## 1. Architecture

```
Retraction Watch CSV ──▶ Loader / normalizer
                              │
OpenAlex (primary) ──enrich──▶ Neo4j graph ──▶ Tier-A scoring engine
PubMed (fallback)                 │                (weighted, sourced facts
                                   │                 only — see below)
                                   ▼
                          Phase-4 sensors (citations, PubPeer,
                          tortured phrases, AI-text tells, journal
                          integrity, reference integrity, ...)
                                   │
                                   ▼
                          suspicious not-yet-retracted papers
                                   │
                    paperconan (Claude skill, numeric
                    forensics on data tables) ── deep dive ──▶ review/
                                                          (private, local,
                                                           self-contained
                                                           HTML triage page +
                                                           FastAPI/HTMX
                                                           pipeline dashboard)
```

- **Graph DB:** Neo4j (Community + GDS), chosen over the originally-planned
  embedded LadybugDB because Neo4j GDS's node-classification pipeline is used
  for a Tier-B learned prior, and Browser/Cypher exploration was useful
  throughout the build. See `plan.md` §2 for the full engine comparison.
- **Enrichment:** OpenAlex is primary (ORCID, per-author affiliation,
  citation edges, keyless/`mailto`-only). PubMed/E-utilities is the fallback
  for missing records, not for ORCID top-up (OpenAlex's ORCID coverage is a
  strict superset in this corpus).
- **Scoring discipline:** `graph_processing/tier_a_scoring.py` is the only
  place a signal turns into a number. Only **hard, sourced facts** (sensor
  flags, an Expression of Concern, an ORI finding) are weighted in.
  Community/opinion/forensic signals — PubPeer comment counts, the GDS
  learned prior, paperconan's raw severity counts — are **labelled but never
  scored**. Weights live in [`config/weights.yaml`](config/weights.yaml),
  editable without a code change, with the reasoning for every weight (and
  every deliberately-excluded signal) documented inline.
- **Frontend:** FastAPI + Jinja2 + HTMX. Two separate local-only apps: a
  static self-contained triage page (`review/index.html`, built by
  `graph_processing/build_review_page.py`) and a pipeline-execution dashboard
  (`review/pipeline_app.py`) that turns every pipeline stage into a button
  with live status/output.
- **Deep numeric forensics:** [paperconan](https://pypi.org/project/paperconan/),
  a Claude skill, scans a paper's raw supplementary data tables (`.xlsx`/
  `.csv`) for fabrication fingerprints — duplicated values, impossible
  mean/SD combinations (GRIM/GRIMMER), decimal-tail clustering. Run per-paper
  via `runs/run_paperconan.py`, never the bare CLI (see `runs/README.md`).

## 2. Repo layout

| Path | What it is |
|---|---|
| `plan.md` | The design doc. Read §0 first; everything else (schema, phases, scoring rationale, risk log) is here. |
| `AGENTS.md` | Conventions for anyone (human or agent) working in this repo — wrapper scripts, naming, scoring discipline, privacy, reproduction. |
| `graph_processing/` | Loader, enrichment, identity resolution, targeted expansion, GDS scoring, the Tier-A scoring engine, and the review-page builder. |
| `sensors/` | The Phase-4 "flag sensor" skills — one script per independent signal (citations to retracted work, tortured phrases, AI-text tells, PubPeer, journal integrity, reference integrity, image-reuse screens, ...). Each emits evidence, never a verdict. |
| `graph_processing/run_content_sensors_on_selection.py` | Runs the content-based sensors (full-text + paperconan) on a reviewer-picked DOI shortlist, on demand — see §4.1. |
| `config/weights.yaml` | The only place scoring weights live; git-tracked so `git log -p` is the audit trail for every weight change. |
| `runs/` | One archived folder per `paperconan` run (inputs, verbatim tool output, a hand-written `CONCLUSION.md`). See `runs/README.md`. |
| `review/` | The generated, **private, git-ignored** HTML review queue, plus the pipeline-execution dashboard. See `review/README.md` — never publish or share `review/*.html`, it names real researchers. |
| `retraction_watch/` | Where the source Retraction Watch CSV goes (third-party data, not committed — see §3). |
| `data/` | All generated/cached data (graph exports, PDFs, full-text cache, run history). Git-ignored; carried between machines by hand. |
| `import_cypher.txt` | Reference Cypher for exploring the graph directly in Neo4j Browser. |
| `BUG.md` | Running log of bugs found and fixed, with root causes. |

## 3. Setup

### 3.1 Python environment

```bash
uv venv --python 3.11
uv pip install -r requirements.txt
# or, without uv:
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

`requirements.txt` is a full frozen snapshot (`uv pip freeze`), not a
hand-picked list — see its header comment for what pulls in what.

### 3.2 Source data (not committed)

Download the [Retraction Watch database CSV](https://retractionwatch.com/retraction-watch-database-user-guide/) and place it at
`retraction_watch/retraction_watch.csv`. It's third-party data (~62MB) and
deliberately git-ignored.

### 3.3 Credentials / config

Copy your own `.env.yaml` (git-ignored) with sections for `graph_db`,
`openalex`, `pubmed`, `crossref`, `orcid`, `pubpeer`, `paperconan`, and
`data`. Most external calls (OpenAlex, Crossref, PubMed, PubPeer) are
keyless — only a contact email (`mailto=`) is required for the "polite pool".
`graph_db.engine` defaults to `"neo4j"`; point it at a running Neo4j Community
instance with GDS installed.

### 3.4 Bootstrapping the graph

**If you have `data/graph/full_graph_snapshot.jsonl`** (carried by hand from
another machine, same as `.env.yaml` and the Retraction Watch CSV — `data/`
is git-ignored), the fast path is:

```bash
python graph_processing/import_graph_snapshot.py
```

This restores every node/relationship/property the full pipeline would have
produced, against a fresh Neo4j instance, in a couple of minutes — no
OpenAlex/Crossref/PubMed calls needed.

**Otherwise**, run the pipeline from scratch. The full stage order (also
enforced as buttons in `review/pipeline_app.py`) is:

```
build_instances.py
  → apply_overrides.py
  → expand_targets.py
  → refresh_retraction_status.py
  → refresh_editorial_notices.py
  → refresh_ori_findings.py
  → apply_overrides.py (2nd pass)
  → link_instances.py
  → cluster_instances.py
  → mark_adjudication.py
  → Phase-4 sensors (sensors/*.py) → wire_sensor_flags.py
  → gds_node_classification.py
  → tier_a_scoring.py
  → build_review_page.py
```

This is a lengthy, API-bound chain (OpenAlex/PubMed/PubPeer rate limits).
Prefer `review/pipeline_app.py`'s dashboard over running each stage by hand —
it runs stages as background subprocesses with live logs and prevents the
exact "a stage silently got skipped" failure mode this project hit twice
before the dashboard existed (see `BUG.md`).

After any run that meaningfully changes the graph, regenerate the snapshot so
the next machine (or the next `import_graph_snapshot.py` run) stays current:

```bash
python graph_processing/export_graph_snapshot.py
```

## 4. Running it

```bash
# Regenerate the scored triage CSV + the review page
python graph_processing/tier_a_scoring.py --top 500 -o data/tier_a_triage_full.csv
python graph_processing/build_review_page.py --top 50

# Open the review queue locally (never publish this file — see review/README.md)
python -m http.server 8899 --bind 127.0.0.1 --directory review
# then visit http://127.0.0.1:8899/index.html

# Pipeline dashboard (run/monitor any stage from a browser)
uvicorn review.pipeline_app:app --reload --port 8800
# then open http://127.0.0.1:8800/

# Deep numeric forensics on one paper (never call the paperconan CLI directly)
python runs/run_paperconan.py 10.3389/fimmu.2018.00063
```

### 4.1 On-demand content-based sensors (targeted review)

The Phase-4 sensors above split into two kinds. **Metadata-based** sensors
(citations to retracted work, journal/institution/publisher retraction
rates, author retraction history, ...) run automatically corpus-wide via
`wire_sensor_flags.py` and are cheap. **Content-based** sensors
(`tortured_phrases_detector.py`, `ai_text_tell_detector.py`,
`p_value_hacking_detector.py`, plus `paperconan`) need a paper's actual
full text or data tables, are slower, and are meant to be run only on
papers a reviewer has already flagged as worth a closer look — never
corpus-wide.

The loop:

```bash
# 1. Score the whole corpus on metadata alone (fast, already automatic)
python graph_processing/tier_a_scoring.py --top 500 -o data/tier_a_triage_full.csv

# 2. Pick a shortlist by eye from that CSV / review/index.html, one DOI per line
printf '10.1155/2016/2537294\n10.1016/j.nmni.2017.11.003\n' > shortlist.txt

# 3. Run the content-based sensors on just that shortlist
python graph_processing/run_content_sensors_on_selection.py --doi-file shortlist.txt
# add --skip-paperconan to run the 3 text sensors only, or --skip-text-sensors
# for paperconan only; --refresh ignores cached full text/data

# 4. Hand-adjudicate any paperconan runs it flags (never automatic --
#    see runs/README.md and plan.md §0): edit `adjudicated:` in
#    runs/<doi>/meta.yaml to needs_human/confirmed/benign/false_positive

# 5. Re-score -- tier_a_scoring.py re-reads Neo4j + runs/*/meta.yaml fresh
#    every time, so this picks up the new evidence with no wiring step
python graph_processing/tier_a_scoring.py --top 500 -o data/tier_a_triage_full.csv
```

Each sensor's `--doi` mode writes straight to that one `Paper` node, so this
never touches `wire_sensor_flags.py` or any paper outside the shortlist —
running it repeatedly on the same DOI just overwrites that DOI's own flags.
Expect a lot of 0-flag results: full-text coverage on this corpus is thin
(most candidates come back `not_open_access`), so a 0 usually means "no text
to check," not "checked and clean" — the point of this step is a cheap
second pass on the papers that already look suspicious, not a guarantee.

## 5. Privacy

`review/index.html` and everything under `review/` beyond the tracked
`README.md`/`pipeline_app.py` names real, named researchers next to
misconduct-adjacent flags. It is git-ignored on purpose and must **never** be
committed, published as a web artifact, or shared outside local use during
the POC (`plan.md` §7). Regenerate it locally instead of passing the file
around.

## 6. Known limitations

- **Discovery bias.** Retraction Watch reflects what *got caught*, skewed
  toward certain publishers and regions. A high score means "resembles a
  caught paper," not "is fraudulent."
- **Identity resolution is probabilistic.** ORCID is treated as a low-trust
  signal (OpenAlex is known to mis-assign it); a career move can look
  identical to a mis-assignment, so clustering is always human-reviewed, not
  auto-corrected.
- **paperconan coverage is inherently partial** — only papers with
  machine-readable supplementary data tables can be scanned (measured: 86 of
  796 active not-yet-retracted candidates, via Europe PMC).
- **Sensor precision varies.** Web-search-style and p-value sensors are
  noisy and are weighted low (or left unscored) accordingly; see
  `config/weights.yaml` for the full, documented weighting rationale.

See `plan.md` §7 for the complete, continuously-updated risk log.
