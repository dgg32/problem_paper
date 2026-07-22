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

---

## Metadata-only sensors (2026-07-21)

*PDF‑free / full‑text‑free signals that run on graph + OpenAlex/Crossref/ORCID
metadata only. Evaluated against the same corpus constraint (microbiology‑first,
plan.md §0 scoring discipline).*

| Sensor | Verdict | Why |
|---|---|---|
| **Institutional retraction rate** (graph‑only) | ⚡ **Build now** | Pure‑Cypher, same pattern as `journal_retr_rate` (§2.2e) but aggregated per institution via OpenAlex ROR/affiliation. Catches the Hindawi/mill‑hospital‑affiliation pattern as a hard, sourced fact. Slots into existing Tier‑A weighted score. |
| **Corrections/errata‑history** (Crossref) | ⚡ **Build now** | Crossref `relation`/`update-to` over `type:correction` — metadata‑only, no full text. Multiple corrections on one paper (or an author with correction‑dense corpus) is a hard fact. Near‑zero FP as *signal*; keep low‑weight or not‑scored since corrections are often honest, but valuable as explainable context. |
| **Author output‑burst / hyperprolific** (OpenAlex) | ✅ **Graph feature, unbuilt** | Reserved in plan.md §2.2 but never implemented. OpenAlex per‑author works/year — sudden >20 papers/year or a burst in a new field is the classic mill/authorship‑for‑sale signature. Pure metadata, cheap, fits Phase 2 graph features. |
| **Author topic‑drift** (OpenAlex) | ✅ **Build when hyperprolific is done** | OpenAlex per‑author topics vs. candidate paper's topic. A nephrologist co‑authoring oncology knockdown papers is a mill‑fingerprint. Follows same extraction path as hyperprolific; share the author baseline fetch. **Soft signal — not scored** per §0. |
| **Citing‑side reputation** (graph‑only) | ✅ **Graph feature, unbuilt** | Inverse of retracted‑citation checker: is this paper *cited predominantly by* papers that were later retracted (or by one mill cluster)? Entirely graph‑internal. **Not scored** (guilt‑by‑citation‑neighbourhood is associative, not a hard fact) — label on review card alongside GDS prior and PubPeer counts. |
| **Duplicate title/abstract** (OpenAlex) | ⏸️ **Defer — needs MinHash/embedding** | Corpus‑wide near‑duplicate detection via OpenAlex `abstract_inverted_index` (metadata, not full text). Catches salami‑sliced papers and mill template‑abstract batches. Higher engineering effort; revisit when duplicate‑title lookups in literature show mill clustering. |
| **Mill title‑template matcher** (metadata) | ⏸️ **Defer — combine with duplicate‑detect** | Regex/heuristic templates on title alone ("X alleviates Y via miR‑Z axis in…"). Weak alone — legit papers use these — but sharpens as a filter for the duplicate‑abstract clusterer. Hold until that's built. |
| **Special‑issue / guest‑editor flag** (metadata) | ⏸️ **Defer — needs issue data ingestion** | Journal+issue metadata: papers in issues with known mass retractions (Hindawi 2023, MDPI guest‑editor scandals). Computable once issue info is in the graph. Moderate effort to backfill; high signal when it fires. |
| **ORCID provenance** (ORCID API + OpenAlex) | ⏸️ **Defer — soft, low base rate** | ORCID created shortly before publication, single‑work ORCIDs, no employment history on senior author. Cheap but low base rate on this corpus (many authors lack ORCIDs at all). **Not scored** — store as review‑page context if the data's there. |
| **Crossref metadata anomalies** (Crossref) | ❌ **Skip** | Missing DOIs in references, extreme ref counts, missing funder/license on OA‑claimed papers — each is cheap to check but every one has a legitimate explanation. Too noisy per §0's "hard, sourced facts" rule. |

### Scoring‑discipline mapping (per AGENTS.md / plan.md §0)

