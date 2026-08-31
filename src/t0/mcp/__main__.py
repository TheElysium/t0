"""Entrypoint: ``python -m t0.mcp``.

Refuses to start without the Bearer token or the read-only DSN (same refusal
is re-checked in :func:`t0.mcp.server.create_app`): an MCP server that cannot
authenticate its clients or cannot say where it reads from must fail loudly,
not degrade. The same Settings class is shared with ingest/scheduler, whose
environments have neither variable -- hence every MCP field is optional and
the refusal lives here, at the MCP entrypoint, not in Settings.

Startup log cites port and tool count, never the token nor the DSN: both live
in K8s Secrets and must never reach the container logs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import uvicorn

from t0.logging_setup import configure_logging
from t0.mcp.db import Reader
from t0.mcp.server import BIND_HOST, create_app
from t0.settings import get_settings

if TYPE_CHECKING:
    from pydantic import SecretStr

    from t0.settings import Settings

logger = logging.getLogger(__name__)

_UNCONFIGURED_MESSAGE = (
    "MCP server refuses to start: T0_MCP_BEARER_TOKEN and T0_MCP_POSTGRES_DSN "
    "are both required (values intentionally omitted from this message)."
)


def _require_mcp_settings(settings: Settings) -> tuple[SecretStr, SecretStr]:
    """Refuse to start without the token or the read-only DSN; names only."""
    token = settings.mcp_bearer_token
    dsn = settings.mcp_postgres_dsn
    if token is None or dsn is None:
        raise RuntimeError(_UNCONFIGURED_MESSAGE)
    return token, dsn


def main() -> int:
    """Build and serve the MCP app. Returns a process exit code."""
    configure_logging()
    settings = get_settings()
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
