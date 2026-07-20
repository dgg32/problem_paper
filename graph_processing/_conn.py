#!/usr/bin/env python3
"""_conn.py — the one shared Neo4j connection resolver for the whole repo.

Every script that talks to the graph uses `resolve_connection()`. It lived in
`normalize_authors.py` for historical reasons (an orphaned identity script — see
that file), which coupled ~26 modules to an unrelated-named helper. This is its
canonical home; new code should `from _conn import resolve_connection`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def resolve_connection() -> dict:
    """Prefer env vars; fall back to the neo4j server block in .mcp.json."""
    cfg = {
        "uri": os.getenv("NEO4J_URI"),
        "user": os.getenv("NEO4J_USERNAME"),
        "password": os.getenv("NEO4J_PASSWORD"),
        "database": os.getenv("NEO4J_DATABASE"),
    }
    if not all([cfg["uri"], cfg["user"], cfg["password"]]):
        mcp = ROOT / ".mcp.json"
        if mcp.exists():
            env = (
                json.loads(mcp.read_text())
                .get("mcpServers", {})
                .get("neo4j", {})
                .get("env", {})
            )
            cfg["uri"] = cfg["uri"] or env.get("NEO4J_URI")
            cfg["user"] = cfg["user"] or env.get("NEO4J_USERNAME")
            cfg["password"] = cfg["password"] or env.get("NEO4J_PASSWORD")
            cfg["database"] = cfg["database"] or env.get("NEO4J_DATABASE")
    cfg["database"] = cfg["database"] or "neo4j"
    missing = [k for k in ("uri", "user", "password") if not cfg[k]]
    if missing:
        raise SystemExit(f"Missing Neo4j connection settings: {', '.join(missing)}")
    return cfg
