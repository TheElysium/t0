"""Shared fixtures for tests/mcp/.

No database in this package unless one is explicitly provided (see
test_db_reader.py, which skips without ``T0_TEST_POSTGRES_DSN`` like every
other DB test of the repo): handlers take a :class:`~t0.mcp.db.SqlRunner`
Protocol, so the pure tests and the protocol smoke test inject a fake runner,
and the Reader is stubbed at instance level through ``monkeypatch`` (its pool
therefore never opens).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import SecretStr

from t0.mcp.db import Reader
from t0.mcp.server import create_app
from t0.settings import Settings

TEST_TOKEN = "mcp-test-token"
TEST_MCP_DSN = "postgresql://t0_mcp_ro:mcp-canary-password@db.invalid:5432/t0"
TEST_DSN_PASSWORD = "mcp-canary-password"


def build_settings(**overrides: Any) -> Settings:
    """Full Settings with MCP fields set, bypassing the environment."""
    values: dict[str, Any] = {
        "postgres_dsn": SecretStr("postgresql://user:pw@localhost:5432/db"),
        # validation_alias of garmin_tokens_path: pydantic-settings matches the
        # alias, not the field name, when populating from a dict.
        "GARMINTOKENS": "/opt/t0-test/garmin-tokens",
        "fit_archive_path": "/opt/t0-test/fit-archive",
        "garmin_username": "athlete@example.invalid",
        "garmin_password": "pw",
        "mcp_bearer_token": SecretStr(TEST_TOKEN),
        "mcp_postgres_dsn": SecretStr(TEST_MCP_DSN),
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


@dataclass
class FakeRunner:
    """In-memory :class:`SqlRunner`: rows are matched to queries by a marker
    substring, calls are recorded. A query nobody queued fails the test loudly
    rather than silently returning nothing."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def queue(self, marker: str, rows: list[dict[str, Any]]) -> None:
        self._rows[marker] = rows

    def _match(self, sql: str) -> str:
        normalized = " ".join(sql.split()).upper()
        for marker in self._rows:
            if marker in normalized:
                return marker
        raise AssertionError(f"SQL query not queued in this test: {sql!r}")

    def fetch(self, sql: str, params: Any) -> list[dict[str, Any]]:
        self.calls.append((" ".join(sql.split()), dict(params)))
        return [dict(row) for row in self._rows[self._match(sql)]]

    def fetch_one(self, sql: str, params: Any) -> dict[str, Any] | None:
        rows = self.fetch(sql, params)
        return rows[0] if rows else None


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def stub_reader(fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> Reader:
    """A Reader whose queries are answered by ``fake_runner``: the pool never
    opens, while the server code exercises the exact call path (to_thread
    included) it uses in production."""
    reader = Reader(dsn=TEST_MCP_DSN, statement_timeout_ms=1_000)
    monkeypatch.setattr(reader, "fetch", fake_runner.fetch)
    monkeypatch.setattr(reader, "fetch_one", fake_runner.fetch_one)
    monkeypatch.setattr(reader, "check", lambda: None)
    return reader


@pytest.fixture
def mcp_app(stub_reader: Reader) -> Any:
    """The assembled ASGI app (Bearer middleware wrapping the Starlette app)."""
    return create_app(build_settings(), stub_reader)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
