#!/usr/bin/env python3
"""
extract_enrich.py — Phase 1 seed extraction + author ORCID enrichment.

Reads the Retraction Watch CSV, selects the target subject subset (default:
microbiology) keeping only rows that carry BOTH an original-paper DOI and a
PubMed ID, then enriches each paper's authors with ORCID + canonical
affiliations from OpenAlex (primary) and PubMed E-utilities (fallback).

Output follows the graph schema in plan.md §3:
  Nodes:  Author, Paper, Journal, Institution, Reason
  Edges:  WROTE, PUBLISHED_IN, INVOLVES, AFFILIATED_WITH, RETRACTED_FOR

Two output forms are written to --outdir:
  * graph.json      — full per-paper enriched records (for reprocessing/debug)
  * nodes_*.tsv /
    rel_*.tsv       — one file per node/edge type, ready for LadybugDB COPY
                      (or Neo4j bulk import)

No LLM is used: name/DOI/date normalization is deterministic, and author +
institution identities come from OpenAlex/ORCID canonical entities. A seam for
optional LLM reconciliation of unmatched institution strings is noted below but
intentionally not wired in.

Config (credentials, rate limits, paths) is read from ../.env.yaml.

Usage:
  python extract_enrich.py --test            # 5 papers, quick smoke test
  python extract_enrich.py --limit 50        # first 50 matching papers
  python extract_enrich.py                   # full subset (all microbiology)
  python extract_enrich.py --subset bls      # life-science slice instead
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import requests
from requests.adapters import HTTPAdapter
import yaml
from tqdm import tqdm

# CSV has some very long affiliation fields; lift the field-size cap.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".env.yaml"

ORCID_RE = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])")

# Retraction Watch reason codes that reflect a formal, adjudicated finding
# (as opposed to a plain retraction). Authoring a paper retracted for one of
# these marks an author "adjudicated" — a sourced fact, traced back to the
# triggering reason + paper DOI (see plan.md §0 on facts-not-verdicts).
ADJUDICATION_REASONS = {
    "Misconduct - Official Investigation(s) and/or Finding(s)",
    "Investigation by ORI",
}


# --------------------------------------------------------------------------- #
# Config + rate limiting
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class RateLimiter:
    """Enforce a minimum interval between calls (thread-safe, simple)."""

    def __init__(self, per_second: float):
        self.min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._last = 0.0
        self._lock = Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self.min_interval - (now - self._last)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last = time.monotonic()


def get_json(session, url, params, limiter, tries=4):
    """GET JSON with rate limiting + backoff on 429/5xx. None on hard failure."""
    for attempt in range(tries):
        limiter.wait()
        try:
            r = session.get(url, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        return None
    return None


def get_text(session, url, params, limiter, tries=4):
    for attempt in range(tries):
        limiter.wait()
        try:
            r = session.get(url, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.text
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        return None
    return None


# --------------------------------------------------------------------------- #
# Normalization helpers (deterministic — no LLM)
# --------------------------------------------------------------------------- #
def canonical_doi(raw: str) -> str:
    if not raw:
        return ""
    doi = raw.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi.strip()


def canonical_orcid(raw) -> str:
    if not raw:
        return ""
    m = ORCID_RE.search(str(raw))
    return m.group(1) if m else ""


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def name_key(name: str) -> str:
    """Normalized name for cross-paper author matching (weak identity)."""
    if not name:
        return ""
    s = strip_accents(name).lower()
    s = s.replace("‐", " ").replace("-", " ")  # unify hyphens (incl. U+2010)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def split_semicolon(raw: str):
    if not raw:
        return []
    return [p.strip() for p in raw.split(";") if p.strip()]


def tsv_clean(value) -> str:
    """Make a value safe for a single TSV cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value)
    return s.replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