- **Weighted in Tier A:** institutional retraction rate only (same pattern as `journal_retr_rate`). Corrections‑history could be weighted if empirical FP rate proves low — for now keep as labelled context.
- **Labelled but NOT scored (review‑card context):** author output‑burst, topic‑drift, citing‑side reputation, ORCID provenance. Same bucket as PubPeer counts and GDS prior.
- **Deferred / skipped entirely:** everything else in the table above — either too noisy, too high‑effort, or zero corpus overlap.

### Recommended order (microbiology‑first, metadata‑only)

1. **Institutional retraction rate** (1–2 hrs, pure Cypher, reuses `journal_retr_rate` pattern exactly, hard fact → Tier A). Referenced in plan.md §2.2(g) as "shared‑institution" — this is that reserved slot, now actionable.
2. **Corrections/errata‑history** (1–2 hrs, Crossref REST API batch, stored as `Paper.update_count`, `Paper.has_correction`). Low weight initially but high explainability.
3. **Author output‑burst + topic‑drift** (3–4 hrs, shares OpenAlex per‑author fetch with hyperprolific — do as a pair). Both stay not‑scored.
4. **Citing‑side reputation** (1–2 hrs, graph‑internal, pure Cypher). Not‑scored context alongside GDS prior.

---

## Retraction Watch as a data source (2026-07-22)

*What the material on retractionwatch.com offers that can be turned into a sensor,
a graph property, or a curated list this project can ingest — broken out by
machine‑readable vs. reporting‑derived signal.*

### Curated lists (machine‑readable, ready to ingest)

Three maintained lists are directly consumable. None overlap with anything the
graph currently imports, and all three carry DOIs, journal titles, or dates that
make them matchable to existing `Paper`/`Journal` nodes.

