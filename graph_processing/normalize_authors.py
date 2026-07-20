#!/usr/bin/env python3
"""
normalize_authors.py — DEPRECATED shim (2026-07-19).

This module used to generate a candidate author-identity layer by writing
``:SAME_AS`` edges between ``:Author`` nodes. That layer is **orphaned and was
removed**: the live identity layer is `link_instances.py`, which writes
``:PROBABLY_SAME_AS`` edges between ``:AuthorInstance`` nodes — the type the rest
of the system actually reads (`cluster_instances.py`, `tier_a_scoring.py`,
`import_cypher.txt`, plan.md §2.1b). The graph has 0 ``:Author`` nodes and 0
``:SAME_AS`` edges (verified), so the old script did nothing useful and, worse,
running it could have created a second divergent identity layer — exactly the
mis-attribution hazard plan.md §0 warns against.

The one thing everything imported from here was ``resolve_connection``; it now
lives in `graph_processing/_conn.py` and is re-exported below for the ~26
existing importers. New code should import from `_conn` directly.
"""
from _conn import resolve_connection  # noqa: F401  (re-exported for back-compat)

__all__ = ["resolve_connection"]
