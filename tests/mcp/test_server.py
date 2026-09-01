"""Entrypoint refusal and instructions-block coherence tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from t0.mcp.db import Reader
from t0.mcp.instructions import INSTRUCTIONS
from t0.mcp.server import McpConfigError, create_app
from tests.mcp.conftest import TEST_MCP_DSN, TEST_TOKEN, build_settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = _REPO_ROOT / "migrations"

EXPECTED_TOOLS = (
    "get_pmc",
    "list_activities",
    "get_activity",
    "get_power_curve",
    "get_wellness",
    "get_coach_flags",
    "get_thresholds",
)


def test_create_app_refuses_without_token() -> None:
    settings = build_settings(mcp_bearer_token=None)

    with pytest.raises(McpConfigError, match="T0_MCP_BEARER_TOKEN"):
        create_app(settings, Reader(dsn=TEST_MCP_DSN, statement_timeout_ms=1_000))


def test_create_app_refuses_without_dsn() -> None:
    settings = build_settings(mcp_postgres_dsn=None)

    with pytest.raises(McpConfigError, match="T0_MCP_POSTGRES_DSN"):
        create_app(settings, Reader(dsn="", statement_timeout_ms=1_000))


def test_refusal_messages_never_contain_a_value() -> None:
    settings = build_settings(mcp_bearer_token=None, mcp_postgres_dsn=None)

    for _field in ("mcp_bearer_token", "mcp_postgres_dsn"):
        with pytest.raises(McpConfigError) as excinfo:
            create_app(settings, Reader(dsn="", statement_timeout_ms=1_000))
        assert TEST_TOKEN not in str(excinfo.value)
        assert TEST_MCP_DSN not in str(excinfo.value)


def test_instructions_name_every_tool_and_every_flag() -> None:
    for tool in EXPECTED_TOOLS:
        assert tool in INSTRUCTIONS, f"instructions never mention {tool}"
    for flag in (
        "ctl_reliable",
        "tss_method",
        "has_estimated_load",
        "data_quality",
        "is_parent",
        "tss_is_canonical",
        "css_estimated",
    ):
        assert flag in INSTRUCTIONS, f"instructions never mention {flag}"


def _normalized(text: str) -> str:
    """Collapse whitespace: both sides wrap lines freely, wording must not."""
    return " ".join(text.split())


@pytest.mark.parametrize(
    "migration,fragment",
    [
        (
            "003_core_ref.sql",
            "an FTP update would retroactively rewrite the entire CTL history",
        ),
        (
            "003_core_ref.sql",
            "an sTSS built on an estimated CSS does not have the same status",
        ),
        (
            "007_mart.sql",
            "is an initialization artifact, not training progression",
        ),
        (
            "007_mart.sql",
            "comes from HR not power changes its interpretation",
        ),
        (
            "004_core_activity.sql",
            "is_parent = false to avoid double-counting",
        ),
        (
            "018_baselines.sql",
            "the 28 days ending THE DAY BEFORE",
        ),
        (
            "019_signals.sql",
            "informs the go/no-go, never decides it",
        ),
        (
            "019_signals.sql",
            "aerobic-only or rest until two green mornings",
        ),
    ],
)
def test_instructions_recycle_the_migration_wording(migration: str, fragment: str) -> None:
    sql = _normalized((_MIGRATIONS / migration).read_text(encoding="utf-8"))
    instructions = _normalized(INSTRUCTIONS)
    quoted = _normalized(fragment)
    assert quoted in sql, f"{migration} no longer carries the quoted fragment"
    assert quoted in instructions


def test_instructions_state_the_dctl_bands_from_019() -> None:
    assert re.search(r"3-5 productive-but-watch", INSTRUCTIONS)
    assert re.search(r">5 caution, >8 red", INSTRUCTIONS)
