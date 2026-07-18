# paperconan report templates

Use this reference when the user asks for a report, PubPeer draft,
research-integrity note, or batch verdict. Normal scan summaries should stay
short. Formal reports are for findings that survive source-table and context
checks.

Every report must preserve the `signal-not-verdict` boundary: describe the
data pattern, not author intent.

## Short Single-Paper Summary

Use this for normal interactive audits.

```text
I scanned <input>. <N> files were read; <M> files failed to parse.
These are numerical signals, not author-intent conclusions.

Highest-priority finding:
- Location: <file> :: <sheet>, rows <range>, columns <labels>
- Detector: <kind>, rule=<rule>, n=<n>
- Evidence: <small value sample>
- Why it matters: <independence premise + numerical pattern>
- Plausible benign explanations: <shared control / re-plot / unit conversion / formula / fixed denominator / boundary value / technical replicate>
- What would resolve it: <specific author data, legend, Methods, or raw-value mapping>

See <audit/report.html> for highlighted table context.
```

If no finding survives context review, say that paperconan found only likely
benign or context-dependent signals. Do not say the paper is clean.

## Formal Eight-Section Report

Use this for Tier 1 or Tier 2 `KEEP`, PubPeer-style drafting, or formal
research-integrity notes. Keep the language neutral and question-based.

### 1. 论文主结论

State what paper claim the affected data support. Use one or two sentences.
Do not evaluate author intent.

### 2. 异常位置

List file, sheet, figure/panel if known, row/column range, detector, `rule`,
`n`, and representative values. For within-column findings, include repeat
counts, distinct-value count, and repeated value or repeated decimal tail.

### 3. 标签含义

Explain what the labels appear to mean: groups, conditions, units, samples,
timepoints, analytes, statistical outputs, or normalization status. If the
labels cannot be interpreted without the paper, say so.

### 4. 为什么这是问题

State the independence premise. Example:

```text
如果这些列代表不同处理组的独立原始测量，那么逐行完全相同或严格固定变换不容易由普通实验波动产生。这里的重点不是判断作者意图，而是需要说明这些数值如何从原始测量得到。
```

### 5. 影响判断

Set `impact_scope` to `core`, `supporting`, or `peripheral`. Explain how the
affected data relate to the paper's main conclusion. Do not inflate a
supplementary side table into a core conclusion.

### 6. 无辜解释的层次

Use three-part reasoning for each plausible benign explanation:

```text
- 解释: <shared control / re-plot / unit conversion / formula / normalization / fixed denominator / boundary value / technical replicate / model output>
  支持它的证据: <what points toward this explanation>
  反驳它的证据: <what makes it insufficient>
  仍缺什么: <specific missing source, legend, Methods, or author clarification>
  当前判断: <fits / partly fits / does not fit / unresolved>
```

### 7. 需要作者澄清

Ask answerable questions:

- Are these rows/columns independent samples or repeated displays of the same
  measurements?
- Are the values raw measurements or formula-derived outputs?
- Is there a disclosed shared control, common baseline, unit conversion, or
  normalization step?
- Can the authors provide the raw values or corrected source-data mapping for
  the affected figure?

### 8. 证据

List reproducibility details:

- paperconan version and profile.
- Input source-data file path or public supplementary-data source.
- `scan.json` and `report.html` path if available.
- Finding kind, rule, `n`, row/column range, and small value sample.
- Whether original table, figure legend, Methods, and main text were opened.

Close with:

```text
以上是可复核的数据模式问题，不构成对作者意图或学术不端的判断。
```

## Adaptive Numeric And Image Report

Use one paper-level verdict and one `paperconan report` invocation for mixed
numeric and image review. PaperConan registers assets and may provide
deterministic hints; an external multimodal Agent performs semantic review and
coverage accounting. PaperConan does not configure model APIs, keys, or
provider SDKs.

The scan-side `image_assets[]` record used by the Agent is:

```json
{
  "asset_id": "img:0123456789abcdef0123",
  "file": "Fig3.png",
  "source_files": ["Fig3.png"],
  "path": "images/native/img-0123456789abcdef0123.png",
  "preview_path": "images/preview/img-0123456789abcdef0123.jpg",
  "preview_mime": "image/jpeg",
  "source_type": "local_image",
  "source_url": null,
  "parent_file": null,
  "page": null,
  "render_dpi": null,
  "figure_label": null,
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "width": 2480,
  "height": 1760,
  "exif_orientation": 1,
  "mime": "image/png"
}
```

An optional deterministic `image_findings[]` hint has this complete shape:

```json
{
  "finding_id": "image:pair:stable-id",
  "kind": "image_pair_similarity_signal",
  "severity": "medium",
  "rule": "two registered image regions retain high structural similarity under flip",
  "asset_ids": ["img:a"],
  "regions": [
    {"asset_id": "img:a", "box": [120, 80, 740, 610]},
    {"asset_id": "img:a", "box": [820, 80, 1440, 610]}
  ],
  "method": "panel_pair_similarity",
  "score": 0.94,
  "transform": "flip",
  "evidence": {
    "crop_a_path": "images/evidence/image-pair-stable-id-a.png",
    "crop_b_path": "images/evidence/image-pair-stable-id-b.png",
    "preview_path": "images/evidence/image-pair-stable-id-preview.jpg"
  },
  "profile_action": "kept"
}
```