def parse_date(raw: str) -> str:
    """Retraction Watch dates look like 'M/D/YYYY H:MM'. Return ISO date or ''."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:
        mth, day, yr = m.groups()
        if yr != "0000":
            return f"{yr}-{int(mth):02d}-{int(day):02d}"
    return ""


# --------------------------------------------------------------------------- #
# Enrichment sources
# --------------------------------------------------------------------------- #
def fetch_openalex_work(session, doi, pmid, limiter, cfg):
    base = cfg["openalex"]["base_url"].rstrip("/")
    params = {"mailto": cfg["openalex"].get("mailto", "")}
    # Prefer DOI lookup; fall back to PMID.
    for ident in (f"doi:{doi}" if doi else None, f"pmid:{pmid}" if pmid else None):
        if not ident:
            continue
        data = get_json(session, f"{base}/works/{ident}", params, limiter)
        if data:
            return data
    return None


def parse_openalex_authors(work: dict):
    """Return list of author dicts from an OpenAlex work."""
    authors = []
    for a in work.get("authorships", []):
        auth = a.get("author") or {}
        institutions = []
        for inst in a.get("institutions", []):
            institutions.append(
                {
                    "id": inst.get("ror") or inst.get("id") or "",
                    "name": inst.get("display_name", ""),
                    "country": inst.get("country_code", ""),
                }
            )
        raw_aff = a.get("raw_affiliation_strings") or []
        authors.append(
            {
                "name": auth.get("display_name") or a.get("raw_author_name", ""),
                "orcid": canonical_orcid(auth.get("orcid") or a.get("raw_orcid")),
                "position": a.get("author_position", ""),
                "is_corresponding": bool(a.get("is_corresponding")),
                "affiliation": raw_aff[0] if raw_aff else "",
                "institutions": institutions,
                "orcid_source": "openalex"
                if canonical_orcid(auth.get("orcid") or a.get("raw_orcid"))
                else "",
            }
        )
    return authors


def fetch_pubmed_authors(session, pmid, limiter, cfg):
    """Return {name_key: {'orcid','affiliation'}} parsed from PubMed efetch XML."""
    pm = cfg["pubmed"]
    params = {
        "db": "pubmed",
        "id": str(pmid),
        "retmode": "xml",
        "email": pm.get("email", ""),
        "tool": pm.get("tool_name", "fraud-paper-scanner"),
    }
    if pm.get("api_key"):
        params["api_key"] = pm["api_key"]
    text = get_text(session, f"{pm['base_url'].rstrip('/')}/efetch.fcgi", params, limiter)
    if not text:
        return {}
    out = {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    for a in root.iter("Author"):
        last = a.findtext("LastName") or ""
        fore = a.findtext("ForeName") or ""
        collective = a.findtext("CollectiveName") or ""
        full = f"{fore} {last}".strip() or collective
        if not full:
            continue
        orcid = ""
        for ident in a.findall("Identifier"):
            if ident.get("Source") == "ORCID":
                orcid = canonical_orcid(ident.text)
        aff = a.findtext(".//Affiliation") or ""
        out[name_key(full)] = {"orcid": orcid, "affiliation": aff, "name": full}
    return out


def merge_pubmed_orcids(oa_authors, pm_map):
    """Fill missing ORCIDs/affiliations on OpenAlex authors from PubMed by name."""
    for author in oa_authors:
        if author["orcid"]:
            continue
        pm = pm_map.get(name_key(author["name"]))
        if pm and pm.get("orcid"):
            author["orcid"] = pm["orcid"]
            author["orcid_source"] = "pubmed"
        if pm and not author["affiliation"] and pm.get("affiliation"):
            author["affiliation"] = pm["affiliation"]
    return oa_authors


def authors_from_pubmed_only(pm_map):
    """When OpenAlex has no record, build authors from PubMed alone."""
    authors = []
    for info in pm_map.values():
        authors.append(
            {
                "name": info["name"],
                "orcid": info.get("orcid", ""),
                "position": "",
                "is_corresponding": False,
                "affiliation": info.get("affiliation", ""),
                "institutions": [],  # PubMed gives raw strings only, no canonical IDs
                "orcid_source": "pubmed" if info.get("orcid") else "",
            }
        )
    return authors


def authors_from_csv(row):
    """Last-resort authors from the Retraction Watch CSV `Author` column.

    Used only when both OpenAlex and PubMed return no authors — typically
    withdrawn papers whose author metadata was scrubbed upstream. The CSV
    still records the original author names (no ORCID/affiliation).
    """
    authors = []
    for name in split_semicolon(row.get("Author", "")):
        authors.append(
            {
                "name": name,
                "orcid": "",
                "position": "",
                "is_corresponding": False,
                "affiliation": "",
                "institutions": [],
                "orcid_source": "",
            }
        )
    return authors


# --------------------------------------------------------------------------- #
# Per-paper record
# --------------------------------------------------------------------------- #
def build_record(row, work, oa_authors):
    doi = canonical_doi(row["OriginalPaperDOI"])
    pmid = row["OriginalPaperPubMedID"].strip()
    nature = row.get("RetractionNature", "").strip()

    journal = row.get("Journal", "").strip()
    publisher = row.get("Publisher", "").strip()
    title = row.get("Title", "").strip()
    pub_date = parse_date(row.get("OriginalPaperDate", ""))
    openalex_id = ""

    if work:
        openalex_id = (work.get("ids") or {}).get("openalex", "")
        title = work.get("title") or title
        pub_date = work.get("publication_date") or pub_date
        src = (work.get("primary_location") or {}).get("source") or {}
        journal = src.get("display_name") or journal
        publisher = src.get("host_organization_name") or publisher

    # Paper-level institutions = union of authors' canonical institutions.
    paper_institutions = {}
    for a in oa_authors:
        for inst in a["institutions"]:
            if inst["id"]:
                paper_institutions[inst["id"]] = inst

    return {
        "doi": doi,
        "pmid": pmid,
        "openalex_id": openalex_id,
        "title": title,
        "published_date": pub_date,
        "journal": journal,
        "publisher": publisher,
        # A retraction is a fact; nature distinguishes true retraction from
        # expression-of-concern / correction / reinstatement (plan §0).
        "is_retracted": nature == "Retraction",
        "retraction_nature": nature,
        "retraction_date": parse_date(row.get("RetractionDate", "")),
        "reasons": split_semicolon(row.get("Reason", "")),
        "csv_institutions": split_semicolon(row.get("Institution", "")),
        "authors": oa_authors,
        "referenced_works_count": (work or {}).get("referenced_works_count", 0),
        "referenced_works": (work or {}).get("referenced_works", []),  # for later CITES
        "enrichment": {
            "openalex": bool(work),
            "n_authors": len(oa_authors),
            "n_orcid": sum(1 for a in oa_authors if a["orcid"]),
        },
    }


# --------------------------------------------------------------------------- #
# Graph accumulation + writers
# --------------------------------------------------------------------------- #
class GraphAccumulator:
    def __init__(self):
        self.authors = {}       # author_id -> (name, orcid, name_key)
        self.author_adjudication = {}  # author_id -> {'reasons': set, 'dois': set}
        self.papers = {}        # doi -> tuple
        self.journals = {}      # name -> publisher
        self.institutions = {}  # inst_id -> (name, country)
        self.reasons = set()
        self.wrote = {}         # (author_id, doi) -> row
        self.published_in = set()
        self.involves = set()
        self.affiliated_with = set()
        self.retracted_for = set()

    @staticmethod
    def author_id(author):
        return author["orcid"] if author["orcid"] else f"name:{name_key(author['name'])}"

    def add_paper(self, rec):
        doi = rec["doi"]
        if not doi:
            return
        self.papers[doi] = (
            doi, rec["pmid"], rec["openalex_id"], rec["title"],
            rec["published_date"], rec["is_retracted"],
            rec["retraction_date"], rec["retraction_nature"],
        )
        if rec["journal"]:
            self.journals.setdefault(rec["journal"], rec["publisher"])
            self.published_in.add((doi, rec["journal"]))
        for reason in rec["reasons"]:
            self.reasons.add(reason)
            self.retracted_for.add((doi, reason))
        adj_reasons = [r for r in rec["reasons"] if r in ADJUDICATION_REASONS]

        for author in rec["authors"]:
            aid = self.author_id(author)
            if adj_reasons:
                adj = self.author_adjudication.setdefault(aid, {"reasons": set(), "dois": set()})
                adj["reasons"].update(adj_reasons)
                adj["dois"].add(doi)
            existing = self.authors.get(aid)
            # keep first non-empty orcid/name we see
            self.authors[aid] = (
                author["name"] or (existing[0] if existing else ""),
                author["orcid"] or (existing[1] if existing else ""),
                name_key(author["name"]) or (existing[2] if existing else ""),
            )
            self.wrote[(aid, doi)] = (
                aid, doi, author["position"],
                author["is_corresponding"], author["affiliation"],
                "",  # email — not available from either source (plan §1)
                author["orcid_source"],
            )
            for inst in author["institutions"]:
                if not inst["id"]:
                    continue
                self.institutions.setdefault(inst["id"], (inst["name"], inst["country"]))
                self.affiliated_with.add((aid, inst["id"]))
                self.involves.add((doi, inst["id"]))

    # -- writers --------------------------------------------------------- #
    def write_tsvs(self, outdir: Path):
        def dump(name, header, rows):
            with open(outdir / name, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f, delimiter="\t", lineterminator="\n")
                w.writerow(header)
                for r in rows:
                    w.writerow([tsv_clean(c) for c in r])

        author_rows = []
        for aid, vals in self.authors.items():
            adj = self.author_adjudication.get(aid)
            author_rows.append((
                aid, *vals,
                bool(adj),
                ";".join(sorted(adj["reasons"])) if adj else "",
                ";".join(sorted(adj["dois"])) if adj else "",
            ))
        dump("nodes_author.tsv",
             ["author_id", "name", "orcid", "name_key",
              "adjudicated", "adjudicated_reasons", "adjudicated_dois"],
             author_rows)
        dump("nodes_paper.tsv",
             ["doi", "pmid", "openalex_id", "title", "published_date",
              "is_retracted", "retraction_date", "retraction_nature"],
             self.papers.values())
        dump("nodes_journal.tsv", ["name", "publisher"],
             [(n, p) for n, p in self.journals.items()])
        dump("nodes_institution.tsv", ["institution_id", "name", "country"],
             [(iid, *vals) for iid, vals in self.institutions.items()])
        dump("nodes_reason.tsv", ["code"], [(c,) for c in sorted(self.reasons)])

        dump("rel_wrote.tsv",
             ["author_id", "doi", "author_position", "is_corresponding",
              "affiliation", "email", "orcid_source"],
             self.wrote.values())
        dump("rel_published_in.tsv", ["doi", "journal_name"], sorted(self.published_in))
        dump("rel_involves.tsv", ["doi", "institution_id"], sorted(self.involves))
        dump("rel_affiliated_with.tsv", ["author_id", "institution_id"],
             sorted(self.affiliated_with))
        dump("rel_retracted_for.tsv", ["doi", "reason_code"], sorted(self.retracted_for))

    def summary(self):
        return {
            "authors": len(self.authors),
            "authors_with_orcid": sum(1 for v in self.authors.values() if v[1]),
            "authors_adjudicated": len(self.author_adjudication),
            "papers": len(self.papers),
            "journals": len(self.journals),
            "institutions": len(self.institutions),
            "reasons": len(self.reasons),
            "edges_wrote": len(self.wrote),
            "edges_published_in": len(self.published_in),
            "edges_involves": len(self.involves),
            "edges_affiliated_with": len(self.affiliated_with),
            "edges_retracted_for": len(self.retracted_for),
        }


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
SUBSET_MATCH = {
    "microbiology": lambda subj: "Microbiology" in subj,
    "bls": lambda subj: "(BLS)" in subj or "Microbiology" in subj,
}

# Retraction Watch uses these sentinels for "no identifier".
DOI_SENTINELS = {"", "unavailable", "na", "n/a", "none", "null"}


def valid_doi(raw: str) -> bool:
    doi = canonical_doi(raw)
    return doi not in DOI_SENTINELS and doi.startswith("10.")


def valid_pmid(raw: str) -> bool:
    pmid = (raw or "").strip()
    return pmid.isdigit() and pmid != "0"


def select_rows(csv_path, subset, limit):
    """Rows in the subject subset with BOTH a valid DOI and a valid PMID.

    "Valid" excludes Retraction Watch sentinels ('unavailable', PMID '0', etc.)
    that otherwise pass a bare non-empty check and produce author-less orphans.
    """
    match = SUBSET_MATCH.get(subset, SUBSET_MATCH["microbiology"])
    rows = []
    with open(csv_path, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            if not match(row.get("Subject", "")):
                continue
            if not valid_doi(row.get("OriginalPaperDOI", "")):
                continue
            if not valid_pmid(row.get("OriginalPaperPubMedID", "")):
                continue
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    return rows


# --------------------------------------------------------------------------- #
# Per-paper worker (pure: fetch + build, no shared-state mutation)
# --------------------------------------------------------------------------- #
def process_paper(row, session, oa_limiter, pm_limiter, cfg):
    """Fetch + enrich one paper. Returns (record, per_paper_stats).

    Safe to run in a thread: it only touches the (thread-safe) session and
    rate limiters and returns a fresh record. Graph accumulation happens
    single-threaded in the caller.
    """
    doi = canonical_doi(row["OriginalPaperDOI"])
    pmid = row["OriginalPaperPubMedID"].strip()
    stats = {"openalex_hit": 0, "pubmed_fallback_orcids": 0,
             "no_openalex": 0, "pubmed_author_rescue": 0, "csv_author_fallback": 0}

    work = fetch_openalex_work(session, doi, pmid, oa_limiter, cfg)
    pm_map = fetch_pubmed_authors(session, pmid, pm_limiter, cfg)

    if work:
        stats["openalex_hit"] = 1
        oa_authors = parse_openalex_authors(work)
        if not oa_authors and pm_map:
            # OpenAlex found the work but has no authorships — rescue from PubMed.
            oa_authors = authors_from_pubmed_only(pm_map)
            stats["pubmed_author_rescue"] = 1
        else:
            before = sum(1 for a in oa_authors if a["orcid"])
            oa_authors = merge_pubmed_orcids(oa_authors, pm_map)
            stats["pubmed_fallback_orcids"] = sum(1 for a in oa_authors if a["orcid"]) - before
    else:
        stats["no_openalex"] = 1
        oa_authors = authors_from_pubmed_only(pm_map)

    if not oa_authors:
        # Both sources scrubbed the author list (withdrawn papers) — use CSV.
        oa_authors = authors_from_csv(row)
        if oa_authors:
            stats["csv_author_fallback"] = 1

    return build_record(row, work, oa_authors), stats


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Seed extraction + author ORCID enrichment.")
    ap.add_argument("--subset", default=None, help="microbiology | bls (default: config)")
    ap.add_argument("--limit", type=int, default=None, help="max papers to process")
    ap.add_argument("--test", action="store_true", help="shortcut for --limit 5")
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent fetch workers (default 8). Global API rates "
                         "are still capped by the shared rate limiters.")
    ap.add_argument("--outdir", default=str(REPO_ROOT / "data" / "graph"))
    args = ap.parse_args()

    cfg = load_config()
    subset = args.subset or cfg.get("data", {}).get("seed_subset", "microbiology")
    limit = 5 if args.test else args.limit
    csv_path = (REPO_ROOT / cfg["data"]["retraction_watch_csv"]).resolve()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    oa_limiter = RateLimiter(cfg["openalex"].get("requests_per_second", 10))
    pm_limiter = RateLimiter(cfg["pubmed"].get("requests_per_second", 3))
    session = requests.Session()
    session.headers.update({"User-Agent": "fraud-paper-scanner/0.1 (mailto:%s)"
                            % cfg["openalex"].get("mailto", "")})
    # Size the connection pool for the worker count so threads don't contend.
    adapter = HTTPAdapter(pool_connections=args.workers, pool_maxsize=args.workers * 2)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    rows = select_rows(csv_path, subset, limit)
    print(f"Subset='{subset}'  papers: {len(rows)}  workers: {args.workers}  ->  {outdir}")

    acc = GraphAccumulator()
    stats = {"openalex_hits": 0, "pubmed_fallback_orcids": 0,
             "no_openalex": 0, "pubmed_author_rescue": 0, "csv_author_fallback": 0}
    # Fetch concurrently; accumulate into the (non-thread-safe) graph in the
    # main thread as futures complete. Keep records in input order for
    # reproducible output.
    results = [None] * len(rows)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        fut_to_idx = {
            pool.submit(process_paper, row, session, oa_limiter, pm_limiter, cfg): i
            for i, row in enumerate(rows)
        }
        for fut in tqdm(as_completed(fut_to_idx), total=len(rows),
                        desc="enrich", unit="paper"):
            idx = fut_to_idx[fut]
            rec, pstats = fut.result()
            results[idx] = rec
            stats["openalex_hits"] += pstats["openalex_hit"]
            stats["pubmed_fallback_orcids"] += pstats["pubmed_fallback_orcids"]
            stats["no_openalex"] += pstats["no_openalex"]
            stats["pubmed_author_rescue"] += pstats["pubmed_author_rescue"]
            stats["csv_author_fallback"] += pstats["csv_author_fallback"]

    records = results
    for rec in records:
        acc.add_paper(rec)

    # ---- write outputs ---- #
    with open(outdir / "graph.json", "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    acc.write_tsvs(outdir)

    summary = {"subset": subset, "processed": len(rows), **stats, "graph": acc.summary()}
    with open(outdir / "run_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n=== Summary ===")
    print(json.dumps(summary, indent=2))
    print(f"\nWrote graph.json + nodes_*.tsv / rel_*.tsv to {outdir}")


if __name__ == "__main__":
    main()
