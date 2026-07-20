#!/usr/bin/env python3
"""
_crossref_verify.py — OpenAlex data-corruption / name-substitution guardrail.

Every expansion script (expand_targets.py, add_manual_target.py,
add_manual_target_by_doi.py) writes OpenAlex's per-work title/author/orcid
fields to the graph as-is. Confirmed live (2026-07-20, Ping Wang expansion):
that data isn't always trustworthy.
  - `10.1016/j.molcel.2015.03.033`'s OpenAlex record returned the title and
    author of a completely unrelated Numismatic Chronicle paper.
  - On `10.1038/s41586-024-08248-5` (and 5 other DOIs), OpenAlex substituted
    a different real person's name for the target's own authorship position
    -- once because it resolved the display name from the (mismatched)
    ORCID account instead of the paper's own byline, and separately because
    its author-entity disambiguation confused two different same-surname
    researchers.
Crossref carries the publisher-deposited record, which is what actually
governs authorship credit, so it's the cross-check here -- not the primary
data source (OpenAlex's structured institution/topic data stays better for
that; this module only verifies before a write, never replaces the import).

Both checks below raise LookupUnavailable on transport failure rather than
silently passing a paper through on a network blip -- per plan.md §0, a
transient error must never quietly become either a false accusation or a
false clean bill of health.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".env.yaml"
cfg = yaml.safe_load(CONFIG_PATH.read_text())
CROSSREF = cfg.get("crossref", {})

TITLE_MATCH_THRESHOLD = 0.7


class LookupUnavailable(Exception):
    """Crossref could not be reached -- distinct from 'no record'."""


def _canon_doi(doi: str) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _name_key(name: str) -> str:
    """Same normalization as build_instances.name_key -- word-boundary
    preserving (hyphens become spaces), used for whole-name identity
    comparisons where 'Ping Wang' must stay distinguishable from 'Peijun
    Wang'."""
    s = _strip_accents(name).lower()
    s = s.replace("‐", " ").replace("-", " ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _squash(name: str) -> str:
    """Aggressive normalization for detecting pure formatting variance
    within one name token (accents, hyphenation, internal spacing) --
    'Chen-Chen', 'Chen Chen', and 'Chenchen' must all compare equal here.
    Not used for whole-name identity comparisons (that needs _name_key's
    word boundaries), only for deciding whether a given/family-name
    difference between OpenAlex and Crossref is a real conflict or just
    a spelling-convention mismatch."""
    s = _strip_accents(name).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def crossref_record(doi: str) -> dict | None:
    """Fetch a Crossref work record. None on a genuine 404 (no record to
    contradict OpenAlex with); raises LookupUnavailable on transport/parse
    error."""
    canon = _canon_doi(doi)
    if not canon:
        return None
    try:
        r = requests.get(f"{CROSSREF['base_url']}/works/{canon}",
                          params={"mailto": CROSSREF.get("mailto", "")}, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("message", {})
    except (requests.RequestException, ValueError) as e:
        raise LookupUnavailable(f"Crossref lookup failed for {doi}: {e}") from e


def title_matches_crossref(doi: str, openalex_title: str) -> tuple[bool, str | None]:
    """(ok, crossref_title). ok=True if Crossref has no record for this DOI
    (nothing to contradict) or its title closely matches OpenAlex's.
    ok=False means the two sources disagree about what paper this DOI even
    is -- confirmed live once (full title+author swap)."""
    rec = crossref_record(doi)
    if rec is None:
        return True, None
    ctitle = (rec.get("title") or [""])[0]
    if not ctitle or not openalex_title:
        return True, ctitle
    ratio = difflib.SequenceMatcher(None, ctitle.lower(), openalex_title.lower()).ratio()
    return ratio >= TITLE_MATCH_THRESHOLD, ctitle


def name_in_crossref_authors(doi: str, name: str) -> bool | None:
    """True if `name` (fuzzy match, case/diacritic/hyphen-insensitive)
    appears among Crossref's deposited authors for this DOI. None if
    Crossref has no record for this DOI (can't assess -- caller should fall
    back to trusting OpenAlex rather than treat this as a failure). Catches
    silent author-name substitution -- confirmed live twice."""
    rec = crossref_record(doi)
    if rec is None:
        return None
    want = _name_key(name)
    for a in rec.get("author", []):
        got = _name_key(f"{a.get('given', '')} {a.get('family', '')}")
        if got == want:
            return True
    return False


def _is_initials(given: str) -> bool:
    """True if every token of a given name is a bare 1-letter initial (e.g.
    'P', 'J A') -- Crossref deposits vary by era/journal, and older records
    commonly carry only initials. An initials-only given name can't
    distinguish 'Ping' from 'Peijun' (both reduce to 'P'), so it must never
    be treated as confirming OR contradicting a fuller name."""
    tokens = given.replace(".", " ").split()
    return bool(tokens) and all(len(t) == 1 for t in tokens)


def _name_signature(tokens: list[str]) -> str:
    """Order-insensitive canonical form: Chinese-origin names get Romanized
    in either given-family or family-given order depending on the source
    (confirmed live: OpenAlex and Crossref disagree on which token is the
    surname for the same person on the same paper -- 'Li Yu' vs 'Yu Li',
    'Fang Lan' vs 'Lan Fang', etc.). Sorting the squashed tokens before
    joining makes those equal, so only an actual content difference (a
    different token, not just different token order) trips the comparison."""
    return "".join(sorted(_squash(t) for t in tokens if t))


def reconcile_authors_with_crossref(doi: str, parsed_authors: list[dict]) -> tuple[list[dict], list[str]]:
    """Cross-check OpenAlex-parsed authors (list of dicts with a 'name' and
    'orcid' key, in paper order) against Crossref's deposited author list at
    the same DOI, correcting the display name only on a genuine conflict.

    Deliberately conservative -- confirmed live that cruder rules actively
    harm data quality:
      - Crossref records (especially older ones) commonly deposit only
        initials, and downgrading OpenAlex's full name ('Peijun Wang') to
        Crossref's initial ('P Wang') would both lose real information AND
        fail to resolve the one case that actually matters (initials can't
        tell 'Ping' from 'Peijun' apart -- resolving that substitution
        required external co-authorship-network evidence, out of scope
        here) -- so an initials-only Crossref given name is never compared.
      - given/family order for Chinese-origin names isn't stable between
        the two sources for the SAME real person ('Li Yu' vs 'Yu Li') --
        so token order is ignored; only a genuine token-content difference
        counts as a conflict.
    Only acts when the two author lists are the same length (order/count is
    normally stable between sources for one paper; when it isn't, this
    silently leaves the OpenAlex data untouched rather than guess at a
    realignment). Returns (possibly-corrected authors, list of human-readable
    change descriptions for logging) -- never raises on 'no record'; raises
    LookupUnavailable only on transport failure, propagated from
    crossref_record."""
    rec = crossref_record(doi)
    if rec is None:
        return parsed_authors, []
    cr_authors = rec.get("author", [])
    if len(cr_authors) != len(parsed_authors):
        return parsed_authors, []

    changes: list[str] = []
    out = list(parsed_authors)
    for i, (oa_auth, cr_auth) in enumerate(zip(out, cr_authors)):
        cr_given = (cr_auth.get("given") or "").strip()
        cr_family = (cr_auth.get("family") or "").strip()
        if not cr_family or _is_initials(cr_given):
            continue
        oa_name = oa_auth.get("name", "")
        oa_tokens = oa_name.split()
        if any(_is_initials(t) for t in oa_tokens):
            continue

        cr_tokens = (cr_given.split() if cr_given else []) + [cr_family]
        if _name_signature(oa_tokens) == _name_signature(cr_tokens):
            continue
        # OpenAlex carrying every Crossref token plus an extra (e.g. a
        # middle name Crossref never deposited) is enrichment, not conflict
        # -- confirmed live ('Xiao Jian Tan' vs Crossref's plain 'Xiao Tan',
        # same person). Only a genuinely substituted token is a conflict.
        oa_squashed = {_squash(t) for t in oa_tokens}
        cr_squashed = {_squash(t) for t in cr_tokens}
        if cr_squashed <= oa_squashed:
            continue

        cr_name = f"{cr_given} {cr_family}".strip()
        cr_orcid = cr_auth.get("ORCID")
        cr_orcid = cr_orcid.rstrip("/").rsplit("/", 1)[-1] if cr_orcid else None
        changed = dict(oa_auth)
        changed["name"] = cr_name
        if cr_orcid:
            changed["orcid"] = cr_orcid
        elif oa_auth.get("orcid"):
            # A confirmed content-level name conflict (not just reordering)
            # means OpenAlex's orcid likely belongs to a different real
            # person; keeping it would misattribute it.
            changed["orcid"] = None
        out[i] = changed
        changes.append(f"position {i}: OpenAlex said {oa_name!r}, "
                        f"Crossref says {cr_name!r} -- corrected")
    return out, changes
