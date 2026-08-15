#!/usr/bin/env python3
"""
full_text_fetcher.py — fetch open-access full text for a DOI.

Strategy (in order of preference):
  1. Crossref work record -> look for links with content-type text/plain,
     text/xml, application/pdf, or HTML and an open license.
  2. Europe PMC search by DOI -> if a PMC ID exists, fetch the XML full text.
  3. PubMed Central (US) idconv -> DOI to PMC ID, then fetch XML/txt.

Returns a dict:
  {
    "doi": <canonical doi>,
    "status": "ok" | "not_open_access" | "not_found" | "error",
    "source": "crossref" | "europepmc" | "pmc_us" | None,
    "url": <the URL that provided the text>,
    "text": <plain text, best effort>,
    "error": <message if status != ok>,
  }

Text extraction is intentionally lightweight: strip XML/HTML tags, decode
entities, collapse whitespace. No PDF parsing in the first pass — if the only
full-text link is a PDF we mark it as not_open_access rather than pulling in
PyPDF2/poppler dependencies.

Usage:
  python sensors/full_text_fetcher.py --doi 10.xxx/xxx
  python sensors/full_text_fetcher.py --doi 10.xxx/xxx --cache-dir ./data/full_text_cache
"""
from __future__ import annotations

import argparse
import html
import json
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path

import requests
import yaml

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except Exception:  # noqa: BLE001
    HAS_PYPDF = False

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".env.yaml"

cfg = yaml.safe_load(CONFIG_PATH.read_text())
CROSSREF = cfg.get("crossref", {})
PUBMED = cfg.get("pubmed", {})

USER_AGENT = "fraud-paper-scanner/0.1 (mailto:{}; research integrity POC)"


def canon_doi(doi: str) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


def _headers(mailto: str) -> dict:
    return {
        "User-Agent": USER_AGENT.format(mailto),
        "Accept": "application/json, text/html, application/xml, text/xml",
    }


def _get(url: str, params: dict | None = None, timeout: int = 30) -> requests.Response:
    mailto = CROSSREF.get("mailto", "")
    return requests.get(url, params=params, headers=_headers(mailto), timeout=timeout)


