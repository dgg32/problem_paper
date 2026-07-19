#!/usr/bin/env python3
"""
run_paperconan.py — project entrypoint that drives the paperconan CLI so it
honors THIS repo's conventions, instead of scattering output wherever it's run.

Why a wrapper and not a skill edit: paperconan is a general CLI (installed in
.venv); the naming/caching rules below are project-specific and belong in the
repo, versioned next to the data, not baked into a reusable global skill.

What it enforces:
  1. Output convention — one run folder per paper at runs/<doi-with-__>/, DOI
     with '/' -> '__' (same as data/pdfs/ and data/full_text_cache/). paperconan
     writes its audit into <input-dir>/audit/, so pointing it at that run's
     data/ dir lands the archive layout automatically.
  2. Cache-first, then PMC-first — if runs/<doi>/data/ already holds the paper's
     files, scan them and DO NOT re-fetch. When absent, it acquires data in this
     order: (a) download the Europe PMC supplementary ZIP if one exists (the same
     source the review-page 📎 badge / suppl_data_check.py use — paperconan's own
     `fetch` CANNOT see PMC); (b) only if there's no PMC suppl, fall back to
     `paperconan fetch --auto` (open data repositories: Zenodo/Dryad/figshare).
     (--force-fetch ignores the cache; --skip-pmc skips step (a).)
  3. Provenance — after the scan it writes a meta.yaml DRAFT (mechanical fields +
     severity counts derived from scan.json), with the conclusion left for
     human/agent adjudication. The narrative CONCLUSION.md is always hand-written
     — plan.md §0: paperconan output is signal, not verdict, so this script never
     auto-manufactures a conclusion.

Usage:
  python runs/run_paperconan.py 10.3389/fimmu.2018.00063
  python runs/run_paperconan.py 10.3389/fimmu.2018.00063 --title "..." --images
  python runs/run_paperconan.py <doi> --force-fetch     # ignore cache, re-fetch
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS = REPO_ROOT / "runs"
PAPERCONAN = REPO_ROOT / ".venv" / "bin" / "paperconan"  # project venv CLI
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"         # for the vendored image screen
IMG_DIR = REPO_ROOT / "sensors" / "image_forensics"
IMAGE_SCREEN = IMG_DIR / "image_similarity_screen.py"
FIGURE_CLASSIFIER = IMG_DIR / "figure_classifier.py"
# Router-gated wet-lab screens: screen name (from figure_classifier ROUTING) -> script.
WETLAB_SCREENS = {
    "blot_gel_lane_audit": "blot_gel_lane_audit.py",
    "microscopy_reuse_screen": "microscopy_reuse_screen.py",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

# Europe PMC: the source our review-page 📎 badge + suppl_data_check.py use.
# This is DIFFERENT from what `paperconan fetch` searches (data repositories like
# Zenodo/Dryad) — PMC supplementary files are not visible to paperconan's fetch,
# which is why "available on PMC" never got picked up until we fetch it here.
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_SUPPL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"

# Files under a run's data/ that are NOT scannable inputs (so their presence
# alone must not count as a cache hit).
NON_INPUT = {"audit", "paperconan_source.json"}

# What paperconan's numeric scan can actually read (from its own CLI message):
# .xlsx via openpyxl, legacy .xls/.xlsm/.xlsb via calamine, .csv/.tsv, and tables
# inside .pdf/.docx. A ZIP of only .tif/.gif/.jpg has nothing for it.
TABULAR_EXTS = {".xlsx", ".xls", ".xlsm", ".xlsb", ".csv", ".tsv", ".pdf", ".docx"}


def doi_dirname(doi: str) -> str:
    return doi.replace("/", "__")


def _http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "problem-paper-poc/1.0 (research-integrity triage)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_pmc_suppl(doi: str, data_dir: Path) -> bool:
    """PMC-first fetch: if Europe PMC has supplementary files for this DOI,
    download the ZIP and unzip it into data_dir. Returns True if files landed.

    Re-derives the PMC id from the DOI live (no Neo4j needed), so the wrapper
    works standalone even if the graph isn't running or the node was never
    checked by suppl_data_check.py."""
    q = urllib.parse.urlencode(
        {"query": f'DOI:"{doi}"', "resultType": "core", "format": "json", "pageSize": 1})
    try:
        data = json.loads(_http_get(f"{EPMC_SEARCH}?{q}"))
    except Exception as e:  # noqa: BLE001 — network/parse; fall back gracefully
        print(f"  PMC lookup failed ({e}); will try paperconan repository fetch")
        return False
    results = data.get("resultList", {}).get("result", [])
    if not results:
        return False
    rec = results[0]
    pmcid = rec.get("pmcid")
    if (rec.get("hasSuppl") or "").upper() != "Y" or not pmcid:
        return False

    print(f"  PMC has supplementary files ({pmcid}) — downloading ZIP from Europe PMC")
    try:
        blob = _http_get(EPMC_SUPPL.format(pmcid=pmcid))
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except Exception as e:  # noqa: BLE001
        print(f"  PMC suppl download/unzip failed ({e}); will try paperconan repository fetch")
        return False

    extracted = 0
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        safe = Path(name).name  # flatten to basename — defuses zip-slip / nested paths
        if not safe:
            continue
        (data_dir / safe).write_bytes(zf.read(name))
        extracted += 1
    if extracted:
        # Record real provenance so meta.yaml's file_source isn't the "local"
        # default (write_source_stub only writes when this is absent).
        (data_dir / "paperconan_source.json").write_text(json.dumps(
            {"doi": doi, "title": "", "source": "europepmc",
             "cand_id": f"europepmc:{pmcid}", "related_dois": []}, indent=2) + "\n")
    print(f"  extracted {extracted} file(s) from PMC into {_rel(data_dir)}")
    return extracted > 0


def _rel(p: Path) -> str:
    """Repo-relative path for display, tolerant of paths outside the repo."""
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def run_image_screen(data_dir: Path) -> dict | None:
    """Run the vendored aHash image-reuse screen on the figures in data_dir.

    Complements paperconan (which does NOT touch images): so an images-only ZIP
    that paperconan can't scan still gets a forensic result. Saves the full JSON
    to audit/image_screen.json and returns a compact summary. Signal, not verdict
    — never scored (see sensors/image_forensics/NOTICE.md). Returns None if there
    are no images."""
    if not IMAGE_SCREEN.exists():
        return None
    imgs = [p for p in data_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if not imgs:
        return None
    try:
        out = subprocess.run([str(VENV_PY), str(IMAGE_SCREEN), str(data_dir), "--format", "json"],
                             cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)
        result = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        print(f"  image screen failed ({e}); skipping")
        return None
    audit = data_dir / "audit"
    audit.mkdir(exist_ok=True)
    (audit / "image_screen.json").write_text(json.dumps(result, indent=2))
    findings = result.get("findings", [])
    n_img = result.get("metadata", {}).get("image_count", len(imgs))
    print(f"  image screen: {result.get('risk_level')} ({n_img} images, {len(findings)} reuse finding(s))")
    return {
        "risk_level": result.get("risk_level", "unknown"),
        "n_images": n_img,
        "n_findings": len(findings),
        "pairs": [f.get("location", "") for f in findings][:6],
    }


def run_wetlab_screens(data_dir: Path) -> dict | None:
    """Router-GATED wet-lab screens (opt-in, --wetlab). figure_classifier decides
    which images are confidently a given assay type; each specialized screen runs
    ONLY on its routed subset. This is what makes the wet-lab screens usable at
    all — run ungated they false-positive to BLACK on charts/composites (see
    sensors/image_forensics/NOTICE.md). Not scored; signal, not verdict."""
    imgs = [p for p in data_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if not imgs or not FIGURE_CLASSIFIER.exists():
        return None
    try:
        out = subprocess.run([str(VENV_PY), str(FIGURE_CLASSIFIER), str(data_dir), "--format", "json"],
                             cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)
        routing = json.loads(out.stdout).get("routing", {})
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        print(f"  figure classifier failed ({e}); skipping wet-lab screens")
        return None
    if not routing:
        print("  wet-lab screens: no figures confidently routed (all charts/composites) — none run")
        return {"routed": {}, "screens": {}}
    audit = data_dir / "audit"
    audit.mkdir(exist_ok=True)
    screens: dict[str, dict] = {}
    full: dict[str, object] = {}
    for screen, files in routing.items():
        script = WETLAB_SCREENS.get(screen)
        if not script:
            continue
        try:
            r = subprocess.run([str(VENV_PY), str(IMG_DIR / script), *files, "--format", "json"],
                               cwd=REPO_ROOT, capture_output=True, text=True, timeout=600)
            res = json.loads(r.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError):
            continue
        full[screen] = res
        screens[screen] = {"n_files": len(files), "risk_level": res.get("risk_level", "?"),
                           "n_findings": len(res.get("findings", []))}
        print(f"  wet-lab [{screen}]: {screens[screen]['risk_level']} on {len(files)} routed image(s), "
              f"{screens[screen]['n_findings']} finding(s)")
    (audit / "wetlab_screens.json").write_text(json.dumps(full, indent=2))
    return {"routed": {k: len(v) for k, v in routing.items()}, "screens": screens}


def has_input_files(data_dir: Path) -> bool:
    if not data_dir.exists():
        return False
    return any(p.is_file() and p.name not in NON_INPUT for p in data_dir.iterdir())


def has_tabular_files(data_dir: Path) -> bool:
    return any(p.is_file() and p.suffix.lower() in TABULAR_EXTS for p in data_dir.iterdir())


def count_severities(scan: dict) -> tuple[dict, str | None]:
    """Walk the scan tree and tally severity-tagged findings; grab a top signal."""
    counts = {"high": 0, "medium": 0, "low": 0}
    top: str | None = None

    def walk(o):
        nonlocal top
        if isinstance(o, dict):
            sev = o.get("severity")
            if sev in counts:
                counts[sev] += 1
                if top is None and sev in ("high", "medium"):
                    kind, rule = o.get("kind", "finding"), o.get("rule", "")
                    top = f"{kind} · {rule}".strip(" ·")
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(scan)
    return counts, top


def run_cli(cmd: list) -> int:
    print("  $", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, cwd=REPO_ROOT).returncode


def write_source_stub(data_dir: Path, doi: str, title: str) -> None:
    """Only if paperconan fetch didn't already write a richer one."""
    src = data_dir / "paperconan_source.json"
    if src.exists():
        return
    src.write_text(json.dumps(
        {"doi": doi, "title": title, "source": "local", "cand_id": None, "related_dois": []},
        indent=2) + "\n")


