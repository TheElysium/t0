"""Liveness and readiness endpoints for the MCP Deployment probes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from t0.mcp.db import QueryError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request

    from t0.mcp.db import Reader


async def healthz(request: Request) -> JSONResponse:
    """Liveness only -- a dependency outage must never cause a restart."""
    return JSONResponse({"status": "ok"})


def build_readyz(reader: Reader) -> Callable[[Request], Awaitable[JSONResponse]]:
    """Build the readiness endpoint bound to the MCP reader."""

    async def readyz(request: Request) -> JSONResponse:
        try:
            await asyncio.to_thread(reader.check)
        except QueryError:
            # No dependency named: /readyz is unauthenticated (kubelet probes).
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return readyz