def _strip_xml_tags(xml_text: str) -> str:
    """Remove XML tags and decode entities; keep paragraph breaks as newlines."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
        text_parts = []
        for elem in root.iter():
            if elem.text and elem.text.strip():
                text_parts.append(elem.text.strip())
            if elem.tail and elem.tail.strip():
                text_parts.append(elem.tail.strip())
        text = "\n".join(text_parts)
    except ET.ParseError:
        # Fallback regex strip
        text = re.sub(r"<[^>]+>", "\n", xml_text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_html_tags(html_text: str) -> str:
    # Drop scripts and styles first to avoid matching their content.
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html_text, flags=re.DOTALL | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _pdf_text(data: bytes) -> str | None:
    """Extract text from PDF bytes using pypdf."""
    if not HAS_PYPDF or not data.startswith(b"%PDF"):
        return None
    try:
        reader = PdfReader(BytesIO(data))
        parts = []
        for page in reader.pages:
            txt = page.extract_text()
            if txt:
                parts.append(txt)
        text = "\n".join(parts)
        text = re.sub(r"\s+", " ", text)
        return text.strip() if len(text) > 200 else None
    except Exception:  # noqa: BLE001
        return None


def unpaywall_full_text(doi: str) -> dict | None:
    """Use Unpaywall to find an open-access PDF or HTML location."""
    try:
        mailto = CROSSREF.get("mailto", "")
        url = f"https://api.unpaywall.org/v2/{doi}"
        r = _get(url, params={"email": mailto}, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        if not data.get("is_oa"):
            return None
        loc = data.get("best_oa_location") or {}
        if not loc:
            return None
        target = loc.get("url_for_pdf") or loc.get("url")
        if not target:
            return None
        rr = _get(target, timeout=30)
        if rr.status_code in (403, 404, 401):
            return None
        rr.raise_for_status()
        ct = rr.headers.get("content-type", "").lower()
        if ct.startswith("application/pdf"):
            text = _pdf_text(rr.content)
        else:
            text = _strip_html_tags(rr.text)
        if not text or len(text) < 200:
            return None
        return {
            "doi": doi,
            "status": "ok",
            "source": "unpaywall",
            "url": target,
            "text": text,
            "error": None,
        }
    except Exception:  # noqa: BLE001
        return None


def crossref_full_text(doi: str) -> dict | None:
    """Ask Crossref for open full-text links. Returns fetch result or None."""
    try:
        url = f"{CROSSREF['base_url'].rstrip('/')}/works/{doi}"
        r = _get(url, params={"mailto": CROSSREF.get("mailto", "")}, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        msg = r.json().get("message", {})

        # Prefer links with explicit content-type and a license URL
        best = None
        for link in msg.get("link", []):
            ct = (link.get("content-type") or "").lower()
            lu = link.get("URL", "")
            if ct in ("text/plain", "text/xml", "application/xml") and lu:
                best = link
                break
            if ct == "text/html" and lu and not best:
                best = link
            if ct == "unspecified" and lu and not best:
                best = link
            if ct == "application/pdf" and lu and not best:
                best = link

        if not best:
            return None

        target = best["URL"]
        ct = (best.get("content-type") or "").lower()
        rr = _get(target, timeout=30)
        rr.raise_for_status()

        if ct == "application/pdf" or rr.headers.get("content-type", "").lower().startswith("application/pdf"):
            text = _pdf_text(rr.content)
            if not text:
                return None
        else:
            raw = rr.text
            if ct in ("text/xml", "application/xml") or raw.strip().startswith("<?xml") or ("<article" in raw[:500] and "<!doctype" not in raw[:200].lower()):
                text = _strip_xml_tags(raw)
            else:
                text = _strip_html_tags(raw)

        if not text or len(text) < 200:
            return None

        return {
            "doi": doi,
            "status": "ok",
            "source": "crossref",
            "url": target,
            "text": text,
            "error": None,
        }
    except Exception:  # noqa: BLE001
        return None


def europepmc_full_text(doi: str) -> dict | None:
    """Search Europe PMC by DOI and, if a PMC ID exists, fetch XML full text."""
    try:
        base = "https://www.ebi.ac.uk/europepmc/webservices/rest"
        search_url = f"{base}/search"
        params = {"query": f"DOI:{doi}", "format": "json", "pageSize": 1}
        r = _get(search_url, params=params, timeout=10)
        r.raise_for_status()
        results = r.json().get("resultList", {}).get("result", [])
        if not results:
            return None
        pmcid = results[0].get("pmcid")
        if not pmcid:
            return None

        # /fullTextXML is the real endpoint (returns JATS XML). The old code used
        # /fullText, which 404s — so Europe PMC, the best OA source, never fired
        # and in-PMC papers fell through to short Crossref landing-page text.
        ft_url = f"{base}/{pmcid}/fullTextXML"
        rr = _get(ft_url, timeout=30)
        if rr.status_code in (404, 403, 401):
            return None
        rr.raise_for_status()
        text = _strip_xml_tags(rr.text)
        if len(text) < 200:
            return None
        return {
            "doi": doi,
            "status": "ok",
            "source": "europepmc",
            "url": f"https://europepmc.org/article/PMC/{pmcid}",
            "text": text,
            "error": None,
        }
    except Exception:  # noqa: BLE001
        return None


def pmc_us_full_text(doi: str) -> dict | None:
    """Use NCBI idconv to get PMC ID, then fetch the XML full text."""
    try:
        idconv_url = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
        params = {"ids": doi, "format": "json"}
        if PUBMED.get("api_key"):
            params["api_key"] = PUBMED["api_key"]
        r = _get(idconv_url, params=params, timeout=10)
        r.raise_for_status()
        records = r.json().get("records", [])
        pmcid = None
        for rec in records:
            if rec.get("pmcid"):
                pmcid = rec["pmcid"]
                break
        if not pmcid:
            return None

        ft_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/?format=xml"
        rr = _get(ft_url, timeout=20)
        if rr.status_code in (404, 403, 401):
            return None
        rr.raise_for_status()
        text = _strip_xml_tags(rr.text)
        if len(text) < 200:
            return None
        return {
            "doi": doi,
            "status": "ok",
            "source": "pmc_us",
            "url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/",
            "text": text,
            "error": None,
        }
    except Exception:  # noqa: BLE001
        return None


# A result shorter than this is a landing page / abstract, not real full text.
# Real OA full text is typically >15k chars; landing pages are <3k.
MIN_FULLTEXT = 4000


def fetch_full_text(doi: str, cache_dir: Path | None = None, force: bool = False) -> dict:
    """Fetch full text with caching.

    Sources are tried best-first (Europe PMC JATS XML is real, complete full
    text; Crossref/Unpaywall links are often just landing pages), and the LONGEST
    successful text wins — so a short Crossref result never shadows real full
    text. Stops early once a source clears MIN_FULLTEXT. `force` re-fetches even
    a cached result (used to re-run past the old broken-endpoint cache)."""
    canonical = canon_doi(doi)
    if not canonical:
        return {"doi": doi, "status": "error", "source": None, "url": None, "text": "", "error": "empty DOI"}

    cache_file = None
    if cache_dir:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{canonical.replace('/', '__')}.json"
        if cache_file.exists() and not force:
            try:
                cached = json.loads(cache_file.read_text())
                # Trust the cache only if it's already good full text — otherwise
                # fall through and retry (the old cache is full of short results).
                if cached.get("doi") == canonical and len(cached.get("text") or "") >= MIN_FULLTEXT:
                    return cached
            except Exception:  # noqa: BLE001
                pass

    best = None
    for source_fn in (europepmc_full_text, crossref_full_text, unpaywall_full_text, pmc_us_full_text):
        r = source_fn(canonical)
        if r and r.get("text"):
            if best is None or len(r["text"]) > len(best["text"]):
                best = r
            if len(best["text"]) >= MIN_FULLTEXT:
                break  # good enough — real full text, stop

    result = best or {
        "doi": canonical,
        "status": "not_open_access",
        "source": None,
        "url": None,
        "text": "",
        "error": "no open-access full-text source found via Europe PMC/Crossref/Unpaywall/PMC",
    }

    if cache_file:
        cache_file.write_text(json.dumps(result, indent=2))

    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", required=True, help="DOI to fetch")
    ap.add_argument("--cache-dir", default=str(REPO_ROOT / "data" / "full_text_cache"), help="cache directory")
    args = ap.parse_args()

    res = fetch_full_text(args.doi, Path(args.cache_dir))
    print(json.dumps({k: v for k, v in res.items() if k != "text"}, indent=2))
    if res["status"] == "ok":
        print(f"\ntext length: {len(res['text'])} chars")
        print("---snippet---")
        print(res["text"][:800])


if __name__ == "__main__":
    main()
