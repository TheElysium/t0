"""HTTP tests for garminator.api.upload: the manual upload route, via FastAPI's test
client (no real server starts).

Requires ``python-multipart`` (so FastAPI can even DEFINE the route — see
garminator/api/upload.py) and ``httpx`` (starlette's TestClient dependency). Neither is
installed in this repository's venv at batch write time: this entire module is skipped
cleanly (``pytest.importorskip``) rather than failing — see batch end report for
dependencies to add.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("multipart", reason="python-multipart missing from venv (see batch report)")
httpx = pytest.importorskip("httpx", reason="httpx missing from venv (see batch report)")

from fastapi.testclient import TestClient  # noqa: E402

from garminator.api.app import create_app  # noqa: E402
from garminator.api.upload import get_upload_db_conn, get_upload_dest_dir  # noqa: E402
from tests.api.conftest import FakeRawStoreConnection  # noqa: E402

if TYPE_CHECKING:
    from pathlib import Path

_VALID_TCX = b"""<?xml version="1.0"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities>
    <Activity Sport="Biking">
      <Lap StartTime="2026-08-01T06:00:00Z">
        <Track>
          <Trackpoint>
            <Time>2026-08-01T06:00:00Z</Time>
            <Extensions>
              <TPX xmlns="http://www.garmin.com/xmlschemas/ActivityExtension/v2">
                <Watts>100</Watts>
                <Cadence>75</Cadence>
              </TPX>
            </Extensions>
          </Trackpoint>
        </Track>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""

# Same inert fixture as tests/api/test_ingest.py: FIT signature recognizable but not
# decodable.
_FAKE_FIT = b"\x0e\x10\xc0\x00\x00\x00\x00\x00.FIT" + b"\x00" * 20


def _client(tmp_path: Path, conn: FakeRawStoreConnection | None = None) -> TestClient:
    app = create_app()
    # Dependency override: archive directory comes from tmp_path, and database connection
    # from an in-memory stub — never real settings (no Garmin/PostgreSQL environment
    # variables required for these tests, and no PostgreSQL exists in this environment
    # anyway).
    app.dependency_overrides[get_upload_dest_dir] = lambda: tmp_path
    app.dependency_overrides[get_upload_db_conn] = lambda: conn or FakeRawStoreConnection()
    return TestClient(app)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_upload_valid_tcx_returns_201_with_ok_status(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/uploads", files={"file": ("activity.tcx", _VALID_TCX, "application/octet-stream")}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ok"
    assert body["source_format"] == "tcx"
    assert body["parsed"]["n_samples"] == 1
    assert body["parse_error"] is None
    assert isinstance(body["fit_file_id"], int)
    # Proof that archiving actually happened on the overridden disk volume.
    assert list(tmp_path.iterdir()) != []


def test_upload_zip_wrapped_tcx_is_sniffed_by_content_not_by_extension(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    zip_data = _zip_bytes({"whatever.bin": _VALID_TCX})

    # Announced filename ("archive.zip") AND internal member name ("whatever.bin") are
    # both misleading: only content should determine detected format.
    response = client.post("/uploads", files={"file": ("archive.zip", zip_data, "application/zip")})

    assert response.status_code == 201
    assert response.json()["source_format"] == "tcx"


def test_upload_fit_shaped_but_undecodable_returns_422_but_reports_archiving(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/uploads", files={"file": ("activity.fit", _FAKE_FIT, "application/octet-stream")}
    )

    # Counterproof of "no false success": a 201 here would be wrong (parsing really
    # failed) — status MUST be 422, not 201, despite successful archiving.
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["status"] == "archived_but_unparsed"
    assert detail["source_format"] == "fit"
    assert detail["parse_error"] is not None
    # Counterproof of "no silent loss": raw file is indeed on disk despite parse failure.
    assert list(tmp_path.iterdir()) != []


def test_upload_unrecognized_content_returns_422_with_no_side_effect(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/uploads",
        files={"file": ("mystery.bin", b"neither FIT nor TCX nor ZIP", "application/octet-stream")},
    )

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_upload_empty_zip_is_rejected_explicitly(tmp_path: Path) -> None:
    client = _client(tmp_path)
    zip_data = _zip_bytes({})

    response = client.post("/uploads", files={"file": ("empty.zip", zip_data, "application/zip")})

    assert response.status_code == 422
    assert "Empty ZIP" in response.json()["detail"]


def test_upload_multi_member_zip_is_rejected_explicitly(tmp_path: Path) -> None:
    client = _client(tmp_path)
    zip_data = _zip_bytes({"a.tcx": _VALID_TCX, "b.tcx": _VALID_TCX})

    response = client.post("/uploads", files={"file": ("multi.zip", zip_data, "application/zip")})

    assert response.status_code == 422
    assert "files" in response.json()["detail"]


def test_upload_zip_with_path_escaping_member_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    zip_data = _zip_bytes({"../evil.tcx": _VALID_TCX})

    response = client.post("/uploads", files={"file": ("evil.zip", zip_data, "application/zip")})

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_upload_oversized_payload_returns_413(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Actually sending 200 MiB (default bound) would be expensive for just this test:
    # instead tighten the bound read by the route (global name looked up at call, not
    # captured at definition) rather than enlarging the sent body.
    import garminator.api.upload as upload_module

    monkeypatch.setattr(upload_module, "DEFAULT_MAX_UPLOAD_BYTES", 8)
    client = _client(tmp_path)

    response = client.post(
        "/uploads", files={"file": ("activity.tcx", _VALID_TCX, "application/octet-stream")}
    )

    assert response.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_upload_returns_409_when_content_already_belongs_to_another_activity(
    tmp_path: Path,
) -> None:
    conn = FakeRawStoreConnection()
    conn.preload(sha256=hashlib.sha256(_VALID_TCX).digest(), garmin_activity_id=9001)
    client = _client(tmp_path, conn=conn)

    response = client.post(
        "/uploads", files={"file": ("activity.tcx", _VALID_TCX, "application/octet-stream")}
    )

    assert response.status_code == 409
    # Archive-first: file stays archived despite insert conflict, no loss.
    assert list(tmp_path.iterdir()) != []


def test_route_annotations_resolve_at_runtime() -> None:
    """Every route and dependency annotation must be importable at runtime.

    A name left under ``TYPE_CHECKING`` does not make FastAPI fail loudly: get_type_hints()
    raises NameError, FastAPI falls back to treating the parameter as an untyped query
    parameter, and every request answers 422 "Field required" for something that was never
    meant to come from the query string. Shipping ``DBConnection`` under TYPE_CHECKING did
    exactly that -- the module imported cleanly and the endpoint was dead.
    """
    import typing

    from garminator.api import upload as upload_module

    for func in (
        upload_module.upload_activity_file,
        upload_module.get_upload_db_conn,
        upload_module.get_upload_dest_dir,
    ):
        typing.get_type_hints(func, include_extras=True)
