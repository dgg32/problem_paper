# How to change scoring weights

The Tier-A score every paper gets in the review queue is a weighted sum of
independent signals (citations to retracted work, an ORI finding, an
Expression of Concern, venue-level retraction rates, ...). All of those
weights live in one file, [`config/weights.yaml`](config/weights.yaml) — no
code change needed to adjust them.

## 1. Edit the weight

Open `config/weights.yaml` and change a number, e.g.:

```yaml
ori_finding_flag: 4.0
```

Each entry has a comment explaining what it means and why it's set where it
is — read that before changing it. `weights.yaml` is git-tracked, so
`git log -p config/weights.yaml` is the audit trail for every past change;
consider a commit message explaining *why* you changed a weight, the same
way past changes in that file are documented.

## 2. Rebuild — editing the YAML alone does nothing yet

`review/index.html` is a **static, pre-generated file**. It doesn't read
`weights.yaml` at page-load time, so saving the YAML and reloading the page
in your browser has no effect by itself. You have to regenerate the score
data and the page:

```bash
# 1. Re-score every candidate with the new weight(s)
python graph_processing/tier_a_scoring.py --top 500 -o data/tier_a_triage_full.csv

# 2. Regenerate the static review page from the new scores
python graph_processing/build_review_page.py --top 50
```

The first command re-runs the scoring engine and writes the new ranked CSV.
The second rebuilds `review/index.html` from scratch, baking the new score
and per-signal contributions into every card.

## 3. Reload the page

*Now* refresh the browser tab serving `review/index.html`
(`python -m http.server 8899 --bind 127.0.0.1 --directory review`) — you're
loading the newly-written file, so the new weight is reflected in the
ranking.

## Alternative: the pipeline dashboard

Instead of running the two commands by hand, you can trigger the same two
stages as buttons in the pipeline dashboard:

```bash
uvicorn review.pipeline_app:app --reload --port 8800
# open http://127.0.0.1:8800/
```

Run the `tier_a_scoring` stage, then the `build_review_page` stage, and
watch their live logs — functionally identical to step 2 above, just
clickable instead of typed.

## Note on live, in-browser toggles

The review page's signal chips (e.g. "cites-retracted", "ORI finding") let
you turn a signal **on/off** and see the ranking re-sort instantly in the
browser, with no rebuild needed. That's different from what this doc covers:
chips only zero a signal's contribution or restore it — they can't set a
weight to an arbitrary new value. To actually change *how much* a signal is
worth, you need the edit-and-rebuild steps above.
