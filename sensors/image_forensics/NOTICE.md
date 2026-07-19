# Vendored: image-reuse screen (sensor #8)

Source: **[1anj/academic-integrity-skill](https://github.com/1anj/academic-integrity-skill)**
(MIT, © 2026 lanj — the 耿同学/geng whistleblower behind the Tongji HDAC6 case).
See `LICENSE`. Vendored 2026-07-19.

## What we vendored, and how the dangerous parts are gated

- `image_similarity_screen.py` — generic whole-image reuse screen. **Runs by
  default** on every figure; low FP (see evaluation below).
- `blot_gel_lane_audit.py`, `microscopy_reuse_screen.py`,
  `flow_plot_duplicate_screen.py` — the wet-lab screens. **Opt-in only
  (`--wetlab`) and NEVER run un-gated**, because they assume the input image IS
  that assay type. Verified live: `blot_gel_lane_audit` on ordinary composite
  figures returned **BLACK / 44 findings** — pure false positives from
  "detecting lanes" in any vertical intensity change.
- `figure_classifier.py` — our own conservative **gate** (added 2026-07-19) that
  makes the wet-lab screens usable. It classifies each figure (Pillow-only:
  white/dark/colour fractions) into `chart_or_text` / `color_image` /
  `grayscale_image`, and routes each specialized screen to ONLY its matching
  type (`blot_gel_lane_audit` <- grayscale_image, `microscopy_reuse_screen` <-
  color_image; flow plots aren't reliably pixel-detectable, so never
  auto-routed). Because published figures are often multi-panel COMPOSITES, the
  gate is deliberately conservative: skipping a real blot (false negative) is
  safe — the generic screen still runs on it — but mis-routing a chart into the
  blot audit is the catastrophe it prevents. Validated: the composite that gave
  44 FPs now routes to nothing (0 findings); a synthetic gel routes to the blot
  audit, a dark fluorescence image to microscopy.

The wet-lab screens retain their upstream `getdata()` calls (a Pillow-14/2027
deprecation, harmless now); only the generic screen's `ahash()` was patched.

## Why the generic screen is trustworthy (evaluation, 2026-07-19)

`image_similarity_screen` is average-hash (aHash) with flip/rotate variants +
Hamming distance. Measured:
- **0 false positives** across 30 real distinct figures from 3 papers (all GREEN).
- **Correct detection** in a controlled test: caught an exact duplicate
  (hamming 0) and a mirror-flipped copy (original↔flip_lr), while leaving
  distinct figures untouched.
- **Known limit (self-declared):** aHash is coarse — it screens *whole images*,
  so it MISSES partial/cropped sub-panel reuse and can flag genuinely-similar
  legitimate figures. It is a **screen, not proof** (plan.md §0).

## Discipline

Its result is a **not-scored, signal-not-verdict** input — same lane as
paperconan / PubPeer / GDS. Never folded into the weighted Tier-A score. Surfaced
on the review card as a `🖼` badge + a "Review context (not scored)" line, and
adjudicated by a human. See [[paperconan-run-convention]] / `runs/README.md`.

## Local patch

`ahash()` was changed from `list(gray.getdata())` to `list(gray.tobytes())` —
version-stable, avoids the getdata() deprecation slated for Pillow 14 (2027).
No other changes.
