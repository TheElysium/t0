"""Protocol smoke test over the real transport stack: 7 tools, one round-trip."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from tests.mcp.conftest import TEST_TOKEN, FakeRunner

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

EXPECTED_TOOLS = {
    "get_pmc",
    "list_activities",
    "get_activity",
    "get_power_curve",
    "get_wellness",
    "get_ramp_risk",
    "get_coach_flags",
    "get_thresholds",
}


@asynccontextmanager
async def _mcp_client(mcp_app: Any, token: str | None) -> AsyncIterator[ClientSession]:
    """Open an SDK client session against the ASGI app; the app lifespan runs
    manually (ASGI transport does not run it)."""
    request_headers = {"Authorization": f"Bearer {token}"} if token else {}
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app),
        base_url="http://testserver",
        headers=request_headers,
    )
    async with (
        mcp_app.app.router.lifespan_context(mcp_app.app),
        http_client,
        streamable_http_client("http://testserver/mcp", http_client=http_client) as (
            read_stream,
            write_stream,
            _get_session_id,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


@pytest.mark.anyio
async def test_server_lists_exactly_the_eight_tools(mcp_app: Any) -> None:
    async with _mcp_client(mcp_app, TEST_TOKEN) as session:
        result = await session.list_tools()

    assert {tool.name for tool in result.tools} == EXPECTED_TOOLS


@pytest.mark.anyio
async def test_tool_round_trip_over_the_transport(mcp_app: Any, fake_runner: FakeRunner) -> None:
    fake_runner.queue(
        "FROM CORE.THRESHOLD",
        [
            {
                "threshold_id": 1,
                "discipline": "bike",
                "valid_from": "2026-03-01",
                "valid_to": "infinity",
                "ftp_watts": 245.0,
                "rftp_watts": None,
                "ftp_pace_s_per_km": None,
                "css_m_s": None,
                "lthr_bpm": 152,
                "hr_max_bpm": 188,
                "hr_rest_bpm": 44,
                "weight_kg": 71.2,
                "power_source": "powermeter_4iiii",
                "source": "cp_model",
                "css_estimated": False,
                "note": None,
            }
        ],
    )

    async with _mcp_client(mcp_app, TEST_TOKEN) as session:
        result = await session.call_tool("get_thresholds", {"discipline": "bike"})

    assert not result.isError
    block = result.content[0]
    assert isinstance(block, TextContent)
    # FastMCP serializes each list element into its own JSON text block;
    # exactly one row was queued, so the first block is that row's JSON object.
    payload = json.loads(block.text)
    assert payload["ftp_watts"] == 245.0
    assert payload["css_estimated"] is False
    assert payload["valid_to"] == "infinity"


def _all_messages(exc: BaseException) -> str:
    """Flatten an ExceptionGroup tree (anyio task groups) into one string."""
    if isinstance(exc, BaseExceptionGroup):
        return " ".join(_all_messages(sub) for sub in exc.exceptions)
    return str(exc)


@pytest.mark.anyio
async def test_unauthenticated_protocol_client_is_rejected(mcp_app: Any) -> None:
    with pytest.raises(BaseException) as excinfo:
        async with _mcp_client(mcp_app, None):
            pass

    assert "401" in _all_messages(excinfo.value)
