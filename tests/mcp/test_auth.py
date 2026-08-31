"""Bearer middleware tests over the real assembled ASGI app."""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from tests.mcp.conftest import TEST_TOKEN


def _client(mcp_app: Any) -> TestClient:
    # The MCP protocol layer may raise on a bare POST /mcp: observe the
    # middleware, not the endpoint.
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
    response = _client(mcp_app).post(
        "/mcp", content=b"", headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    )

    assert response.status_code != 401


def test_401_never_echoes_the_token(mcp_app: Any) -> None:
    response = _client(mcp_app).post("/mcp", content=b"")

    assert TEST_TOKEN not in response.text
