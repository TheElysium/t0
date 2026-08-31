"""MCP server: read-only training data access for an LLM (docs/HANDOFF-MCP.md).

A separate process by design: routing this via the Gateway must never, by
construction, route the ingest upload endpoint (unauthenticated, ClusterIP-only).
Seven curated tools over the marts, no raw SQL, no write, static Bearer token.

Import the submodule you need: ``t0.mcp.server`` (ASGI app factory),
``t0.mcp.db`` (read-only connection pool), ``t0.mcp.handlers`` (pure
query->shape->round functions). Run with ``python -m t0.mcp``.
"""

from __future__ import annotations
