"""HTTP tests for garminator.api.rpe: the manual RPE entry route, via FastAPI's test
client (no real server starts, no PostgreSQL — same pattern as
``tests/api/test_upload_endpoint.py``, with ``FakeCoreConnection`` standing in for the
``core.*`` side).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("multipart", reason="python-multipart missing from venv (see batch report)")
httpx = pytest.importorskip("httpx", reason="httpx missing from venv (see batch report)")

from fastapi.testclient import TestClient  # noqa: E402

from garminator.api.app import create_app  # noqa: E402
from garminator.api.rpe import RPE_METHOD_CODE  # noqa: E402
from garminator.api.upload import get_upload_db_conn  # noqa: E402
from tests.api.conftest import FakeCoreConnection  # noqa: E402

if TYPE_CHECKING:
    from typing import Any

ACTIVITY_ID = 9000000000000000001


def _client(conn: FakeCoreConnection) -> TestClient:
    """Dependency override: database connection comes from an in-memory stub — never
    real settings (no Garmin/PostgreSQL environment variables required for these tests,
    and no PostgreSQL exists in this environment anyway)."""
    app = create_app()
    app.dependency_overrides[get_upload_db_conn] = lambda: conn
    return TestClient(app)


def _post_rpe(
    client: TestClient,
    activity_id: int,
    rpe: int | str,
    *,
    force: bool | None = None,
) -> Any:
    # `rpe` accepts invalid types on purpose (validation tests): JSON body stays loose.
    body: dict[str, Any] = {"rpe": rpe}
    if force is not None:
        body["force"] = force
    return client.post(f"/activities/{activity_id}/rpe", json=body)


def _pending_activity_conn() -> FakeCoreConnection:
    """Stub preloaded with an activity flagged ``needs_manual_rpe``."""
    conn = FakeCoreConnection()
    conn.add_activity(ACTIVITY_ID, needs_manual_rpe=True)
    return conn


# -- happy path ------------------------------------------------------------------------


def test_happy_path_writes_canonical_rpe_row_and_flips_the_flag() -> None:
    conn = _pending_activity_conn()

    response = _post_rpe(_client(conn), ACTIVITY_ID, 7)

    assert response.status_code == 200
    assert response.json() == {
        "activity_id": ACTIVITY_ID,
        "tss": 7.0,
        "method_code": RPE_METHOD_CODE,
        "needs_manual_rpe": False,
    }
    row = conn.metrics[(ACTIVITY_ID, "tss", RPE_METHOD_CODE)]
    assert row.value == 7.0
    assert row.is_canonical is True
    # The backlog query must stop listing this activity: flag flipped in the SAME
    # transaction as the metric write.
    assert conn.activities[ACTIVITY_ID] is False
    assert conn.commits >= 1


def test_happy_path_on_activity_not_flagged_for_rpe_still_succeeds() -> None:
    """An activity whose canonical TSS is absent but that was never flagged (e.g.
    transformed before any flag existed) is a legitimate target too."""
    conn = FakeCoreConnection()
    conn.add_activity(ACTIVITY_ID, needs_manual_rpe=False)

    response = _post_rpe(_client(conn), ACTIVITY_ID, 5)

    assert response.status_code == 200
    assert conn.metrics[(ACTIVITY_ID, "tss", RPE_METHOD_CODE)].is_canonical is True


# -- unknown activity --------------------------------------------------------------------


def test_unknown_activity_returns_404_and_writes_nothing() -> None:
    conn = FakeCoreConnection()  # no activity preloaded at all

    response = _post_rpe(_client(conn), ACTIVITY_ID, 7)

    assert response.status_code == 404
    assert conn.metrics == {}
    assert conn.commits == 0


# -- measured canonical TSS present ------------------------------------------------------


def test_refuses_without_force_when_measured_tss_is_canonical() -> None:
    conn = FakeCoreConnection()
    conn.add_activity(ACTIVITY_ID, needs_manual_rpe=False)
    conn.set_canonical_tss(ACTIVITY_ID, "bike_power", 312.5)

    response = _post_rpe(_client(conn), ACTIVITY_ID, 6)

    assert response.status_code == 409
    assert "bike_power" in response.json()["detail"]
    # Refusal is total: nothing written, nothing demoted, no commit.
    assert conn.metrics[(ACTIVITY_ID, "tss", "bike_power")].is_canonical is True
    assert (ACTIVITY_ID, "tss", RPE_METHOD_CODE) not in conn.metrics
    assert conn.commits == 0


def test_force_demotes_measured_row_but_never_deletes_it() -> None:
    conn = FakeCoreConnection()
    conn.add_activity(ACTIVITY_ID, needs_manual_rpe=False)
    conn.set_canonical_tss(ACTIVITY_ID, "bike_power", 312.5)

    response = _post_rpe(_client(conn), ACTIVITY_ID, 6, force=True)

    assert response.status_code == 200
    assert response.json()["needs_manual_rpe"] is False
    # Measured value kept comparable, just no longer authoritative.
    measured = conn.metrics[(ACTIVITY_ID, "tss", "bike_power")]
    assert measured.value == 312.5
    assert measured.is_canonical is False
    assert conn.metrics[(ACTIVITY_ID, "tss", RPE_METHOD_CODE)].is_canonical is True


def test_force_on_already_rpe_canonical_is_a_plain_overwrite() -> None:
    """Canonical method already 'rpe': no conflict, no demotion needed."""
    conn = _pending_activity_conn()
    client = _client(conn)

    first = _post_rpe(client, ACTIVITY_ID, 8)
    second = _post_rpe(client, ACTIVITY_ID, 4, force=True)

    assert first.status_code == 200
    assert second.status_code == 200
    assert conn.metrics[(ACTIVITY_ID, "tss", RPE_METHOD_CODE)].value == 4.0


# -- validation --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_rpe", [0, -3, 11])
def test_rpe_outside_1_10_returns_422(bad_rpe: int) -> None:
    conn = _pending_activity_conn()

    response = _post_rpe(_client(conn), ACTIVITY_ID, bad_rpe)

    assert response.status_code == 422
    assert conn.metrics == {}


def test_non_integer_rpe_returns_422() -> None:
    conn = _pending_activity_conn()

    response = _post_rpe(_client(conn), ACTIVITY_ID, "hard")

    assert response.status_code == 422
    assert conn.metrics == {}


def test_missing_rpe_field_returns_422() -> None:
    conn = _pending_activity_conn()

    response = _client(conn).post(f"/activities/{ACTIVITY_ID}/rpe", json={})

    assert response.status_code == 422
    assert conn.metrics == {}


def test_non_integer_activity_id_returns_422() -> None:
    conn = _pending_activity_conn()

    response = _client(conn).post("/activities/not-an-id/rpe", json={"rpe": 7})

    assert response.status_code == 422
    assert conn.executed == []


# -- idempotent overwrite -----------------------------------------------------------------


def test_reposting_replaces_previous_rpe_entry() -> None:
    conn = _pending_activity_conn()
    client = _client(conn)

    first = _post_rpe(client, ACTIVITY_ID, 5)
    second = _post_rpe(client, ACTIVITY_ID, 8)

    assert first.status_code == 200
    assert second.status_code == 200
    rows = [row for key, row in conn.metrics.items() if key[0] == ACTIVITY_ID]
    # Exactly ONE tss row remains, carrying the latest value: overwrite, never duplicate.
    assert len(rows) == 1
    assert rows[0].method_code == RPE_METHOD_CODE
    assert rows[0].value == 8.0
    assert rows[0].is_canonical is True


@pytest.mark.parametrize(("low", "high"), [(1, 10)])
def test_boundary_values_are_accepted(low: int, high: int) -> None:
    conn = _pending_activity_conn()
    client = _client(conn)

    first = _post_rpe(client, ACTIVITY_ID, low)
    second = _post_rpe(client, ACTIVITY_ID, high)

    assert first.status_code == 200
    assert second.status_code == 200
    assert conn.metrics[(ACTIVITY_ID, "tss", RPE_METHOD_CODE)].value == float(high)


# -- route wiring -------------------------------------------------------------------------


def test_route_annotations_resolve_at_runtime() -> None:
    """Every route and dependency annotation must be importable at runtime.

    Same guard as ``tests/api/test_upload_endpoint.py``: a name left under
    ``TYPE_CHECKING`` makes FastAPI silently treat the parameter as an untyped query
    parameter, and every request answers 422 "Field required" for something that was
    never meant to come from the query string.
    """
    import typing

    from garminator.api import rpe as rpe_module

    for func in (
        rpe_module.enter_manual_rpe,
        rpe_module.record_manual_rpe,
        rpe_module._load_needs_manual_rpe,
        rpe_module._canonical_tss_method_code,
    ):
        typing.get_type_hints(func, include_extras=True)


def test_app_mounts_the_rpe_router_under_activities_prefix() -> None:
    from fastapi.routing import APIRoute

    paths = {route.path for route in create_app().routes if isinstance(route, APIRoute)}
    assert "/activities/{activity_id}/rpe" in paths
