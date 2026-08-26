"""Tests for t0.api.ingest: pure business logic of L7 batch.

Depend on neither FastAPI, python-multipart, nor httpx (neither of the last two is
installed in this repository's venv — see batch end report): precisely why business
logic lives in a module separate from the HTTP layer (t0.api.upload). All
fixtures are synthetic bytes constructed here, never derived from athlete's real files.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import TYPE_CHECKING

import pytest

from t0.api.errors import HostileArchiveError, PayloadTooLargeError
from t0.api.ingest import (
    DEFAULT_MAX_COMPRESSION_RATIO,
    UploadOutcome,
    check_zip_safety,
    ingest_upload,
)
from t0.fit.errors import ArchiveError
from t0.garmin.raw_store import FitFileShaConflictError, insert_fit_file

if TYPE_CHECKING:
    from pathlib import Path

    from tests.api.conftest import FakeRawStoreConnection

# Same signature bytes as tests/fit/test_archive.py: FIT structure recognizable by
# sniff_format (signature ".FIT" at correct offset) but NOT actually decodable FIT
# stream (no valid message definition/CRC) — ideal for testing "archive succeeded,
# parsing failed".
_FAKE_FIT = b"\x0e\x10\xc0\x00\x00\x00\x00\x00.FIT" + b"\x00" * 20

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

# TCX whose XML content is syntactically valid and sniffs as TCX, but no Trackpoint
# has usable Time: decode_tcx must raise TcxParseError (cf. tests/tcx/
# test_reader_hardening.py for equivalent at reader level).
_CORRUPT_TCX = b"""<?xml version="1.0"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities>
    <Activity Sport="Running">
      <Lap StartTime="2026-08-01T06:00:00Z">
        <Track>
          <Trackpoint>
            <HeartRateBpm><Value>150</Value></HeartRateBpm>
          </Trackpoint>
        </Track>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


# --- Happy path: valid TCX, direct and wrapped in ZIP ---


def test_ingest_upload_valid_tcx_produces_ok_summary(tmp_path: Path) -> None:
    outcome = ingest_upload(_VALID_TCX, tmp_path)

    assert isinstance(outcome, UploadOutcome)
    assert outcome.parse_error is None
    assert outcome.parsed is not None
    assert outcome.parsed.n_samples == 1
    assert outcome.parsed.has_session is False  # TCX never has session
    assert outcome.archived.source_format == "tcx"
    assert outcome.archived.storage_path.exists()
    assert outcome.archived.sha256 == hashlib.sha256(_VALID_TCX).digest()


def test_ingest_upload_valid_tcx_wrapped_in_zip(tmp_path: Path) -> None:
    zip_data = _zip_bytes({"activity.tcx": _VALID_TCX})

    outcome = ingest_upload(zip_data, tmp_path)

    assert outcome.parse_error is None
    assert outcome.parsed is not None
    assert outcome.parsed.n_samples == 1
    assert outcome.archived.source_format == "tcx"


def test_ingest_upload_uses_content_addressed_filename_not_client_supplied_name(
    tmp_path: Path,
) -> None:
    # Falsifiable proof: two calls with same content produce same storage path, even
    # though no filename is ever passed to ingest_upload — if naming depended on an
    # external name, this test could not verify it (there is no external name here), and
    # a random-naming implementation (uuid per call) would fail it.
    first = ingest_upload(_VALID_TCX, tmp_path)
    second = ingest_upload(_VALID_TCX, tmp_path)

    assert first.archived.storage_path == second.archived.storage_path


# --- Archive-first: successful archiving despite parse failure ---


def test_ingest_upload_fit_shaped_but_undecodable_is_archived_then_reported_as_unparsed(
    tmp_path: Path,
) -> None:
    outcome = ingest_upload(_FAKE_FIT, tmp_path)

    # Counterproof: if "archive-first" principle weren't respected, file would not be on
    # disk despite parse failure — it MUST be here.
    assert outcome.archived.storage_path.exists()
    assert outcome.archived.source_format == "fit"
    assert outcome.parsed is None
    assert outcome.parse_error is not None


def test_ingest_upload_tcx_without_usable_trackpoint_is_archived_then_reported_as_unparsed(
    tmp_path: Path,
) -> None:
    outcome = ingest_upload(_CORRUPT_TCX, tmp_path)

    assert outcome.archived.storage_path.exists()
    assert outcome.parsed is None
    assert outcome.parse_error is not None
    assert "Trackpoint" in outcome.parse_error