def read_source_field(data_dir: Path, field: str) -> str | None:
    src = data_dir / "paperconan_source.json"
    if not src.exists():
        return None
    try:
        return json.loads(src.read_text()).get(field)
    except (json.JSONDecodeError, OSError):
        return None


def meta_target(run_dir: Path) -> Path:
    """Where to write the draft. Never clobber a hand-adjudicated meta.yaml: an
    existing meta WITHOUT the `needs_adjudication: true` marker is treated as
    adjudicated, so the fresh draft goes to meta.autodraft.yaml for comparison."""
    meta = run_dir / "meta.yaml"
    if meta.exists() and "needs_adjudication: true" not in meta.read_text():
        return run_dir / "meta.autodraft.yaml"
    return meta


# Non-scan outcomes: the run legitimately produced no numeric scan. Each records
# a distinct, honest reason (never a clean pass — plan.md §0/Phase 3).
OUTCOMES = {
    "no_data_files_available":
        "no machine-readable data files found to scan — NOT a clean result (plan.md §0/Phase 3)",
    "no_tabular_data":
        "supplementary files were fetched but none are tabular (figures/images only) — "
        "paperconan's numeric scan has nothing to run on; NOT a clean result. "
        "Re-run with --images for multimodal figure review.",
}


def write_meta(run_dir: Path, doi: str, title: str, scan: dict | None,
               outcome: str | None = None, image_screen: dict | None = None) -> None:
    """meta.yaml DRAFT — mechanical fields only; conclusion needs adjudication.
    `outcome` (a key of OUTCOMES) records a non-scan result instead of findings.
    `image_screen` (optional) adds the vendored image-reuse screen summary."""
    data_dir = run_dir / "data"
    file_source = read_source_field(data_dir, "source") or "unknown"
    if outcome:
        body = (
            f'doi: "{doi}"\n'
            f'title: "{title}"\n'
            'tool: paperconan\n'
            f'tool_version: "{_paperconan_version()}"\n'
            f'checked_date: "{date.today()}"\n'
            f'file_source: {file_source}\n'
            f'outcome: {outcome}\n'
            'needs_adjudication: false\n'
            f'conclusion: "{OUTCOMES[outcome]}"\n'
        )
    else:
        counts, top = count_severities(scan or {})
        title = (scan or {}).get("paper", {}).get("title", title) or title
        body = (
            '# paperconan run provenance (DRAFT auto-written by runs/run_paperconan.py).\n'
            '# Mechanical fields + severity counts only. The `conclusion` below is a\n'
            '# placeholder: paperconan output is signal, not verdict (plan.md §0) — a\n'
            '# human/agent must adjudicate and hand-write CONCLUSION.md.\n'
            f'doi: "{doi}"\n'
            f'title: "{title}"\n'
            'tool: paperconan\n'
            f'tool_version: "{(scan or {}).get("tool_version", _paperconan_version())}"\n'
            f'profile: {(scan or {}).get("profile", "forensic")}\n'
            f'scanned_at: "{(scan or {}).get("scanned_at", "")}"\n'
            f'file_source: {file_source}\n'
            f'n_files_scanned: {(scan or {}).get("n_files", 0)}\n'
            'findings:\n'
            f'  high: {counts["high"]}\n'
            f'  medium: {counts["medium"]}\n'
            f'  low: {counts["low"]}\n'
            f'top_finding: "{(top or "").replace(chr(34), chr(39))}"\n'
            'needs_adjudication: true\n'
            'conclusion: "TODO — adjudicate the findings and replace this line; paperconan output is signal, not verdict"\n'
        )
    if image_screen:
        # Not-scored image-reuse screen (vendored aHash). Signal, not verdict.
        body += (
            'image_screen:\n'
            '  tool: image_similarity_screen\n'
            f'  risk_level: "{image_screen["risk_level"]}"\n'
            f'  n_images: {image_screen["n_images"]}\n'
            f'  n_findings: {image_screen["n_findings"]}\n'
        )
    target = meta_target(run_dir)
    target.write_text(body)
    if target.name != "meta.yaml":
        print(f"  NOTE: adjudicated meta.yaml preserved; fresh draft written to {target.name} for comparison")


