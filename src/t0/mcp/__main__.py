"""Entrypoint: ``python -m t0.mcp``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import uvicorn

from t0.logging_setup import configure_logging
from t0.mcp.db import Reader
from t0.mcp.server import BIND_HOST, create_app
from t0.settings import get_mcp_settings

if TYPE_CHECKING:
    from pydantic import SecretStr

    from t0.settings import McpSettings

logger = logging.getLogger(__name__)

_UNCONFIGURED_MESSAGE = (
    "MCP server refuses to start: T0_MCP_BEARER_TOKEN and T0_MCP_POSTGRES_DSN "
    "are both required (values intentionally omitted from this message)."
)


def _require_mcp_settings(settings: McpSettings) -> tuple[SecretStr, SecretStr]:
    """Refuse to start without the token or the read-only DSN; names only."""
    token = settings.mcp_bearer_token
    dsn = settings.mcp_postgres_dsn
    if token is None or dsn is None:
        raise RuntimeError(_UNCONFIGURED_MESSAGE)
    return token, dsn


def main() -> int:
    """Build and serve the MCP app. Returns a process exit code."""
    configure_logging()
    # MCP scope, not the full Settings: this pod holds no ingest credentials
    # by design, so only the T0_MCP_* variables are validated here.
    settings = get_mcp_settings()
    try:
        _token, dsn = _require_mcp_settings(settings)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1
    reader = Reader(
        dsn=dsn.get_secret_value(),
        statement_timeout_ms=settings.mcp_statement_timeout_ms,
    )
    app = create_app(settings, reader)
    logger.info("MCP server starting on port %d (7 read-only tools).", settings.mcp_port)
    uvicorn.run(app, host=BIND_HOST, port=settings.mcp_port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
