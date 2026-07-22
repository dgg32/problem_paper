#!/usr/bin/env python3
"""
pull_retraction_watch_csv.py — MANUAL, on-demand refresh of the local
retraction_watch.csv snapshot from its upstream source (skill_worth_
exploring.md 2026-07-22: "RW Database daily CSV").

Deliberately NOT scheduled/cron'd and NOT wired into review/pipeline_app.py's
stage registry. This whole project's external-rate sensors added 2026-07-22
(publisher/country/journal/author retraction rate, all keyed off this one
csv as their numerator) make the underlying data's freshness matter more
than it used to -- but the user explicitly wants to FREEZE the current
snapshot to publish an article against a known, reproducible dataset, and
only pull a fresh copy when THEY choose to, not on any timer. Run this file
by hand; nothing else in the pipeline calls it.

Source: Crossref's own GitLab mirror of the Retraction Watch database,
https://gitlab.com/crossref/retraction-watch-data -- the same CSV schema
already consumed via .env.yaml's data.retraction_watch_csv, just pulled live
instead of a static copy.

Safety, since this file feeds several already-scored Tier-A signals and a
bad pull would corrupt all of them silently:
  - Downloads to a temp file first; the live csv is only replaced after the
    download passes sanity checks (never a partial/corrupt file swapped in).
  - Refuses to replace the file if the new copy is missing any of the core
    columns this project actually reads (Journal/Publisher/Country/Author/
    OriginalPaperDOI/Reason/...), or if its row count DROPS by more than
    DROP_TOLERANCE vs. the current file -- Retraction Watch only grows day
    to day, so a shrink beyond a small rounding margin means a bad/partial
    fetch or an upstream format change, not real data.
  - The file being replaced is archived first, timestamped, never deleted --
    retraction_watch/archive/retraction_watch_<old-mtime-date>.csv -- so the
    exact csv snapshot behind any past run (e.g. the one behind a published
    article) stays recoverable even after a later refresh.
  - Does NOT re-run any downstream sensor (publisher/country/journal/author
    rate scripts, tier_a_scoring.py, etc.) -- pulling fresh data and
    re-scoring against it are two separate, deliberate decisions. Re-run
    those by hand afterward if/when you actually want the refresh to count.

Usage:
  python graph_processing/pull_retraction_watch_csv.py             # pull + swap
  python graph_processing/pull_retraction_watch_csv.py --dry-run    # check only, no write
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".env.yaml"
cfg = yaml.safe_load(CONFIG_PATH.read_text())
CSV_PATH = (REPO_ROOT / cfg["data"]["retraction_watch_csv"]).resolve()
ARCHIVE_DIR = CSV_PATH.parent / "archive"

SOURCE_URL = "https://gitlab.com/crossref/retraction-watch-data/-/raw/main/retraction_watch.csv"

# A real schema change would break every sensor that reads these columns
# (extract_enrich.py, publisher/country/journal/institution retraction rate,
# refresh_retraction_status.py, ...) -- checked before ever touching the live
# file. Not the full column list (Notes/Paywalled/etc. can shift harmlessly).
CORE_COLUMNS = {
    "Record ID", "Title", "Journal", "Publisher", "Country", "Author",
    "OriginalPaperDOI", "OriginalPaperDate", "RetractionDate", "Reason",
}

# Retraction Watch only grows day to day. A drop this large means a bad
# fetch (truncated download, upstream outage serving an error page/empty
# file) or an upstream restructuring -- not real data -- so abort rather
# than silently adopting fewer records than we already have.
DROP_TOLERANCE = 0.01


def count_rows(path: Path) -> tuple[int, set[str]]:
    """(row count, column set) -- uses csv.reader so embedded commas/newlines
    in free-text fields (Notes, Title) don't corrupt the count like a naive
    line-count would."""
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        header = set(next(reader))
        n = sum(1 for _ in reader)
    return n, header


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="check + report only, never write anything")
    args = ap.parse_args()

    if not CSV_PATH.exists():
        print(f"ERROR: no existing file at {CSV_PATH} -- this script only REFRESHES an existing "
              f"snapshot, it won't bootstrap one from scratch.", file=sys.stderr)
        sys.exit(1)

    old_n, old_cols = count_rows(CSV_PATH)
    old_mtime = date.fromtimestamp(CSV_PATH.stat().st_mtime)
    print(f"[pull_retraction_watch_csv] current snapshot: {old_n} rows, dated {old_mtime} "
          f"({CSV_PATH})", file=sys.stderr)

    print(f"[pull_retraction_watch_csv] fetching {SOURCE_URL} ...", file=sys.stderr)
    try:
        r = requests.get(SOURCE_URL, timeout=120, stream=True)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: download failed ({e}) -- current file left untouched.", file=sys.stderr)
        sys.exit(1)

    tmp_fd, tmp_path_str = tempfile.mkstemp(dir=CSV_PATH.parent, suffix=".csv.tmp")
    tmp_path = Path(tmp_path_str)
    try:
        with open(tmp_fd, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)

        new_n, new_cols = count_rows(tmp_path)
        missing = CORE_COLUMNS - new_cols
        if missing:
            print(f"ERROR: downloaded file is missing core column(s) {sorted(missing)} -- "
                  f"looks like a schema change or a bad fetch, NOT applying. Current file untouched.",
                  file=sys.stderr)
            sys.exit(1)

        drop = (old_n - new_n) / old_n if old_n else 0
        print(f"[pull_retraction_watch_csv] downloaded snapshot: {new_n} rows "
              f"({'+' if new_n >= old_n else ''}{new_n - old_n} vs. current)", file=sys.stderr)
        if drop > DROP_TOLERANCE:
            print(f"ERROR: new file has {drop:.1%} FEWER rows than the current one (tolerance "
                  f"{DROP_TOLERANCE:.0%}) -- Retraction Watch only grows day to day, so this looks "
                  f"like a bad/partial fetch. NOT applying. Current file untouched.", file=sys.stderr)
            sys.exit(1)

        if args.dry_run:
            print(f"\n[dry-run] would replace {CSV_PATH} ({old_n} rows, {old_mtime}) with the "
                  f"downloaded snapshot ({new_n} rows) -- no files were changed.", file=sys.stderr)
            return

        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        archive_path = ARCHIVE_DIR / f"retraction_watch_{old_mtime}.csv"
        if archive_path.exists():
            # Same-day re-pull -- don't clobber an already-archived snapshot.
            archive_path = ARCHIVE_DIR / f"retraction_watch_{old_mtime}_{datetime.now():%H%M%S}.csv"
        shutil.copy2(CSV_PATH, archive_path)
        print(f"[pull_retraction_watch_csv] archived current snapshot -> {archive_path}", file=sys.stderr)

        tmp_path.replace(CSV_PATH)
        print(f"[pull_retraction_watch_csv] wrote new snapshot: {new_n} rows -> {CSV_PATH}", file=sys.stderr)
        print(
            "\nNOTE: only the raw csv changed. Nothing downstream was re-run. If you want this "
            "refresh to actually count, re-run (in order): institution_retraction_rate.py, "
            "publisher_retraction_rate.py, country_retraction_rate.py, "
            "journal_retraction_rate_external.py, author_retraction_rate_external.py, then "
            "tier_a_scoring.py / flag_evidence_report.py / build_review_page.py.",
            file=sys.stderr,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
