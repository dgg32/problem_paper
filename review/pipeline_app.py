#!/usr/bin/env python3
"""
pipeline_app.py — Phase 5 Increment 2 (plan.md): a FastAPI + HTMX ops
dashboard to trigger part or all of the pipeline from a browser.

Why this exists: plan.md's BUG.md #11 already flagged "no pipeline
orchestrator" as a risk -- every stage has to be run by hand, in the right
order, from the CLI. That's not theoretical: confirmed live 2026-07-21, a
full Neo4j rebuild in this same project silently skipped expand_targets.py
and three refresh_*.py stages (only surfaced when a reviewer asked about one
DOI's correction history and the absence didn't add up), and separately left
the entire Phase-4 sensor layer + gds_node_classification.py unrun (surfaced
only when familiar review-page tags were missing). This dashboard makes every
stage a button with visible last-run status, so a skipped stage is visible
at a glance instead of discovered incidentally days later.

FIRST DRAFT scope (see plan.md Phase 5 "Increment 2"): a fixed, hardcoded
registry of stages (STAGES below) -- never arbitrary user-supplied commands,
so there's no command-injection surface. Each stage is a real subprocess
(the same script you'd run from the CLI), started in a background thread and
polled via HTMX (`hx-trigger="every 2s"` while running, dropped once the
stage's own re-rendered row stops being "running" -- standard HTMX
poll-until-done pattern). State lives in memory only (lost on restart) plus
a small on-disk history file so "when did this last run" survives a restart.

NOT built yet (left for a later increment, per plan.md): a real task queue
(this uses plain threads -- fine for a single local operator, not for
concurrent multi-user use), hard dependency enforcement (a stage can be run
out of order; the UI shows recommended order and flags genuinely dangerous
stages with a confirm dialog, but doesn't block), and the reviewer-decision
backend (still browser localStorage only, per build_review_page.py).

Usage:
  source .venv/bin/activate
  uvicorn review.pipeline_app:app --reload --port 8800
  # then open http://127.0.0.1:8800/

Same privacy posture as review/index.html: this is a local-only tool for the
project's own operator, never meant to be exposed beyond localhost.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
HISTORY_PATH = REPO_ROOT / "data" / "pipeline_run_history.json"

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
app = FastAPI(title="Pipeline Ops Dashboard")


@dataclass
class Stage:
    id: str
    label: str
    category: str
    command: list[str]
    note: str = ""
    optional: bool = False   # excluded from "run all" / "run category" batches
    dangerous: bool = False  # gets an hx-confirm prompt before running


@dataclass
class StageState:
    status: str = "idle"          # idle | running | done | failed
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    duration_s: float | None = None
    output: list[str] = field(default_factory=list)  # tail of stdout/stderr


# ---------------------------------------------------------------------------
# Stage registry -- mirrors plan.md's documented pipeline order exactly.
# Commands match what was actually run and validated in this project's own
# session history (e.g. ai_text_tell_detector.py's --sample override, which
# defaults to only 100 papers if omitted -- a real gotcha hit live).
# ---------------------------------------------------------------------------
STAGES: list[Stage] = [
    # --- Identity & expansion pipeline (run in order) ---
    Stage("build_instances", "1. build_instances.py", "identity",
          [PYTHON, "graph_processing/build_instances.py"],
          "Rebuilds AuthorInstance/WROTE/AFFILIATED_WITH from graph.json. "
          "DESTRUCTIVE on a live graph that already has expansion data (see plan.md) -- "
          "only safe on a fresh/seed-only graph.",
          dangerous=True),
    Stage("apply_overrides_1", "2. apply_overrides.py (1st pass)", "identity",
          [PYTHON, "graph_processing/apply_overrides.py"],
          "Applies identity_overrides.yaml splits to seed-sourced instances."),
    Stage("expand_targets", "3. expand_targets.py", "identity",
          [PYTHON, "graph_processing/expand_targets.py"],
          "Pulls high-impact not-yet-retracted papers for the top-25 frequent-retraction "
          "clusters. Many OpenAlex/Crossref calls -- several minutes."),
    Stage("refresh_retraction_status", "4. refresh_retraction_status.py", "identity",
          [PYTHON, "graph_processing/refresh_retraction_status.py"],
          "Re-checks candidates against OpenAlex for retraction-status staleness."),
    Stage("refresh_editorial_notices", "5. refresh_editorial_notices.py", "identity",
          [PYTHON, "graph_processing/refresh_editorial_notices.py"],
          "PubMed Expression-of-Concern / Erratum notices."),
    Stage("refresh_ori_findings", "6. refresh_ori_findings.py", "identity",
          [PYTHON, "graph_processing/refresh_ori_findings.py"],
          "Federal Register ORI misconduct-finding cross-check."),
    Stage("apply_overrides_2", "7. apply_overrides.py (2nd pass)", "identity",
          [PYTHON, "graph_processing/apply_overrides.py"],
          "Re-run required: some splits target expansion-sourced papers that only "
          "exist after step 3 (confirmed live -- 0 matches before expansion, 14 after)."),
    Stage("link_instances", "8. link_instances.py", "identity",
          [PYTHON, "graph_processing/link_instances.py"],
          "Builds PROBABLY_SAME_AS identity edges."),
    Stage("cluster_instances", "9. cluster_instances.py", "identity",
          [PYTHON, "graph_processing/cluster_instances.py"],
          "Clusters instances into probable persons."),
    Stage("mark_adjudication", "10. mark_adjudication.py", "identity",
          [PYTHON, "graph_processing/mark_adjudication.py"],
          "Derives on_misconduct_paper / cluster_adjudicated."),

    # --- Phase-4 sensors ---
    Stage("retracted_citation_checker", "retracted_citation_checker.py", "phase4",
          [PYTHON, "sensors/retracted_citation_checker.py"],
          "Fast (~1s) -- pure Cypher over existing CITES edges."),
    Stage("external_retracted_citation_checker", "external_retracted_citation_checker.py", "phase4",
          [PYTHON, "sensors/external_retracted_citation_checker.py"],
          "~5 min -- batched OpenAlex lookups on candidates' external references."),
    Stage("journal_integrity_check", "journal_integrity_check.py", "phase4",
          [PYTHON, "sensors/journal_integrity_check.py"],
          "Fast (~1s) -- local DOAJ CSV + graph retraction-rate check."),
    Stage("ai_text_tell_detector", "ai_text_tell_detector.py", "phase4",
          [PYTHON, "sensors/ai_text_tell_detector.py", "--sample", "800"],
          "~30 min -- fetches full text (first sensor to warm the shared cache). "
          "--sample 800 required for full coverage; the default caps at only 100 papers."),
    Stage("p_value_hacking_detector", "p_value_hacking_detector.py", "phase4",
          [PYTHON, "sensors/p_value_hacking_detector.py", "--sample", "800"],
          "~30 min -- full-text based, shares the cache the previous stage warmed."),
    Stage("tortured_phrases_detector", "tortured_phrases_detector.py", "phase4",
          [PYTHON, "sensors/tortured_phrases_detector.py", "--limit", "800"],
          "~30 min -- full-text based, shares the cache the earlier stages warmed."),
    Stage("reference_integrity_checker", "reference_integrity_checker.py", "phase4",
          [PYTHON, "sensors/reference_integrity_checker.py"],
          "~2 HOURS. NOT scored (its no-DOI bibliographic-search route has a ~71% false-"
          "positive rate on this corpus) -- plan.md holds this out of routine rollout. "
          "Run manually only if you specifically want its unscored review-context output.",
          optional=True),

    # --- Graph features, GDS, scoring ---
    Stage("wire_sensor_flags", "wire_sensor_flags.py", "scoring",
          [PYTHON, "graph_processing/wire_sensor_flags.py"],
          "Imports data/flags/*.json sensor reports into Neo4j as Paper properties."),
    Stage("institution_retraction_rate", "institution_retraction_rate.py", "scoring",
          [PYTHON, "graph_processing/institution_retraction_rate.py"],
          "Per-institution retraction rate, max across a paper's INVOLVES institutions."),
    Stage("refresh_correction_history", "refresh_correction_history.py", "scoring",
          [PYTHON, "graph_processing/refresh_correction_history.py"],
          "Crossref updates:{doi} reverse lookup for correction/errata notices. Rate-limited."),
    Stage("publisher_retraction_rate", "publisher_retraction_rate.py", "scoring",
          [PYTHON, "graph_processing/publisher_retraction_rate.py"],
          "EXTERNAL publisher retraction rate: full Retraction Watch csv over a Crossref "
          "Members API total-dois denominator, not scoped to our own graph. Rate-limited "
          "(one Crossref call per distinct publisher)."),
    Stage("country_retraction_rate", "country_retraction_rate.py", "scoring",
          [PYTHON, "graph_processing/country_retraction_rate.py"],
          "EXTERNAL country retraction rate: full Retraction Watch csv over an OpenAlex "
          "per-country works-count denominator, not scoped to our own graph. Rate-limited "
          "(one OpenAlex call per distinct country)."),
    Stage("journal_retraction_rate_external", "journal_retraction_rate_external.py", "scoring",
          [PYTHON, "graph_processing/journal_retraction_rate_external.py"],
          "EXTERNAL journal retraction rate: full Retraction Watch csv over a Crossref "
          "Journals API total-dois denominator, not scoped to our own graph. Complements "
          "(does not replace) the graph-internal journal_retr_rate. Rate-limited "
          "(one or two Crossref calls per distinct journal)."),
    Stage("gds_node_classification", "gds_node_classification.py", "scoring",
          [PYTHON, "graph_processing/gds_node_classification.py"],
          "GDS FastRP embeddings + train/predict misconduct-class probability. A few seconds."),
    Stage("tier_a_scoring", "tier_a_scoring.py", "scoring",
          [PYTHON, "graph_processing/tier_a_scoring.py", "--top", "500",
           "-o", "data/tier_a_triage_full.csv"],
          "Computes the weighted Tier-A score for the top 500 candidates."),
    Stage("build_review_page", "build_review_page.py", "scoring",
          [PYTHON, "graph_processing/build_review_page.py", "--top", "795"],
          "Regenerates review/index.html (the triage queue itself)."),

    # --- Reproducibility ---
    Stage("export_graph_snapshot", "export_graph_snapshot.py", "snapshot",
          [PYTHON, "graph_processing/export_graph_snapshot.py"],
          "Streams the full graph to data/graph/full_graph_snapshot.jsonl for cross-machine reuse.",
          optional=True),
    Stage("import_graph_snapshot", "import_graph_snapshot.py", "snapshot",
          [PYTHON, "graph_processing/import_graph_snapshot.py"],
          "Restores from that snapshot file. MERGE-based/idempotent, but only meaningful "
          "right after a fresh Neo4j instance -- confirm before running against a graph "
          "you've since changed, since it overwrites matching properties back to the snapshot.",
          optional=True, dangerous=True),
]

CATEGORY_LABELS = {
    "identity": "Identity & Expansion Pipeline (run in order)",
    "phase4": "Phase-4 Sensors",
    "scoring": "Graph Features, GDS & Scoring",
    "snapshot": "Reproducibility Snapshot (manual, opt-in)",
}
CATEGORY_ORDER = ["identity", "phase4", "scoring", "snapshot"]

STAGE_BY_ID: dict[str, Stage] = {s.id: s for s in STAGES}
STATE: dict[str, StageState] = {s.id: StageState() for s in STAGES}
STATE_LOCK = threading.Lock()

# "Run all" / "run category" only ever include non-optional stages -- the
# slow reference_integrity_checker and the reproducibility scripts stay
# manual, single-button opt-ins (see module docstring).
FULL_PIPELINE_ORDER = [s.id for s in STAGES if not s.optional]

BATCH_LOCK = threading.Lock()
BATCH_STATE: dict = {"running": False, "current": None, "index": 0, "total": 0, "stopped_reason": None}


def _load_history() -> None:
    """Best-effort restore of last-run info across an app restart."""
    if not HISTORY_PATH.exists():
        return
    try:
        data = json.loads(HISTORY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return
    for stage_id, rec in data.items():
        if stage_id in STATE:
            st = STATE[stage_id]
            st.status = "done" if rec.get("exit_code") == 0 else "failed"
            st.started_at = rec.get("started_at")
            st.finished_at = rec.get("finished_at")
            st.exit_code = rec.get("exit_code")
            st.duration_s = rec.get("duration_s")


def _save_history() -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        sid: {
            "started_at": st.started_at, "finished_at": st.finished_at,
            "exit_code": st.exit_code, "duration_s": st.duration_s,
        }
        for sid, st in STATE.items() if st.finished_at
    }
    HISTORY_PATH.write_text(json.dumps(data, indent=2))


_load_history()


def _run_stage(stage_id: str) -> None:
    stage = STAGE_BY_ID[stage_id]
    st = STATE[stage_id]
    with STATE_LOCK:
        st.status = "running"
        st.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.finished_at = None
        st.exit_code = None
        st.duration_s = None
        st.output = []

    t0 = time.time()
    exit_code = -1
    try:
        proc = subprocess.Popen(
            stage.command, cwd=REPO_ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            with STATE_LOCK:
                st.output.append(line.rstrip("\n"))
                st.output = st.output[-300:]  # keep tail only, avoid unbounded growth
        proc.wait()
        exit_code = proc.returncode
    except Exception as e:  # noqa: BLE001 -- must never crash the polling thread
        with STATE_LOCK:
            st.output.append(f"ERROR launching process: {e}")

    with STATE_LOCK:
        st.status = "done" if exit_code == 0 else "failed"
        st.exit_code = exit_code
        st.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.duration_s = round(time.time() - t0, 1)
    _save_history()


def _run_batch(stage_ids: list[str]) -> None:
    with BATCH_LOCK:
        BATCH_STATE.update(running=True, index=0, total=len(stage_ids), stopped_reason=None, current=None)
    for i, sid in enumerate(stage_ids, 1):
        with BATCH_LOCK:
            BATCH_STATE.update(current=sid, index=i)
        _run_stage(sid)
        if STATE[sid].status == "failed":
            with BATCH_LOCK:
                BATCH_STATE.update(running=False,
                                    stopped_reason=f"{STAGE_BY_ID[sid].label} failed "
                                                    f"(exit {STATE[sid].exit_code}) -- batch stopped")
            return
    with BATCH_LOCK:
        BATCH_STATE.update(running=False, current=None, stopped_reason=None)


def _categories() -> list[dict]:
    return [
        {
            "id": cat_id,
            "label": CATEGORY_LABELS[cat_id],
            "stages": [(s, STATE[s.id]) for s in STAGES if s.category == cat_id],
        }
        for cat_id in CATEGORY_ORDER
    ]


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "pipeline.html", {
        "categories": _categories(), "batch": BATCH_STATE,
    })


@app.post("/run/{stage_id}", response_class=HTMLResponse)
def run_stage(request: Request, stage_id: str):
    stage = STAGE_BY_ID.get(stage_id)
    if stage is None:
        return HTMLResponse("unknown stage", status_code=404)
    if STATE[stage_id].status != "running" and not BATCH_STATE["running"]:
        threading.Thread(target=_run_stage, args=(stage_id,), daemon=True).start()
    return templates.TemplateResponse(request, "_stage_row.html", {
        "stage": stage, "state": STATE[stage_id], "batch": BATCH_STATE,
    })


@app.get("/status/{stage_id}", response_class=HTMLResponse)
def stage_status(request: Request, stage_id: str):
    stage = STAGE_BY_ID.get(stage_id)
    if stage is None:
        return HTMLResponse("unknown stage", status_code=404)
    return templates.TemplateResponse(request, "_stage_row.html", {
        "stage": stage, "state": STATE[stage_id], "batch": BATCH_STATE,
    })


@app.post("/run-category/{category_id}", response_class=HTMLResponse)
def run_category(request: Request, category_id: str):
    ids = [s.id for s in STAGES if s.category == category_id and not s.optional]
    if not BATCH_STATE["running"] and ids:
        threading.Thread(target=_run_batch, args=(ids,), daemon=True).start()
    return templates.TemplateResponse(request, "_batch_status.html", {"batch": BATCH_STATE})


@app.post("/run-all", response_class=HTMLResponse)
def run_all(request: Request):
    if not BATCH_STATE["running"]:
        threading.Thread(target=_run_batch, args=(FULL_PIPELINE_ORDER,), daemon=True).start()
    return templates.TemplateResponse(request, "_batch_status.html", {"batch": BATCH_STATE})


@app.get("/batch-status", response_class=HTMLResponse)
def batch_status(request: Request):
    return templates.TemplateResponse(request, "_batch_status.html", {"batch": BATCH_STATE})


@app.get("/refresh", response_class=RedirectResponse)
def refresh():
    return RedirectResponse(url="/")
