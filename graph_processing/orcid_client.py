#!/usr/bin/env python3
"""
orcid_client.py — read researchers' PUBLIC works from the ORCID Public API.

Purpose (plan.md §2/§7): OpenAlex sometimes stamps the WRONG same-named person's
ORCID onto a paper (author-disambiguation error). A wrong stamp attributes a
retraction to an innocent researcher. The authoritative cross-check is the
person's OWN ORCID record: does *they* claim this DOI? This module answers that.

Auth: 2-legged client_credentials (scope /read-public). No user login; one
long-lived token reads any public record. Credentials live in ../.env.yaml.

CLI (quick check):
  python graph_processing/orcid_client.py 0000-0002-2103-5494
  python graph_processing/orcid_client.py 0000-0002-2103-5494 --has-doi 10.1128/jcm.01819-10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".env.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def canon_doi(doi: str) -> str:
    """Normalize a DOI for comparison (lowercase, strip URL prefix)."""
    if not doi:
        return ""
    d = doi.strip().lower()
    for pfx in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pfx):
            d = d[len(pfx):]
    return d


class OrcidClient:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or load_config()
        self.oc = cfg["orcid"]
        self.base_url = self.oc["base_url"].rstrip("/")
        self.session = requests.Session()
        self._token: str | None = None

    # -- auth ----------------------------------------------------------- #
    def token(self) -> str:
        if self._token:
            return self._token
        cid, secret = self.oc.get("client_id"), self.oc.get("client_secret")
        if not cid or not secret:
            raise SystemExit("orcid.client_id / client_secret missing in .env.yaml")
        resp = self.session.post(
            self.oc["token_url"],
            headers={"Accept": "application/json"},
            data={
                "client_id": cid,
                "client_secret": secret,
                "grant_type": "client_credentials",
                "scope": "/read-public",
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def _headers(self) -> dict:
        return {"Accept": "application/json", "Authorization": f"Bearer {self.token()}"}

    # -- reads ---------------------------------------------------------- #
    def get_record_summary(self, orcid: str) -> dict:
        """Name + employment/affiliation org names from the public record."""
        r = self.session.get(f"{self.base_url}/{orcid}/record",
                             headers=self._headers(), timeout=30)
        r.raise_for_status()
        rec = r.json()
        person = (rec.get("person") or {})
        name = (person.get("name") or {})
        given = ((name.get("given-names") or {}) or {}).get("value", "")
        family = ((name.get("family-name") or {}) or {}).get("value", "")
        orgs = []
        acts = (rec.get("activities-summary") or {})
        for section in ("employments", "educations"):
            groups = ((acts.get(section) or {}).get("affiliation-group")) or []
            for g in groups:
                for s in (g.get("summaries") or []):
                    summ = next(iter(s.values()))
                    org = (summ.get("organization") or {}).get("name")
                    if org:
                        orgs.append((section[:-1], org))
        return {"orcid": orcid, "given": given, "family": family, "orgs": orgs}

    def get_claimed_dois(self, orcid: str) -> list[str]:
        """Normalized DOIs the person claims on their own ORCID works list."""
        r = self.session.get(f"{self.base_url}/{orcid}/works",
                             headers=self._headers(), timeout=30)
        r.raise_for_status()
        dois: set[str] = set()
        for group in (r.json().get("group") or []):
            for eid in ((group.get("external-ids") or {}).get("external-id") or []):
                if (eid.get("external-id-type") or "").lower() == "doi":
                    d = canon_doi(eid.get("external-id-value") or "")
                    if d:
                        dois.add(d)
        return sorted(dois)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("orcid", help="ORCID iD, e.g. 0000-0002-2103-5494")
    ap.add_argument("--has-doi", help="check whether this DOI is claimed by the ORCID")
    args = ap.parse_args()

    client = OrcidClient()
    summary = client.get_record_summary(args.orcid)
    dois = client.get_claimed_dois(args.orcid)

    print(f"ORCID {args.orcid}: {summary['given']} {summary['family']}".rstrip())
    if summary["orgs"]:
        print("  affiliations (from ORCID record):")
        for kind, org in summary["orgs"]:
            print(f"    - [{kind}] {org}")
    print(f"  claims {len(dois)} works with a DOI")
    for d in dois[:40]:
        print(f"    {d}")
    if len(dois) > 40:
        print(f"    ... (+{len(dois) - 40} more)")

    if args.has_doi:
        target = canon_doi(args.has_doi)
        claimed = target in dois
        verdict = "CLAIMED by this ORCID" if claimed \
            else "NOT claimed by this ORCID  ->  likely OpenAlex mis-assignment"
        print(f"\n  DOI {target}: {verdict}")
        sys.exit(0 if claimed else 2)


if __name__ == "__main__":
    main()
