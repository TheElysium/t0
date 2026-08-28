"""HTTP tests for t0.api.health: the /healthz and /readyz probes the chart's
readiness and liveness checks hit (deploy/helm/t0/templates/ingest-deployment.yaml).

The deployment crash-looped at first install because these routes did not exist:
both probes answered 404 until the liveness failureThreshold killed the pod. These
tests pin the contract the chart relies on.

Requires ``httpx`` (starlette's TestClient dependency), skipped cleanly without it,
same convention as tests/api/test_upload_endpoint.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

httpx = pytest.importorskip("httpx", reason="httpx missing from venv (see batch report)")

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from t0.api.app import create_app  # noqa: E402

if TYPE_CHECKING:
    from pathlib import Path


class _StubCursor:
    def execute(self, query: str, params: object = None) -> None:
        assert query == "SELECT 1"


class _StubConnection:
    def __init__(self, *, fail_execute: bool = False) -> None:
        self._fail_execute = fail_execute
        self.closed = False

    def execute(self, query: str, params: object = None) -> _StubCursor:
        if self._fail_execute:
            raise RuntimeError("boom")
        return _StubCursor()

    def close(self) -> None:
        self.closed = True


def _client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def _settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # readyz reads get_settings(): the five required variables, dummy values --
    # the connection itself is stubbed, nothing dials out. tmp_path keeps ruff's
    # S108 quiet; these paths are never touched.
    monkeypatch.setenv("T0_POSTGRES_DSN", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setenv("GARMINTOKENS", str(tmp_path / "garmin-tokens"))
    monkeypatch.setenv("T0_FIT_ARCHIVE_PATH", str(tmp_path / "fit-archive"))
    monkeypatch.setenv("T0_GARMIN_USERNAME", "athlete@example.com")
    monkeypatch.setenv("T0_GARMIN_PASSWORD", "not-a-real-password")


def test_healthz_is_dependency_free() -> None:
    # No settings, no database: liveness must answer even in a broken environment.
    r = _client().get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_ok_when_database_answers(
    _settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _StubConnection()
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: conn, raising=True)
    r = _client().get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert conn.closed, "readyz must not leak connections"


def test_readyz_503_when_connect_fails(
    _settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refuse(*a: object, **kw: object) -> _StubConnection:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(psycopg, "connect", _refuse, raising=True)
    r = _client().get("/readyz")
    assert r.status_code == 503
    # The exception text (host, user names) must never reach the response body.
    assert r.json()["detail"] == "database unreachable"


def test_readyz_503_and_closed_when_select_fails(
    _settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _StubConnection(fail_execute=True)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: conn, raising=True)
    r = _client().get("/readyz")
    assert r.status_code == 503
    assert r.json()["detail"] == "database unreachable"
    assert conn.closed, "close() runs even when the probe query fails"
