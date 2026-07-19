#!/usr/bin/env python3
"""
pubpeer_comment_checker.py — Phase 4 sensor #3 (plan.md "pubpeer-comment-scanner").

plan.md's framing: "Comments there are the single highest-yield human signal."
This 2026-07-18 rework makes the sensor actually fetch that signal, replacing
the earlier router-only version (which had concluded no keyless structured path
existed — disproved the same day by live probing).

Verified mechanism (all confirmed live 2026-07-18):

  Phase 1  DOI -> PubPeer record.  POST https://pubpeer.com/v3/publications
           ?devkey=PubMedChrome with JSON {"dois": [...]}. This is the lookup
           channel PubPeer's OWN browser extension uses (endpoint and devkey
           are taken verbatim from the publicly distributed extension source,
           js/contentScript/pubpeer.js) — it is keyless, batch-capable (one
           POST resolves many DOIs), and returns per-DOI: total_comments,
           canonical /publications/<id> url, last_commented_at, commenter
           names. DOIs with no PubPeer record are simply absent from the
           response. The site's own /api/search/ JSON is NOT usable
           programmatically: it sits behind Cloudflare Turnstile and never
           clears in automation (verified in headless AND headed Chromium).

  Phase 2  pubpeer_id -> comment bodies.  GET /publications/<id> is plain
           server-rendered HTML: no JS, no key, no challenge. It embeds
           :data-comments-count, :data-publication and :data-comments as
           HTML-escaped JSON attributes with every comment's body, author,
           is_from_author flag and accepted_at timestamp.

Compliance posture (robots.txt, fetched 2026-07-18):
  User-agent: * -> Allow: / , Crawl-delay: 10 , Disallow: /search
  (ClaudeBot/GPTBot/CCBot & friends are named-blocked site-wide.)
  Consequences implemented here:
    * neither phase touches /search — both use bot-allowed paths;
    * a single site-wide rate limiter enforces the 10 s crawl-delay;
    * Phase 1 batches up to `batch_size` DOIs per POST, and resolutions are
      cached forever (not_on_pubpeer for `negative_cache_days`), so network
      volume stays minimal;
    * --counts-only skips Phase 2 entirely for fast bulk triage.
  If PubPeer ever grants this project an official key, set pubpeer.devkey in
  .env.yaml — check_doi_via_api() is the stub to implement against their docs.

What this sensor does NOT do (unchanged from the router version): it never
auto-verdicts a paper. Comment counts measure *community attention*, not guilt
— sound papers attract comments and fraudulent ones can have none. severity is
therefore only "review" (a human should read this) or "info" (nothing to read),
and this sensor stays OUT of tier_a_scoring.py WEIGHTS by design.

Usage:
  python sensors/pubpeer_comment_checker.py                  # all candidates -> report
  python sensors/pubpeer_comment_checker.py --doi 10.xxx/xxx  # single paper, stdout
  python sensors/pubpeer_comment_checker.py --limit 20        # first 20 candidates
  python sensors/pubpeer_comment_checker.py --counts-only     # no comment bodies (fast)
  python sensors/pubpeer_comment_checker.py --refresh         # ignore cache, re-resolve
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

CONFIG_PATH = REPO_ROOT / ".env.yaml"
REPORT_JSON = REPO_ROOT / "data" / "flags" / "pubpeer_flags.json"
CACHE_JSON = REPO_ROOT / "data" / "pubpeer_cache.json"

cfg = yaml.safe_load(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
PUBPEER = cfg.get("pubpeer", {}) or {}

API_URL = "https://pubpeer.com/v3/publications"
DEVKEY = PUBPEER.get("devkey") or "PubMedChrome"  # public key from PubPeer's own extension
BATCH_SIZE = int(PUBPEER.get("batch_size", 50))
CRAWL_DELAY = float(PUBPEER.get("crawl_delay_s", 10))
NEGATIVE_CACHE_DAYS = int(PUBPEER.get("negative_cache_days", 30))
MAX_COMMENT_CHARS = int(PUBPEER.get("max_comment_chars", 4000))
USER_AGENT = PUBPEER.get("user_agent") or (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# robots.txt Crawl-delay: one site-wide limiter shared by both phases.
# ---------------------------------------------------------------------------
_last_request_at = 0.0


def throttle() -> None:
    global _last_request_at
    wait = CRAWL_DELAY - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


# ---------------------------------------------------------------------------
# DOI -> PubPeer-record cache. Records are permanent; "not_on_pubpeer" ages
# out after NEGATIVE_CACHE_DAYS (a paper can be commented on at any time).
# ---------------------------------------------------------------------------
def load_cache() -> dict:
    if CACHE_JSON.exists():
        try:
            return json.loads(CACHE_JSON.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE_JSON.parent.mkdir(parents=True, exist_ok=True)
    CACHE_JSON.write_text(json.dumps(cache, indent=2, sort_keys=True))


def cached_resolution(cache: dict, doi: str) -> dict | None:
    entry = cache.get(doi.lower())
    if not entry:
        return None
    if entry.get("status") == "not_on_pubpeer":
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(entry["resolved_at"])
            if age > timedelta(days=NEGATIVE_CACHE_DAYS):
                return None
        except (KeyError, ValueError):
            return None
    return entry


# ---------------------------------------------------------------------------
# Phase 1: batched DOI resolution through PubPeer's extension endpoint.
# ---------------------------------------------------------------------------
class ResolveError(RuntimeError):
    """The lookup channel failed — must NOT be reported as 'no comments'."""


def resolve_dois(dois: list[str]) -> dict[str, dict]:
    """Resolve a batch of DOIs. Returns {doi_lower: feedback} only for DOIs
    that HAVE a PubPeer record with comments; all others are absent."""
    found: dict[str, dict] = {}
    for i in range(0, len(dois), BATCH_SIZE):
        batch = dois[i:i + BATCH_SIZE]
        throttle()
        try:
            r = requests.post(
                f"{API_URL}?devkey={DEVKEY}",
                headers={
                    "Content-Type": "application/json;charset=UTF-8",
                    "User-Agent": USER_AGENT,
                },
                json={"version": "1.6.2", "browser": "Chrome", "urls": [], "dois": batch},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise ResolveError(f"POST {API_URL} failed: {exc}") from exc
        if r.status_code != 200:
            raise ResolveError(f"POST {API_URL} -> HTTP {r.status_code}")
        try:
            feedbacks = r.json().get("feedbacks") or []
        except json.JSONDecodeError as exc:
            raise ResolveError(f"POST {API_URL} -> non-JSON response") from exc
        for fb in feedbacks:
            if fb.get("id"):
                found[fb["id"].lower()] = fb
    return found


def _entry_from_feedback(fb: dict) -> dict:
    return {
        "status": "resolved",
        "pubpeer_id": (fb.get("url") or "").rstrip("/").rsplit("/", 1)[-1],
        "comments_total": fb.get("total_comments") or 0,
        "last_commented_at": fb.get("last_commented_at"),
        "commenter_names": fb.get("users") or "",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Phase 2: curl-grade fetch of /publications/<id> (bot-allowed path).
# ---------------------------------------------------------------------------
def _json_attr(raw: str, name: str):
    m = re.search(rf':{name}="([^"]*)"', raw, re.S)
    if not m:
        return None
    try:
        return json.loads(html.unescape(m.group(1)))
    except json.JSONDecodeError:
        return None


def _html_to_text(raw_html: str) -> str:
    """Clean-text fallback for comments whose 'markdown' field is null.
    Confirmed live (2026-07-19) that PubPeer's comment JSON carries a separate
    'html' field that is populated even when 'markdown' is None -- markdown is
    null on plenty of ordinary comments, not just an edge case (e.g. older
    2015-era comments, replies). Without this fallback, fetch_publication()
    silently produced an empty-string comment body."""
    text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _comment_body(c: dict) -> tuple[str, list[str]]:
    """Returns (text, image_urls) preferring 'markdown', falling back to
    'html' (see _html_to_text) when markdown is null/empty -- which is common,
    not rare (verified: 189/594 comments in the initial batch run had null
    markdown; 0 had both markdown and html empty)."""
    md = c.get("markdown") or ""
    if md.strip():
        return md, re.findall(r"!\[[^\]]*\]\(([^)]+)\)", md)
    raw_html = c.get("html") or ""
    return _html_to_text(raw_html), re.findall(r'<img[^>]+src="([^"]+)"', raw_html, re.I)


def fetch_publication(pubpeer_id: str) -> dict | None:
    url = f"https://pubpeer.com/publications/{pubpeer_id}"
    throttle()
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            timeout=30,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    raw = r.text

    publication = _json_attr(raw, "data-publication") or {}
    comments_raw = _json_attr(raw, "data-comments") or []
    m = re.search(r':data-comments-count="(\d+)"', raw)
    comments_total = int(m.group(1)) if m else publication.get("comments_total") or 0

    comments = []
    for c in comments_raw:
        body, images = _comment_body(c)
        user = c.get("user") or {}
        comments.append({
            "n": c.get("inner_id"),
            "by": c.get("user_alias") or user.get("display_name"),
            "verified_commenter": bool(user.get("verified")),
            "author_response": bool(c.get("is_from_author")),
            "accepted_at": c.get("accepted_at"),
            "text": body[:MAX_COMMENT_CHARS] + ("…[truncated]" if len(body) > MAX_COMMENT_CHARS else ""),
            "images": images,
        })

    dates = sorted(c["accepted_at"] for c in comments if c.get("accepted_at"))
    return {
        "pubpeer_url": url,
        "comments_total": comments_total,
        "author_responses": sum(1 for c in comments if c["author_response"]),
        "first_comment_at": dates[0] if dates else None,
        "last_comment_at": dates[-1] if dates else publication.get("last_commented"),
        "commenters": {
            name: sum(1 for c in comments if c["by"] == name)
            for name in sorted({c["by"] for c in comments if c["by"]})
        },
        "comments": comments,
    }


# ---------------------------------------------------------------------------
# Flag records
# ---------------------------------------------------------------------------
def search_url(doi: str) -> str:
    return f"https://pubpeer.com/search?q={quote(doi)}"


def _manual_record(doi: str, title: str | None, reason: str) -> dict:
    return {
        "flag": "pubpeer_comments",
        "status": "manual_check_required",
        "severity": "info",
        "reason": reason,
        "paper_doi": doi,
        "paper_title": title,
        "check_url": search_url(doi),
    }


def check_doi_via_api(doi: str) -> dict | None:
    """Stub for an official keyed path, should PubPeer grant one. The default
    devkey above is the public extension key, which IS the implemented Phase 1
    — this stub is only for a future project-specific key with its own terms."""
    raise NotImplementedError(
        "pubpeer.devkey is set to a project-specific key but check_doi_via_api() "
        "is unimplemented — confirm the schema/terms with PubPeer first."
    )


def check_paper(
    doi: str,
    title: str | None = None,
    *,
    cache: dict,
    counts_only: bool = False,
) -> dict:
    """Two-phase check for one paper, given a pre-populated cache. Never a
    verdict — see module docstring."""
    entry = cached_resolution(cache, doi)
    if entry is None:
        return _manual_record(
            doi, title,
            "not resolved in this run (resolve error, or run without --refresh first)",
        )

    if entry.get("status") == "not_on_pubpeer":
        return {
            "flag": "pubpeer_comments",
            "status": "not_on_pubpeer",
            "severity": "info",
            "reason": "no PubPeer record with comments for this DOI "
            f"(checked {entry.get('resolved_at', '?')[:10]}).",
            "paper_doi": doi,
            "paper_title": title,
            "comments_total": 0,
            "check_url": search_url(doi),
        }

    total = entry.get("comments_total") or 0
    base = {
        "flag": "pubpeer_comments",
        "paper_doi": doi,
        "paper_title": title,
        "pubpeer_id": entry["pubpeer_id"],
        "pubpeer_url": f"https://pubpeer.com/publications/{entry['pubpeer_id']}",
        "comments_total": total,
    }

    if total < 1:
        return {**base, "status": "no_comments", "severity": "info",
                "reason": "PubPeer record exists with no comments.",
                "check_url": base["pubpeer_url"]}

    if counts_only:
        return {
            **base,
            "status": "comments_found",
            "severity": "review",
            "reason": f"{total} PubPeer comment(s); last activity "
            f"{entry.get('last_commented_at') or '?'}. Commenters: "
            f"{entry.get('commenter_names') or '?'}. "
            "Community attention, not a misconduct verdict.",
            "check_url": base["pubpeer_url"],
            "last_comment_at": entry.get("last_commented_at"),
            "commenters": entry.get("commenter_names") or "",
        }

    pub = fetch_publication(entry["pubpeer_id"])
    if pub is None:
        return _manual_record(
            doi, title,
            f"fetch of /publications/{entry['pubpeer_id']} failed — check manually",
        )

    return {
        **base,
        "status": "comments_found",
        "severity": "review",
        "reason": (
            f"{pub['comments_total']} PubPeer comment(s) "
            f"({pub['author_responses']} author response(s)); "
            f"first {str(pub['first_comment_at'])[:10]}, "
            f"last {str(pub['last_comment_at'])[:10]}. "
            "Community attention, not a misconduct verdict — read the comments."
        ),
        "check_url": pub["pubpeer_url"],
        "comments_total": pub["comments_total"],
        "author_responses": pub["author_responses"],
        "first_comment_at": pub["first_comment_at"],
        "last_comment_at": pub["last_comment_at"],
        "commenters": pub["commenters"],
        "comments": pub["comments"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doi", help="check a single paper by DOI (stdout only)")
    ap.add_argument("--limit", type=int, help="only the first N candidates")
    ap.add_argument("--counts-only", action="store_true",
                    help="resolve counts/commenters only; skip comment-body fetches")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore cached resolutions and re-resolve every DOI")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))

    if args.doi:
        with driver.session(database=conn["database"]) as s:
            row = s.run(
                "MATCH (p:Paper {doi: $doi}) RETURN p.title AS title",
                doi=args.doi,
            ).single()
        driver.close()
        candidates = [{"doi": args.doi, "title": row["title"] if row else None}]
    else:
        query = ("MATCH (p:Paper {is_retracted:false}) "
                 "RETURN p.doi AS doi, p.title AS title ORDER BY p.doi")
        if args.limit:
            query += f" LIMIT {args.limit}"
        with driver.session(database=conn["database"]) as s:
            candidates = [dict(r) for r in s.run(query)]
        driver.close()

    cache = load_cache()

    # Phase 1 (batched): resolve every DOI that has no fresh cache entry.
    to_resolve = [
        c["doi"] for c in candidates
        if args.refresh or cached_resolution(cache, c["doi"]) is None
    ]
    if to_resolve:
        print(f"  resolving {len(to_resolve)} DOI(s) via {API_URL} ...", file=sys.stderr)
        try:
            found = resolve_dois(to_resolve)
        except ResolveError as exc:
            print(f"  WARNING: {exc}", file=sys.stderr)
            found = {}
        now = datetime.now(timezone.utc).isoformat()
        for doi in to_resolve:
            fb = found.get(doi.lower())
            cache[doi.lower()] = (
                _entry_from_feedback(fb) if fb
                else {"status": "not_on_pubpeer", "resolved_at": now}
            )
        save_cache(cache)
        print(f"  {sum(1 for d in to_resolve if d.lower() in found)} "
              f"with PubPeer comments, "
              f"{sum(1 for d in to_resolve if d.lower() not in found)} without",
                  file=sys.stderr)

    # Phase 2 (per paper with comments, unless --counts-only).
    all_flags = []
    for i, cand in enumerate(candidates, 1):
        doi, title = cand["doi"], cand["title"]
        print(f"  [{i}/{len(candidates)}] {doi}", file=sys.stderr)
        all_flags.append(check_paper(doi, title, cache=cache, counts_only=args.counts_only))

    if args.doi:
        print(json.dumps(all_flags[0], indent=2, ensure_ascii=False))
        return

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(all_flags, indent=2, ensure_ascii=False))

    by_status: dict[str, int] = {}
    for rec in all_flags:
        by_status[rec["status"]] = by_status.get(rec["status"], 0) + 1

    print("\n=== pubpeer-comment-checker ===")
    for status, n in sorted(by_status.items()):
        print(f"  {status:<24}: {n}")
    print(f"  report written -> {REPORT_JSON.relative_to(REPO_ROOT)}")
    print(
        "\n  NOTE: comment counts are a review pointer, not a misconduct signal "
        "(see module docstring). This sensor stays out of tier_a WEIGHTS."
    )


if __name__ == "__main__":
    main()