def test_ingest_upload_unrecognized_content_raises_archive_error_with_no_side_effect(
    tmp_path: Path,
) -> None:
    with pytest.raises(ArchiveError):
        ingest_upload(b"neither FIT nor TCX nor ZIP, just some text", tmp_path)

    # Counterproof of archive-first: unrecognized content must leave NOTHING on disk (no
    # orphan file), otherwise this assertion would fail.
    assert list(tmp_path.iterdir()) == []


# --- Maximum size ---


def test_ingest_upload_rejects_oversized_payload_before_touching_disk(tmp_path: Path) -> None:
    with pytest.raises(PayloadTooLargeError):
        ingest_upload(_VALID_TCX, tmp_path, max_upload_bytes=len(_VALID_TCX) - 1)

    assert list(tmp_path.iterdir()) == []


def test_ingest_upload_accepts_payload_exactly_at_the_limit(tmp_path: Path) -> None:
    # Counterproof of strict bound: "> max" rejects, "== max" must pass — an off-by-one
    # implementation (>=) would fail this test.
    outcome = ingest_upload(_VALID_TCX, tmp_path, max_upload_bytes=len(_VALID_TCX))
    assert outcome.archived.storage_path.exists()


# --- Hostile ZIP: path escapes ---


def test_check_zip_safety_rejects_parent_directory_escape() -> None:
    zip_data = _zip_bytes({"../../etc/evil.tcx": _VALID_TCX})

    with pytest.raises(HostileArchiveError, match="suspicious"):
        check_zip_safety(zip_data)


def test_check_zip_safety_rejects_absolute_path_member() -> None:
    zip_data = _zip_bytes({"/etc/evil.tcx": _VALID_TCX})

    with pytest.raises(HostileArchiveError, match="suspicious"):
        check_zip_safety(zip_data)


def test_check_zip_safety_accepts_plain_relative_member_name() -> None:
    zip_data = _zip_bytes({"activity.tcx": _VALID_TCX})

    check_zip_safety(zip_data)  # must not raise


def test_ingest_upload_rejects_zip_with_escaping_member_with_no_side_effect(
    tmp_path: Path,
) -> None:
    zip_data = _zip_bytes({"../evil.tcx": _VALID_TCX})

    with pytest.raises(HostileArchiveError):
        ingest_upload(zip_data, tmp_path)

    # Counterproof of archive-first: a hostile archive must leave NOTHING on disk, not
    # even the content it carries — rejection happens before any archive_download call.
    assert list(tmp_path.iterdir()) == []


# --- Hostile ZIP: decompression bomb ---------------------------------------------


def _zip_bomb_bytes(ratio_target: float) -> bytes:
    """Build a ZIP archive where a member claims a high compression ratio, without ever
    materializing the entire decompressed content in memory (directly write a highly
    compressible entry — repeated zeros — large enough to exceed ``ratio_target`` once
    compressed by zlib's default degree)."""
    payload = b"\x00" * int(4 * 1024 * 1024)  # 4 MiB of zeros: zlib compresses to a few
    # hundred bytes, well above the module's default ratio.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.tcx", payload)
    data = buf.getvalue()
    # Verify the fixture reaches the target ratio before serving to test — otherwise the
    # next test would be a silent false positive.
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.infolist()[0]
        assert info.file_size / max(info.compress_size, 1) > ratio_target
    return data


def test_check_zip_safety_rejects_high_compression_ratio_member() -> None:
    zip_data = _zip_bomb_bytes(DEFAULT_MAX_COMPRESSION_RATIO)

    with pytest.raises(HostileArchiveError, match="bomb"):
        check_zip_safety(zip_data)


def test_check_zip_safety_rejects_declared_uncompressed_size_above_bound() -> None:
    zip_data = _zip_bytes({"huge.tcx": _VALID_TCX})
    with pytest.raises(HostileArchiveError, match="decompressed bytes"):
        check_zip_safety(zip_data, max_member_uncompressed_bytes=len(_VALID_TCX) - 1)


# --- Simply invalid ZIP: not hostile, left to archive_download ---


def test_check_zip_safety_is_silent_on_malformed_zip_and_lets_archive_download_reject_it(
    tmp_path: Path,
) -> None:
    malformed = b"PK\x03\x04not a real zip stream"

    check_zip_safety(malformed)  # does not raise HostileArchiveError

    with pytest.raises(ArchiveError, match="Invalid ZIP archive"):
        ingest_upload(malformed, tmp_path)
    assert list(tmp_path.iterdir()) == []


