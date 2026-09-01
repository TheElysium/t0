"""Static Bearer token verification, as a plain ASGI middleware."""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

# Kubernetes probes carry no token and expose nothing sensitive.
UNPROTECTED_PATHS = frozenset({"/healthz", "/readyz"})


class BearerTokenMiddleware:
    """Reject every request whose ``Authorization`` header is not exactly
    ``Bearer <token>``; forward everything else untouched. Non-HTTP scopes
    (websocket, lifespan) are rejected outright: this app is HTTP-only, and
    letting them through would bypass the token check by construction."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if scope.get("path") not in UNPROTECTED_PATHS:
                headers = dict(scope["headers"])
                supplied = headers.get(b"authorization", b"")
                if not hmac.compare_digest(supplied, self._expected):
                    await JSONResponse({"detail": "unauthorized"}, status_code=401)(
                        scope, receive, send
                    )
                    return
        elif scope["type"] != "lifespan":
            await JSONResponse({"detail": "unsupported protocol"}, status_code=400)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)
