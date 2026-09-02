"""Prometheus metrics: HTTP counters and latency on a dedicated scrape port.

The registry is served outside the Bearer-protected ASGI app, so Prometheus
scrapes without a token; the port stays off the HTTPRoute, off the LAN.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from prometheus_client import Counter, Histogram, start_http_server

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUESTS = Counter(
    "t0_mcp_http_requests_total",
    "HTTP requests handled by the MCP server.",
    ["method", "path", "status"],
)
DURATION = Histogram(
    "t0_mcp_http_request_duration_seconds",
    "Request latency.",
    ["method", "path"],
)

_KNOWN_PATHS = frozenset({"/mcp", "/healthz", "/readyz"})


def _path_label(scope: Scope) -> str:
    """Cap label cardinality: anything unrecognized collapses to 'other'."""
    path = scope.get("path", "")
    return path if path in _KNOWN_PATHS else "other"


class MetricsMiddleware:
    """Count and time every request, including those rejected by inner auth."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], _path_label(scope)
        status = "500"
        started = time.monotonic()

        async def observe(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = str(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            REQUESTS.labels(method, path, status).inc()
            DURATION.labels(method, path).observe(time.monotonic() - started)


def start_metrics_server(port: int) -> None:
    """Serve the default registry on the dedicated port, blocking nothing."""
    start_http_server(port)
