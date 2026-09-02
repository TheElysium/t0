"""Tests of the Prometheus metrics layer.

The middleware wraps the Bearer app outermost: rejected requests are counted
too, and unrecognized paths collapse to the 'other' label (cardinality cap).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from t0.mcp.metrics import MetricsMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

httpx = pytest.importorskip("httpx")
starlette_testclient = pytest.importorskip("starlette.testclient")


def test_metrics_count_statuses_and_cap_path_labels(mcp_app: Any) -> None:
    from prometheus_client import REGISTRY
    from starlette.testclient import TestClient

    client = TestClient(MetricsMiddleware(mcp_app))
    client.get("/healthz")
    client.post("/mcp")
    # Unrecognized paths reach the Bearer middleware first: 401, labeled 'other'.
    client.get("/untracked-path")

    assert (
        REGISTRY.get_sample_value(
            "t0_mcp_http_requests_total",
            {"method": "GET", "path": "/healthz", "status": "200"},
        )
        == 1.0
    )
    assert (
        REGISTRY.get_sample_value(
            "t0_mcp_http_requests_total",
            {"method": "POST", "path": "/mcp", "status": "401"},
        )
        == 1.0
    )
    assert (
        REGISTRY.get_sample_value(
            "t0_mcp_http_requests_total",
            {"method": "GET", "path": "other", "status": "401"},
        )
        == 1.0
    )


async def test_metrics_middleware_passes_non_http_scopes_through() -> None:
    recorded: list[str] = []

    async def app(
        scope: Any, receive: Callable[[], Awaitable[Any]], send: Callable[[Any], Awaitable[None]]
    ) -> None:
        recorded.append(scope["type"])

    async def no_receive() -> Any:
        return None

    async def no_send(message: Any) -> None:
        return None

    await MetricsMiddleware(app)({"type": "lifecycle"}, no_receive, no_send)

    assert recorded == ["lifecycle"]
