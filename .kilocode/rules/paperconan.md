# Running paperconan in this project

**Always run paperconan through the project wrapper, never the raw CLI directly:**

```bash
python runs/run_paperconan.py <doi>
# e.g. python runs/run_paperconan.py 10.3389/fimmu.2018.00063
```

Do NOT run `paperconan <dir>` or `paperconan fetch ...` by hand. The wrapper
enforces this repo's conventions that the bare CLI knows nothing about:

1. **Output location** — one folder per paper at `runs/<doi-with-__>/` (DOI with
   `/` replaced by `__`), with paperconan's audit under `data/audit/`.
2. **Cache-first** — if `runs/<doi-with-__>/data/` already holds the paper's
   files, it scans them and does NOT re-fetch from the network. Only when absent
   does it fetch the open supplementary data into that `data/` dir.
3. **Provenance** — writes a `meta.yaml` draft (finding counts from `scan.json`)
   and never clobbers an already-adjudicated `meta.yaml`.

After a run, a human/agent still hand-writes the narrative `CONCLUSION.md` and
the real `conclusion:` line in `meta.yaml` — paperconan output is **signal, not
verdict** (plan.md §0). The review page (`graph_processing/build_review_page.py`)
reads these `meta.yaml` files to show the result on each paper card.

Full details: [`runs/README.md`](../../runs/README.md).
