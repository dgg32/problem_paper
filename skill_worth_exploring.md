# Skills / tools worth exploring — assessment (2026-07-19)

Evidence-based triage of external integrity tools against **this** corpus
(biology/microbiology, 835 cached full-text docs, 796 active candidates). Ranked
by what actually fits, not by star counts. Governing rule (plan.md §0): any new
signal stays **labelled, not scored, human-adjudicated** unless it's a hard fact.

---

## The surprise finding: statcheck is dead on this corpus

`statcheck` was built for **psychology** papers (APA inline format:
`t(24)=2.1, p=.04`). Microbiology papers don't report stats that way — they use
`p<0.05`, tables, and asterisked bar charts. Probe over 200 of the 835 cached
docs:

- **~1%** have a statcheck-recomputable stat+p-value pair.
- It would fire on roughly **2 of 200 papers**.

High precision × ~nil recall = **not worth the R-integration cost**. `scrutiny`
(also R, also text-reported stats) likely hits the same wall, and overlaps
paperconan on tables where it doesn't. (Reproduce: grep `full_text_cache/` for
`t(df)=`/`F(df,df)=`/`χ2(df)=` co-occurring with `p=`.)

---

## Ranking

| Tool | Verdict | Why |
|---|---|---|
| **1anj generic + figure router** (Gap 1) | ✅ **Already adopted** | Vendored `image_similarity_screen` (0 FP on 30 real figs) + built `figure_classifier.py` router that safely gates the wet-lab screens |
| **Partial-panel image reuse** (Sherloq / Sandyyy123 algorithms) | ✅ **Highest-value next** | The *real* remaining hole — see below |
| **Seek & Blastn** (Gap 3) | ⏸️ **Deferred to genomics phase** | NOT microbiology-specific (corrected) — it's a *human gene-function / gene-knockdown* tool. Genuinely powerful, but for a different subfield. Revisit when the project expands past microbiology |
| S&B flagged-papers list (scigendetection URL) | ❌ **Corpus mismatch — skipped** | The named URL is the S&B *submission tool*, not a dataset. Real lists exist (PLOS ONE 2019 `10.1371/journal.pone.0213266`, LSA 2022 `10.26508/lsa.202101203`, keyed by PMID), fetchable via our Europe PMC path. But cross-check gave **1 of 246 flagged PMIDs matched, and it's already retracted** — ~0 overlap with this microbiology corpus (statcheck-style). Machinery deferred, not built |
| **Anti-Autoresearch** (Gap 4) | ⚠️ **Mine, don't run** | Adopt its 46-pattern taxonomy + observability tiers + red-team refutation to harden the adjudication layer we already built (draft→adjudicated verdict, not-scored quarantine). ML/CS-paper focused, so don't run wholesale |
| statcheck / scrutiny (Gap 2) | ❌ **Skip** | ~1% yield on this corpus (proven above) |
| metacheck / gunting / sherloq-as-tool | ❌ Skip | Reference-arch only / 0★+404 README / GUI-oriented |

---

## The one to push: close the partial-panel image gap

Counterintuitive but the most important item on the list. We already handle
**whole-image** reuse (aHash, clean, 0 FP). But **real image-duplication fraud —
including the HDAC6 case that motivated this project — hides in
partial/cropped/spliced sub-panels**, which aHash explicitly *cannot* see. That's
the genuine biggest hole, and it sits *inside* the gap we've mostly filled.

Fix = **copy-move / clone detection + ELA** (Sherloq and Sandyyy123 both do
this). Honest catch: those need **OpenCV/numpy**, which breaks the stdlib+Pillow
discipline that made 1anj trivially vendorable. That's the real decision — is
closing partial-reuse worth a heavier dependency? For a biomedical integrity
tool, likely **yes**, because that's where the fraud actually lives.

---

## Scope decision (2026-07-19): microbiology first, expand later

The user's strategy is to **publish the microbiology project first, then expand to
other subfields** (genomics/gene-function is of interest, but later). This reorders
everything: prioritise signals that hit the *current microbiology corpus*, and
defer anything whose corpus is a different subfield — even if the tool is excellent.

Consequences already applied:
- **Seek & Blastn / S&B flagged-papers list → deferred** to the genomics phase
  (proven ~0 overlap with the microbiology corpus).
- **statcheck / scrutiny → skip** (psychology APA-stat format, ~1% yield here).
- **Keep pushing** the tools that *do* fit this corpus: the partial-panel image
  gap (below), and refreshing the corpus-matching PPS **tortured-phrase** dictionary.

## Recommended order (microbiology-first)

1. **Prototype copy-move / ELA** on the figures we already fetch (evaluate
   Sandyyy123's figure-purpose-built algorithms first; mine Sherloq if not) —
   the real image gap, corpus-agnostic so it fits now. Weigh the OpenCV/numpy cost.
2. **Refresh the PPS tortured-phrase dictionary** — corpus-matching, cheap, extends
   a sensor already in use.
3. **(Deferred, genomics phase)** Seek & Blastn build: extract primer/siRNA
   sequences from full text → NCBI blastn → compare vs. claimed target gene.
4. **Skip statcheck / scrutiny entirely.**

---

## Source list (from the user's web-search report)

- **Gap 1 — image forensics:** `1anj/academic-integrity-skill` (56★ MIT — *adopted*),
  `Sandyyy123/image-integrity-screen` (0★, copy-move+ELA, figure-purpose-built),
  `GuidoBartoli/sherloq` (3.2k★, mature ELA/clone-detection, GUI — mine algorithms).
- **Gap 2 — reported-stat consistency:** `MicheleNuijten/statcheck` (191★ R — *skip, 1% yield*),
  `lhdjung/scrutiny` (8★ R, GRIM/GRIMMER on text means/SDs), `scienceverse/metacheck` (45★ R, ref arch).
- **Gap 3 — nucleotide-sequence-sanity:** Seek & Blastn (Labbé/Byrne, PLOS ONE 2019; no maintained
  repo — build it) + flagged-papers DB at scigendetection.imag.fr/TPD52.
- **Gap 4 — agent-side adjudication:** `wanshuiyin/Anti-Autoresearch` (101★ MIT — *mine the architecture*),
  `jpliem/gunting` (0★, README 404 — watch, don't adopt).