def _paperconan_version() -> str:
    try:
        out = subprocess.run([PAPERCONAN, "--version"], capture_output=True, text=True)
        return out.stdout.strip().split()[-1] if out.stdout else "unknown"
    except OSError:
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("doi", help="paper DOI, e.g. 10.3389/fimmu.2018.00063")
    ap.add_argument("--title", default="", help="optional title, improves fetch matching")
    ap.add_argument("--profile", default="forensic", choices=["review", "forensic", "triage"])
    ap.add_argument("--images", action="store_true", help="also register image assets (--images)")
    ap.add_argument("--force-fetch", action="store_true", help="ignore cached files and re-fetch")
    ap.add_argument("--skip-pmc", action="store_true",
                    help="skip the Europe PMC supplementary fetch; go straight to paperconan's repository search")
    ap.add_argument("--wetlab", action="store_true",
                    help="also run the router-gated wet-lab image screens (blot/microscopy); opt-in")
    args = ap.parse_args()

    if not PAPERCONAN.exists():
        sys.exit(f"paperconan CLI not found at {PAPERCONAN}\n  install: .venv/bin/pip install 'paperconan[all]'")

    run_dir = RUNS / doi_dirname(args.doi)
    data_dir = run_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    rel = data_dir.relative_to(REPO_ROOT)

    # 1 + 2. cache-first, else fetch into the convention-correct data/ dir.
    if has_input_files(data_dir) and not args.force_fetch:
        print(f"  cache hit — reusing files already in {rel} (no fetch)")
    else:
        why = "forced re-fetch" if args.force_fetch else f"no cached files in {rel}"
        print(f"  {why} — acquiring data for {args.doi}")
        # PMC-first: pull the Europe PMC supplementary ZIP (what the review-page
        # 📎 badge marks as available). paperconan's own fetch can't see PMC.
        got = False if args.skip_pmc else fetch_pmc_suppl(args.doi, data_dir)
        if not got:
            # Fall back to paperconan's repository search (Zenodo/Dryad/figshare).
            print("  no PMC supplementary files — trying paperconan repository fetch")
            run_cli([PAPERCONAN, "fetch", args.title or args.doi, "--auto", "--out", str(data_dir)]
                    + (["--images"] if args.images else []))
        if not has_input_files(data_dir):
            print("\n  no open data files were fetched — paperconan has nothing to scan.")
            print("  'no data found' is NOT 'paper is clean' — recording as a distinct outcome (§0/Phase 3).")
            write_source_stub(data_dir, args.doi, args.title)
            write_meta(run_dir, args.doi, args.title, None, outcome="no_data_files_available")
            return

    write_source_stub(data_dir, args.doi, args.title)

    # Image-reuse screen — runs on figures regardless of tabular data, so an
    # images-only paper still gets a forensic result (paperconan can't do images).
    img_screen = run_image_screen(data_dir)
    if args.wetlab:
        run_wetlab_screens(data_dir)  # router-gated; results archived to audit/wetlab_screens.json

    # Files present but none tabular (e.g. a figures-only PMC ZIP): paperconan's
    # numeric scan would fail with nothing to read. Record that honestly instead
    # of crashing — the data IS archived, it's just not numerically scannable.
    if not has_tabular_files(data_dir):
        print("\n  no tabular data for the numeric scan (figures/images only).")
        print("  Recording as 'no_tabular_data' (NOT clean); the image screen above still applies.")
        write_meta(run_dir, args.doi, args.title, None, outcome="no_tabular_data", image_screen=img_screen)
        return

    # 3. scan (audit lands in data/audit/ — the archive layout, automatically).
    rc = run_cli([PAPERCONAN, str(data_dir), "--profile", args.profile] + (["--images"] if args.images else []))
    scan_path = data_dir / "audit" / "scan.json"
    if rc != 0 or not scan_path.exists():
        sys.exit(f"  scan failed (rc={rc}); expected {_rel(scan_path)}")

    scan = json.loads(scan_path.read_text())
    write_meta(run_dir, args.doi, args.title, scan, image_screen=img_screen)
    counts, _ = count_severities(scan)
    print(f"\n  done — run archived at runs/{doi_dirname(args.doi)}/")
    print(f"    inputs + audit : data/  (audit/REPORT.md · report.html · scan.json)")
    print(f"    provenance     : meta.yaml (DRAFT — high={counts['high']} medium={counts['medium']} low={counts['low']})")
    print(f"  NEXT: adjudicate the findings, then hand-write CONCLUSION.md (§0: signal, not verdict).")


if __name__ == "__main__":
    main()
