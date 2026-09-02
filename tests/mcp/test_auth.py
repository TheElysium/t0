"""Bearer middleware tests over the real assembled ASGI app."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse

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


def test_websocket_scope_is_rejected_even_with_a_token(mcp_app: Any) -> None:
    # The middleware answers the handshake with an HTTP 400 denial response.
    with (
        pytest.raises(WebSocketDenialResponse) as excinfo,
        _client(mcp_app).websocket_connect(
            "/mcp", headers={"Authorization": f"Bearer {TEST_TOKEN}"}
        ),
    ):
        pass

    assert excinfo.value.status_code == 400
    assert b"unsupported protocol" in excinfo.value.content
