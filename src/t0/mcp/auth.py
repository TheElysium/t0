"""Static Bearer token verification, as a plain ASGI middleware.

HANDOFF-MCP §2: no OAuth 2.1 -- disproportionate for single-user LAN use behind
the shared Gateway, and the real client (Claude Code CLI) accepts a static
header. The token lives in a K8s Secret, is compared constant-time, and is
NEVER logged: the 401 response and the exception messages carry no echo of what
was expected or received.

``/healthz`` and ``/readyz`` are exempt: they are consumed by the Kubernetes
probes, which carry no token, and they expose nothing but "ok"/"database
unreachable".
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

UNPROTECTED_PATHS = frozenset({"/healthz", "/readyz"})


class BearerTokenMiddleware:
    """Reject every request whose ``Authorization`` header is not exactly
    ``Bearer <token>``; forward everything else untouched."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") not in UNPROTECTED_PATHS:
            headers = dict(scope["headers"])
            supplied = headers.get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self._expected):
                await JSONResponse({"detail": "unauthorized"}, status_code=401)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)
