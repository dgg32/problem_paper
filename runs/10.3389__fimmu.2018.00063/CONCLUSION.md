# paperconan conclusion — 10.3389/fimmu.2018.00063

*Leishmania donovani ISP2 Mediated Inhibition of Lectin Pathway and Upregulation
of C5aR Signaling Promote Parasite Survival inside Host* (Frontiers in
Immunology, 2018).

- **Run:** paperconan 0.8.2, `forensic` profile, 2026-07-19
- **Source of files:** Europe PMC (PMC5796892)
- **Files scanned:** 4 (`article_table_1.csv`, `article_table_2.csv`,
  `data_sheet_1.pdf`, `fimmu-09-00063.pdf`)

## Signal (not a verdict — §0)

**1 medium-severity finding, 0 high-severity.** No last-digit χ² anomalies and no
over-represented two-decimal endings across any sheet.

- `[identical_after_rounding]` — `article_table_2.csv`, rows 4–8 (n=6): six cells
  share the rounded value `0.3` but have **5 distinct precise underlying values**.
  This is a rounding-collision flag, not evidence of fabrication: reporting
  several nearby measurements at one-decimal precision naturally produces
  repeated rounded values. It's worth a reviewer glancing at that table block,
  nothing more on its own.

## Takeaway

This paper is **near-clean by paperconan's numeric-forensics detectors** — the
single medium flag is a low-concern rounding artifact. paperconan inspects the
numbers *inside the data tables*; it says nothing about this paper's other
signals (co-author misconduct proximity, cited-retracted-work count, etc.),
which are carried separately by the Phase-4 sensors and the Tier-A score. Treat
this as one clean-ish input to the human review, not a clearance.

## Where the detail lives

- Human-readable findings: [`data/audit/REPORT.md`](data/audit/REPORT.md)
- Styled report: `data/audit/report.html`
- Machine-readable (every finding + per-sheet stats): `data/audit/scan.json`