| Source | Format | Size | Hard fact? | Mapping |
|---|---|---|---|---|
| **RW Database daily CSV** (GitLab) | `retraction_watch.csv`, commits daily | ~65k retractions | Yes — sourced retractions with reasons, dates, authors, journal | ✅ **Built 2026-07-22** as `graph_processing/pull_retraction_watch_csv.py` — but MANUAL, not daily/cron'd: the user wants to freeze the current snapshot to publish an article against a known, reproducible dataset, and only refresh when they explicitly choose to. Pulls from `gitlab.com/crossref/retraction-watch-data/-/raw/main/retraction_watch.csv`, with core-column + row-count-drop sanity checks and a timestamped archive of the file it replaces. Does not itself re-run any downstream sensor. |
| **Hijacked Journal Checker** | Google Sheet, 450+ entries | 450+ journals | Yes — each entry is a confirmed hijacked/cloned journal with evidence | ✅ **Built 2026-07-22** as `graph_processing/hijacked_journal_checker.py` — but kept **unscored** review context, not fed into `journal_integrity_flag_count` as originally suggested here: many rows share an identical title/ISSN between clone and real journal, and this graph can't tell which "instance" a given paper came from, so scoring it risked penalizing papers legitimately published in the real journal. 5/636 graph journals matched, 27/795 candidates covered. |
| **Cabanac ChatGPT‑writing list** | HTML page, PubPeer‑verified | ~90 DOIs | Yes — each entry has a PubPeer comment confirming AI‑generated text fingerprint | ✅ **Built 2026-07-22** as `graph_processing/cabanac_chatgpt_checker.py` / `data/cabanac_chatgpt.csv` (109 DOIs, pulled from the PPS "Suspect" detector page filtered to its ChatGPT category — an Oracle APEX interactive report with no plain export, captured via browser automation, then frozen). 107/109 rows have a PubPeer link (2 don't yet). Matched by DOI, no name-matching at all. Kept **unscored**: it's the same underlying phenomenon `ai_text_tell_flag_count` already scores from full text, so it's used to calibrate that sensor rather than double-weight the same evidence category from two sources. 0/795 candidates matched — expected, this list skews CS/engineering/social-science, essentially no overlap with the microbiology corpus. |
| **Mass Resignations List** | HTML page, timeline | 55 events | Yes (human‑reported) — but the link to individual‑paper risk is indirect | `Journal.editorial_resignation_event` + date. Label on review card as hard context; **not scored** pending empirical proof of a retraction‑rate correlation on this corpus. |

**Recommended ingest order:** the CSV refresh hits the broadest set (every existing
RW‑sourced node stays fresh); then the Hijacked list (maps to an existing sensor,
fills a gap the DOAJ check can't catch); then Cabanac (small, calibrates
`ai-text-tell-detector`). Mass Resignations is the lowest priority of the four —
it's context, not a direct paper‑level signal.

### Reporting‑derived patterns (may become new sensors)

Two recent articles (2026‑07‑20/21) carry metadata‑detectable patterns that
current sensors don't cover.

#### A. "Open‑database fast‑churn" title template

The Nepal research‑factory article (2026‑07‑21) quotes a preprint finding
**"formulaic titles and identical methods"** on CDC WONDER / GBD / SEER / NHANES
database‑study papers — the mill‑production signature. The template is:

> *"X among Y using the [CDC WONDER / NHANES / SEER / GBD] database: a
> [retrospective analysis / systematic review and meta-analysis]"*

This is **metadata‑only**: the title is on OpenAlex/Crossref, no full text needed.

- **Signal:** moderate. Legitimate epidemiologists use these databases too, so a
  title‑alone match has an FP floor.
- **Fit for the current microbiology corpus?** Partial. The mill‑produced papers
  are dominated by clinical/epidemiology topics (cardiology, oncology, neurology),
  not microbiology. But the *microbiology corpus may include papers using NCBI
  SRA / GenBank nucleotide databases*, which are the microbiology equivalent of
  CDC WONDER — same template, different source database.
- **Recommendation:** build a regex title‑matcher scoped to both the clinical‑DB
  set (CDC WONDER/GBD/SEER/NHANES) and the bio‑DB set (SRA/GenBank/GEO/ENA/DDBJ).
  **Label as soft signal; do not score.** Worth at most 2 hours to build — it's a
  single regex + a title‑match Cypher pass.

#### B. Conference‑abstract → paper pipeline

Same article: the mill produces conference abstracts that route into journal
supplements — acceptance at AHA/ESMO/ASCO → supplement publication → possibly
feeds into a full‑paper refereed route. Crossref/Pubmed sometimes link these.
This is a **brand‑new signal category**: a short‑latency abstract‑to‑paper
pipeline across the same author and topic.

- **Metadata‑check:** do OpenAlex/Crossref link a conference‑proceedings DOI to
  a full‑paper DOI for the same author? Unknown without sampling.
- **Recommendation:** defer; sample first. If there's evidence that
  conference‑abstract data is available in the current API surface, a
  "matched proceeding‑body within 18 months" check is a cheap metadata filter.

#### C. Known‑miller co‑author list (manual, but hard fact)

The Nepal article names a **known paper miller** (Shreya Singh Beniwal) with a
confirmed DOI trail. The existing `coauthor_other_misconduct` Tier‑A feature only
fires for *retracted* co‑authors; this would fire for *known‑miller* co‑authors
whose papers haven't (yet) been retracted.

- **Format:** `data/known_millers.csv` (author name + ORCID + source article URL).
- **Recommendation:** low base rate on microbiology corpus, but cheap to maintain
  and high precision. **Keep not‑scored** — guilt‑by‑association, per §0 — but
  label the co‑authorship on the review card with`⚠️ known miller co‑author` and
  a link to the RW article.
- ✅ **Built 2026‑07‑22** as `graph_processing/known_miller_coauthor_checker.py`.
  Seeded with the Nepal research‑factory case (Mahato, confirmed ORCID; Beniwal,
  no ORCID found — a live search surfaced a different same‑named person with her
  own distinct ORCID, confirming the name‑collision risk, so she's documented but
  NOT cross‑matched). **0 hits on the current corpus**, as expected (different
  subfield) — reusable infrastructure for corpus expansion / future entries.

### One meta‑signal from RW reporting

Both the Nepal mill and the peer‑review‑bribery piece share a structural pattern:
modern paper mills increasingly target **conference‑abstract pipelines + peer‑
review manipulation**, not just fabricated data. Together, the Hijacked Journal
Checker and the mass‑resignation list make the case that **compromised peer review
should be Tier‑A scored** on this corpus — it's the #2 retraction reason by RW
count (~11k), and both lists are clean, hard sources to ground it.
