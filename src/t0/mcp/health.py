"""Liveness and readiness endpoints for the MCP Deployment probes.

Same discipline as :mod:`t0.api.health` (the ingest Deployment), because the
split is the whole point:

- /healthz (liveness): process alive, event loop answers, NOTHING else checked.
  A failing liveness probe restarts the container, and a restart can fix
  neither PostgreSQL nor the network -- a dependency outage must never
  translate into a restart loop.
- /readyz (readiness): the MCP is ready when its one dependency, the read-only
  PostgreSQL path, works. Failure answers 503: the Service stops routing to
  this pod, and readiness failures never restart anything.

No exception text is ever echoed in a response: connection errors carry host
and user names, and the DSN carries the database password (see t0.settings on
value leakage). A generic detail is all a probe consumer needs.
"""

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
    """Liveness: process alive. Deliberately dependency-free, see module docstring."""
    return JSONResponse({"status": "ok"})


def build_readyz(reader: Reader) -> Callable[[Request], Awaitable[JSONResponse]]:
    """Build the readiness endpoint bound to the MCP reader."""

    async def readyz(request: Request) -> JSONResponse:
        try:
            await asyncio.to_thread(reader.check)
        except QueryError:
            return JSONResponse({"status": "database unreachable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return readyz