The current deterministic helper compares two native-coordinate regions within
one registered asset. It does not emit cross-asset comparisons.

`image_findings` are optional hints, not the complete review set. Review every
registered asset even when this list is empty. Start with the whole image, then
use a native-pixel crop for small panels or unresolved detail.

Put the Agent judgment in the same `verdict.json findings[]` as numeric
judgments:

```json
{
  "title": "Synthetic mixed review",
  "verdict": "NEEDS_HUMAN",
  "paper_conclusion": "The numeric and image material require contextual review.",
  "findings": [
    {
      "finding_type": "numeric",
      "title": "Numeric relation",
      "finding_ref": {"kind": "constant_offset"},
      "review_status": "needs_human",
      "impact_scope": "supporting",
      "report_md": "The numeric relation requires source context."
    },
    {
      "finding_type": "image",
      "title": "Fig. 3 panel pair requires clarification",
      "finding_ref": {"finding_id": "image:pair:stable-id"},
      "image_refs": [
        {"asset_id": "img:a", "box": [120, 80, 740, 610], "label": "A"},
        {"asset_id": "img:a", "box": [820, 80, 1440, 610], "label": "B"}
      ],
      "review_status": "needs_human",
      "impact_scope": "supporting",
      "report_md": "The registered regions retain high similarity and require source context."
    }
  ],
  "image_review": {
    "status": "completed",
    "reviewed_asset_ids": [],
    "unresolved_asset_ids": ["img:a"],
    "unreadable_asset_ids": [],
    "deferred_asset_ids": [],
    "note": "coverage accounting completed by a multimodal Agent"
  }
}
```

`finding_ref` is optional for Agent-only image observations; use `image_refs`
alone when no deterministic hint exists. Image `review_status` accepts
`needs_human`, `explained`, `different`, or `unresolved`, and unknown values
become `unresolved`.

An external Agent may create a cross-asset observation that is not
deterministic `image_findings` output. Label it as Agent-created and omit
`finding_ref` when no deterministic finding matches:

```json
{
  "finding_type": "image",
  "title": "Agent-created cross-asset observation",
  "image_refs": [
    {"asset_id": "img:a", "box": [120, 80, 740, 610], "label": "Fig. 3A"},
    {"asset_id": "img:b", "box": [40, 55, 660, 585], "label": "Fig. 4B"}
  ],
  "review_status": "needs_human",
  "impact_scope": "supporting",
  "report_md": "The Agent-registered comparison requires source context."
}
```

Every asset must appear in exactly one `image_review` coverage list:
`reviewed_asset_ids`, `unresolved_asset_ids`, `unreadable_asset_ids`, or
`deferred_asset_ids`. `image_review.status: "completed"` means coverage
accounting completed, not that every image question was explained. Use
`partial` when review is deferred, `unavailable_no_multimodal` when the Agent
cannot open local images, and `not_requested` only when image review was not
requested. Unknown `image_review.status` values normalize to `partial`, while
unknown image finding `review_status` values normalize to `unresolved`.

Render the single unified report with:

```bash
paperconan report audit/scan.json --verdict verdict.json --out adjudication.html
```

## Batch Verdict Record

For batch work, one paper can be summarized as JSON. This schema is advisory;
it does not require a database or remote service.

```json
{
  "verdict": "KEEP",
  "suspicion_tier": 1,
  "impact_scope": "core",
  "tier_why": "strict transform across columns presented as independent raw measurements",
  "drop_reason": null,
  "innocent_explanation": "unit conversion checked and does not fit the labels",
  "needs_author_data": "raw source data and figure-panel mapping",
  "report_md": "### 1. 论文主结论\n...",
  "review_status": "unreviewed"
}
```

Use `null` for fields that do not apply. Do not include author names or
speculation about intent.

**The primary shape for a rendered adjudicated report is the paper-level object
with a `findings` array** (each entry has its own tier/status and `finding_ref`);
see [adjudication-tiers.md](adjudication-tiers.md) › "Multiple Findings In One
Paper". A single finding is just a one-element `findings` list — `paperconan
report` renders it in the same high-fidelity layout (paper header + per-finding
card + evidence heatmap), so single vs multiple is only a matter of how many
findings you list, not of presentation. The flat single-verdict schema above
stays valid and now renders in that same rich layout too.

## DROP Note

DROP records should be short:

```json
{
  "verdict": "DROP",
  "suspicion_tier": null,
  "impact_scope": null,
  "tier_why": "",
  "drop_reason": "fixed_denominator",
  "innocent_explanation": "values are percentages generated from a common small denominator",
  "needs_author_data": null,
  "report_md": null,
  "review_status": "unreviewed"
}
```

## NEEDS_HUMAN Note

NEEDS_HUMAN records should say exactly what is missing:

```json
{
  "verdict": "NEEDS_HUMAN",
  "suspicion_tier": null,
  "impact_scope": null,
  "tier_why": "source table does not identify whether rows are independent samples or technical repeats",
  "drop_reason": null,
  "innocent_explanation": "technical-repeat export remains plausible",
  "needs_author_data": "row-level sample provenance and raw instrument export",
  "report_md": null,
  "review_status": "unreviewed"
}
```
