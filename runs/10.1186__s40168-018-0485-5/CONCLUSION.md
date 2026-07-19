# paperconan conclusion — 10.1186/s40168-018-0485-5

*Fecal microbiota transplantation / microbiome study* (Microbiome, 2018).

- **Run:** paperconan 0.8.2, `forensic` profile, 2026-07-19
- **Source of files:** Europe PMC (PMC5966928) — auto-downloaded by `run_paperconan.py` (PMC-first)
- **Files scanned:** 3 Excel supplementary tables (MOESM1/2/3), 605 / 171 / 2777 rows

## Signal (not a verdict — §0)

Raw forensic counts: **26 high, 2 medium** — but on adjudication these are **false
positives**, not integrity concerns:

- All 26 `within_col_value_duplication` and both `missing_last_digits` findings
  are on **coded categorical columns**, not measurements. The top hit —
  `col[2] has value 0.0 repeated 327/604 times` — is the **"Oxygen tolerance"**
  column of MOESM1 (headers: Name, Categorie, Oxygen tolerance, Risk Group,
  Pathogenecity, PMID). A category code (0 = one class) repeated across hundreds
  of organisms is expected *by construction*, and a coded column has no decimal
  spread, which trivially explains `missing_last_digits`.
- paperconan's own detector guidance flags `within_col_*` and "categorical/index
  labels / repeated fill values" as false-positive-heavy — do not strongly report
  them. That matches exactly what these tables are: **reference/lookup tables**
  (organism → phylum → risk group), not experimental measurement grids.

## Takeaway

**Near-clean after adjudication.** paperconan's numeric detectors found nothing
that indicates data-integrity problems here; the high raw count is entirely an
artifact of scanning categorical lookup tables. This says nothing about the
paper's other signals (co-author history, etc.), which the Phase-4 sensors carry
separately. Treat as one clean-ish input to human review, not a clearance.

## Note for reviewers

This is a good calibration case: a **high raw-severity count that is benign on
inspection**. The folded review-card badge shows the raw count (`26 high`); the
real signal is this adjudicated conclusion.

## Where the detail lives

- Machine-readable (every finding + per-sheet stats): `data/audit/scan.json`
- Styled evidence browser: `data/audit/report.html`
