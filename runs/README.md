# paperconan run archive (Phase 3 numeric-forensics)

One folder per paper, holding a paperconan run's **inputs, verbatim output, and
a distilled conclusion**, so a run's result is kept inside the project and never
has to be regenerated to be re-read.

## Naming convention

One directory per paper, named by **DOI with `/` replaced by `__`** — the same
convention as `data/pdfs/` and `data/full_text_cache/`.

Example: `10.3389/fimmu.2018.00063` → `runs/10.3389__fimmu.2018.00063/`

## Per-run layout

```
runs/<doi-with-__>/
  CONCLUSION.md   # distilled, human-readable takeaway + provenance (hand-written
                  #   from the audit). Facts-not-verdicts (plan.md §0): records the
                  #   SIGNAL, never asserts fraud. Read this first.
  meta.yaml       # structured provenance + finding counts, for a future
                  #   load_paperconan_runs.py replay onto the Paper node (same
                  #   idempotent YAML pattern as data/pdfs/manifest.yaml).
  data/           # paperconan's own run directory, kept VERBATIM as the tool
                  #   produced it — do not rewrite tool output:
    <input files>          # the .csv / .xlsx / .pdf actually scanned
    paperconan_source.json # what was fed in (doi, title, source, cand_id)
    audit/
      REPORT.md            # paperconan's human-readable findings report
      report.html          # same, styled
      scan.json            # machine-readable: tool_version, profile, scanned_at,
                           #   per-file/-sheet stats, every finding
```

## Adding a new run — use the wrapper

`runs/run_paperconan.py` drives the paperconan CLI so the conventions above are
enforced automatically:

```bash
python runs/run_paperconan.py 10.3389/fimmu.2018.00063            # DOI
python runs/run_paperconan.py <doi> --title "..." --images       # better fetch match + image assets
python runs/run_paperconan.py <doi> --force-fetch                # ignore cache, re-fetch
```

It:
1. Computes `runs/<doi-with-__>/data/` and points paperconan there, so the
   `data/audit/` layout lands automatically.
2. **Cache-first, then PMC-first:** if that `data/` already holds the paper's
   files, it scans them and does NOT hit the network. When absent, it acquires
   data in order: **(a)** the Europe PMC supplementary ZIP if one exists (the
   same source the review-page 📎 badge / `suppl_data_check.py` use — paperconan's
   own `fetch` cannot see PMC); **(b)** otherwise `paperconan fetch --auto`
   (open repositories: Zenodo/Dryad/figshare). `--skip-pmc` skips (a);
   `--force-fetch` ignores the cache.
   Two non-scan outcomes are recorded honestly in `meta.yaml` (never a clean
   pass): `no_data_files_available` (nothing fetched) and `no_tabular_data`
   (files fetched but figures/images only — re-run with `--images` for figure
   review). `hasSuppl=Y` in PMC does NOT guarantee a downloadable ZIP; when the
   ZIP endpoint 404s the wrapper falls back to repositories automatically.
3. Writes a **`meta.yaml` DRAFT** (mechanical fields + severity counts from
   `scan.json`) with `needs_adjudication: true` and a placeholder conclusion.

**You still hand-write the adjudication** (paperconan output is signal, not
verdict — §0). After reviewing, edit `meta.yaml`:
- write the real **`conclusion:`** line and the narrative **`CONCLUSION.md`**,
- **drop `needs_adjudication: true`**, and
- add an **`adjudicated:`** verdict — one of `false_positive`, `benign`,
  `inconclusive`, `needs_data`, `confirmed`.

The review card's paperconan badge reflects this: an unadjudicated run shows the
raw severity marked `(draft)`; once `adjudicated:` is set, the badge shows the
human verdict instead (e.g. a benign "26 high" becomes a calm grey "reviewed —
false positive", while `confirmed` shows loud/red).

Safety: the wrapper never clobbers an adjudicated `meta.yaml`. If one exists
without `needs_adjudication: true`, the fresh draft is written to
`meta.autodraft.yaml` instead, for you to compare and reconcile.

Everything under `data/` is left exactly as paperconan wrote it — `scan.json` is
the tool's own record of the run (including the input path it used).

## What paperconan is

A numeric-forensics Claude skill (see plan.md Phase 3): ~30 detectors over the
paper's supplementary data tables (identical/constant-offset/constant-ratio
columns, repeated values, decimal-tail clustering, GRIM/GRIMMER). Its runnable
set is the papers with fetchable supplementary data — see the `has_pmc_suppl` /
`pmc_suppl_url` flag from `sensors/suppl_data_check.py` (88 of 796 candidates).
"Signal not verdict" is paperconan's own stated principle, matching §0.
