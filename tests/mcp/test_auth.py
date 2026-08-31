"""Bearer middleware tests over the real assembled ASGI app.

The probes (/healthz) are exempt; everything else -- including the MCP endpoint
itself -- requires exactly ``Bearer <token>``. The failure responses must not
echo the expected token. starlette TestClient (not a raw httpx.Client): the
ASGI transport is async-only in httpx, and TestClient manages the app lifespan.
"""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from tests.mcp.conftest import TEST_TOKEN


def _client(mcp_app: Any) -> TestClient:
    # raise_server_exceptions=False: a POST /mcp with an empty body hits the MCP
    # protocol layer, which may raise before responding -- here we only observe
    # what crossed the Bearer middleware.
    return TestClient(mcp_app, raise_server_exceptions=False)


def test_healthz_is_reachable_without_a_token(mcp_app: Any) -> None:
    response = _client(mcp_app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mcp_endpoint_without_token_is_401(mcp_app: Any) -> None:
    response = _client(mcp_app).post("/mcp", content=b"")

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_wrong_token_is_401(mcp_app: Any) -> None:
    response = _client(mcp_app).post(
        "/mcp", content=b"", headers={"Authorization": "Bearer not-the-token"}
    )

    assert response.status_code == 401


def test_token_is_case_sensitive(mcp_app: Any) -> None:
    response = _client(mcp_app).post(
        "/mcp", content=b"", headers={"Authorization": f"Bearer {TEST_TOKEN.upper()}"}
    )

    assert response.status_code == 401


def test_correct_token_passes_the_middleware(mcp_app: Any) -> None:
    """The middleware forwards: any status other than 401 is its pass-through
    signature (the endpoint itself answers on the protocol layer)."""
    response = _client(mcp_app).post(
        "/mcp", content=b"", headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    )

    assert response.status_code != 401


def test_401_never_echoes_the_token(mcp_app: Any) -> None:
    response = _client(mcp_app).post("/mcp", content=b"")

    assert TEST_TOKEN not in response.text
