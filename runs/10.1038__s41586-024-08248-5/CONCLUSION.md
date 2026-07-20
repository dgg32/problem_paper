# paperconan conclusion — 10.1038/s41586-024-08248-5

*Human HDAC6 senses valine abundancy to regulate DNA damage* (Nature, 2024) —
the paper carrying a live Nature Editor's Note ("concerns have been raised
regarding the reliability of data presented in this article") and reported
contract terminations for the corresponding author (Ping Wang) and first
author (Jiali Jin) at Tongji University.

- **Run:** paperconan 0.8.2, `forensic` profile, 2026-07-20 (re-adjudicated
  2026-07-21 after a first pass called this clean — see "Adjudication history"
  below; that call did not hold up under harder scrutiny)
- **Source of files:** Nature's own Source Data links (per-figure XLSX), not
  PMC or a generic repository — `run_paperconan.py --auto` found nothing (PMC
  has no supplementary files for this DOI, and repository search found no
  confident title match), so the files were fetched by hand from the article
  page's "Source data" section, which paperconan's fetch path doesn't reach.
- **Files scanned:** 14 XLSX (12 per-figure/per-extended-data-figure Source
  Data files + Supplementary Tables + one duplicate)

## Signal (not a verdict — §0)

Raw forensic counts: **482 blocks with findings, 64 high-severity**. Six
structurally distinct high-severity *kinds* drive that count. Four hold up as
benign on inspection; **two do not, and are unresolved, not cleared**:

### Two findings that remain open

- **`identical_column` (ExtDataFig10, "0.41% Val" tumor group, n=7)**:
  width(mm) and length(mm) are byte-identical for all 7 tumors in this one
  dietary group. The immediately neighboring group in the same sheet (n=7,
  larger tumors) has only 1 coincidental width=length match — a ~14%
  per-sample baseline rate. At that baseline, getting 7/7 exact matches by
  chance is **~1.2×10⁻⁶**. A first pass at this finding checked that the
  volume column (`V = 0.5 × length × width²`) reproduces correctly from the
  reported width/length and treated that as evidence of authenticity — that
  reasoning doesn't hold: the formula check only shows the *volume* column is
  internally consistent with whatever width/length values are in the sheet;
  it cannot tell whether width was independently measured or copied from
  length. "Small round tumors measured at 0.1mm resolution" remains a
  plausible innocent explanation, but it is not confirmed, and the 7/7-vs-1/7
  contrast with the neighboring group is a real, quantified anomaly.
  **Needs human/author context — original calipers/notebook, not resolvable
  from the spreadsheet alone.**
- **`sum_constant` + `exact_linear` (ExtDataFig7, "ED Fig.7o", sNC condition)**:
  three columns (C, D, E) are grouped under one merged "sNC" header, i.e.
  presented as three replicate measurements. But `C + D = 2.00000000` exactly
  (8 decimal places) in every one of 5 rows, while the third replicate, E,
  does **not** participate in that relationship (`C+E` and `D+E` both wobble
  around 2, never landing on it exactly). Two of three nominally-independent
  replicates being exact algebraic mirrors of each other, while the third
  isn't, is not a pattern real biological replicate noise produces — it is
  the signature of one column being computed from the other (`D = 2 − C`).
  This pattern does not recur anywhere else across all 14 scanned files, so
  it isn't an established lab-wide normalization convention either. A
  legitimate explanation is still possible (e.g. C and D encode complementary
  normalized fractions rather than true replicates, mislabeled as replicates)
  but nothing in the visible table structure confirms that, and the isolation
  + exactness both cut against it. **Needs human/author context.**

### Four findings that do hold up as benign

- **`constant_offset` (ExtDataFig8, 6 instances)**: `col[1]` is the literal
  sequence 1,2,3...8 — a sample-index column, not measured data. The "+16"
  offset is a second panel's samples renumbered 17-24 in a combined scheme.
- **`many_equal_pairs` (several sheets)**: sample values show heavy `0.0`
  clustering (detection-floor / censoring values common in MS and RNA-seq
  data); checked that the *non-zero* values differ between the "equal"
  columns in every instance — equality is a zero-floor artifact, not real
  column duplication.
- **`cross_sheet_*` (14 findings)**: every one is a figure's Source Data
  restating numbers that also appear in a Supplementary Table (e.g. TET2
  ChIP-seq binding-site coordinates, or DE-gene log2FC/Padj) — expected,
  intentional overlap between a table and the figure built from it.
- **`within_col_value_duplication` (bulk of the 64 "high" count)**: small-n
  (n=6–8) repeated round values — paperconan's own judgment rubric calls this
  pattern false-positive-heavy by default. Not individually re-verified
  instance-by-instance beyond that category-level rubric check (unlike the
  two findings above, which were opened and checked directly) — flagging this
  explicitly rather than implying exhaustive per-instance verification.
- **`digit_distribution` (17/25 sheets FDR-significant)**: expected for large
  genomic/RNA-seq datasets (coordinates, DE stats) — last-digit non-uniformity
  in this kind of data is normal on its own.

## Takeaway

**Not clean. Two independent, quantified anomalies remain unresolved** —
one a statistically improbable measurement duplication (tumor width/length),
one a mathematically exact but selectively-applied relationship between two
of three nominal replicates (valine dose-response). Neither is proof of
fabrication on its own — both have a conceivable innocent explanation — but
neither should have been adjudicated as a false positive, and an earlier pass
at this file did exactly that. Correcting the record: this is a
**NEEDS_HUMAN** finding, not a clean numeric scan.

**Scope caveat still stands**: the Editor's Note and institutional action
don't specify whether the concern is about numeric data or figures, and this
run did not include `--images`. Separately, this paper has 21 PubPeer
comments (checked 2026-07-20), the first of which flags "unexpected
overlapping areas" in a figure with an annotated image attached — a
different modality from the two numeric findings above, not the same
concern, but corroborating that this paper is under substantive, specific
scrutiny beyond the Editor's Note's vague wording.

## Adjudication history

- 2026-07-20 (first pass): called all 64 high-severity findings benign,
  including these same two. That call was wrong — it treated the volume-
  formula consistency check as ruling out duplication (it doesn't), and
  didn't check whether the third "sNC" replicate (E) shared the C+D=2
  relationship (it doesn't). Revised 2026-07-21 after direct challenge.

## Where the detail lives

- Machine-readable (every finding + per-sheet stats): `audit/scan.json`
- Styled report (raw detector output, pre-adjudication): `audit/report.html`