# --- Insert raw.fit_file (conn optional, garmin_activity_id=None) ---


def test_ingest_upload_without_conn_leaves_fit_file_id_none(tmp_path: Path) -> None:
    """``conn`` remains optional: without it, this function only validates."""
    outcome = ingest_upload(_VALID_TCX, tmp_path)

    assert outcome.fit_file_id is None


def test_ingest_upload_with_conn_inserts_raw_fit_file_row_as_manual_upload(
    tmp_path: Path, raw_store_conn: FakeRawStoreConnection
) -> None:
    outcome = ingest_upload(_VALID_TCX, tmp_path, conn=raw_store_conn)

    assert outcome.fit_file_id is not None
    sha = hashlib.sha256(_VALID_TCX).digest()
    row = raw_store_conn.rows_by_sha[sha]
    # Convention set by migration 012: NULL is the ONLY marker for manual upload source,
    # never a sentinel garmin_activity_id.
    assert row.garmin_activity_id is None
    assert row.fit_file_id == outcome.fit_file_id
    assert row.storage_path == str(outcome.archived.storage_path)
    assert row.size_bytes == outcome.archived.size_bytes
    assert raw_store_conn.commits >= 1


def test_ingest_upload_inserts_row_even_when_local_parse_validation_fails(
    tmp_path: Path, raw_store_conn: FakeRawStoreConnection
) -> None:
    """An archived-but-locally-unparseable upload must still enter the queue — it is
    t0.transform that attempts real parsing, not this entry point."""
    outcome = ingest_upload(_FAKE_FIT, tmp_path, conn=raw_store_conn)

    assert outcome.parse_error is not None
    assert outcome.fit_file_id is not None


def test_ingest_upload_writes_file_on_volume_before_inserting_raw_fit_file_row(
    tmp_path: Path, raw_store_conn: FakeRawStoreConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Central invariant of raw.fit_file module: write-fsync-before-row.

    An implementation reversing the order would pass all other tests in this module (they
    only check final state) — only inspection AT insertion time can distinguish the two
    orders, hence the spy below.
    """
    import t0.api.ingest as ingest_module

    original_insert = insert_fit_file
    volume_contents_at_insert: list[list[Path]] = []

    def _spy_insert(
        conn: FakeRawStoreConnection,
        *,
        garmin_activity_id: int | None,
        storage_path: str,
        sha256: bytes,
        size_bytes: int,
    ) -> int:
        volume_contents_at_insert.append(list(tmp_path.iterdir()))
        return original_insert(
            conn,
            garmin_activity_id=garmin_activity_id,
            storage_path=storage_path,
            sha256=sha256,
            size_bytes=size_bytes,
        )

    monkeypatch.setattr(ingest_module, "insert_fit_file", _spy_insert)

    ingest_upload(_VALID_TCX, tmp_path, conn=raw_store_conn)

    assert len(volume_contents_at_insert) == 1
    assert volume_contents_at_insert[0] != []  # file already exists on disk


def test_ingest_upload_raises_conflict_when_sha_belongs_to_a_different_activity(
    tmp_path: Path, raw_store_conn: FakeRawStoreConnection
) -> None:
    sha = hashlib.sha256(_VALID_TCX).digest()
    raw_store_conn.preload(sha256=sha, garmin_activity_id=555)

    with pytest.raises(FitFileShaConflictError):
        ingest_upload(_VALID_TCX, tmp_path, conn=raw_store_conn)

    # Archive-first: file stays archived on disk despite insert conflict, no loss — same
    # guarantee as for local parse failure.
    assert list(tmp_path.iterdir()) != []


def test_ingest_upload_reuses_existing_row_for_same_manual_upload_content_replayed(
    tmp_path: Path, raw_store_conn: FakeRawStoreConnection
) -> None:
    """Two identical manual uploads (same sha256, both garmin_activity_id=None) are NOT a
    conflict: None == None is source agreement."""
    first = ingest_upload(_VALID_TCX, tmp_path, conn=raw_store_conn)
    second = ingest_upload(_VALID_TCX, tmp_path, conn=raw_store_conn)

    assert first.fit_file_id == second.fit_file_id
    assert len(raw_store_conn.rows_by_sha) == 1
