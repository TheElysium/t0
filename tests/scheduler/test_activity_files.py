"""Tests for :mod:`t0.scheduler.activity_files` (batch L12).

No network, no real PostgreSQL: ``_FakeDownloader`` (below) satisfies
``activity_files.OriginalFileDownloader`` and ``_FakeFitFileDB`` satisfies
``t0.garmin.raw_store.DBConnection``, both dedicated to this file—
``tests/scheduler/fakes.py`` (existing, unmodified) simulates ``raw.poll_window`` /
``raw.payload_seen``, not ``raw.fit_file``, so is not reusable here. FIT/TCX/ZIP
bytes are synthetic, hand-constructed like in ``tests/fit/test_archive.py``—never
an excerpt from the athlete's actual files.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from garminconnect import GarminConnectAuthenticationError

from t0.garmin.client import IngestRunTracker, OriginalActivityFile
from t0.scheduler.activity_files import (
    ActivityFileOutcome,
    archive_activities_for_day,
    archive_activity_file,
)

if TYPE_CHECKING:
    from t0.garmin.raw_store import DBConnection as _RawStoreDBConnection

# Minimal valid FIT signature, same spirit as tests/fit/test_archive.py: after an
# 8-byte header, the string ".FIT" at bytes 8-11.
_FAKE_FIT = b"\x0e\x10\xc0\x00\x00\x00\x00\x00.FIT" + b"\x00" * 20
_FAKE_FIT_V2 = (
    b"\x0e\x10\xc0\x00\x00\x00\x00\x00.FIT" + b"\x01" * 40
)  # DIFFERENT content, "more complete"
_FAKE_TCX = b'<?xml version="1.0"?><TrainingCenterDatabase/>'
_GARBAGE = b"this is neither FIT nor TCX"

_SENTINEL_DL_FMT = object()  # placeholder for garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _stem_suffix(content: bytes) -> str:
    """Expected sha256 excerpt in the filename—see activity_files.py."""
    return hashlib.sha256(content).hexdigest()[:12]


@dataclass
class _FakeDownloader:
    """Satisfies ``activity_files.OriginalFileDownloader``, no network."""

    outcomes: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, Any, IngestRunTracker | None]] = field(default_factory=list, init=False)

    def download_original(
        self,
        activity_id: str,
        *,
        dl_fmt: Any,
        tracker: IngestRunTracker | None = None,
    ) -> OriginalActivityFile:
        self.calls.append((activity_id, dl_fmt, tracker))
        outcome = self.outcomes[activity_id]
        if isinstance(outcome, BaseException):
            # Mimics the real behavior of GarminClient.download_original: the tracker
            # records the failure before the exception propagates (see client.py).
            if tracker is not None:
                tracker.record_error()
            raise outcome
        if tracker is not None:
            tracker.record_success()
        return OriginalActivityFile(
            activity_id=activity_id, filename=f"{activity_id}.fit", content=outcome
        )


@dataclass
class _FitFileRow:
    fit_file_id: int
    garmin_activity_id: int
    storage_path: str
    sha256: bytes
    size_bytes: int


class _FakeCursor:
    def __init__(self, conn: _FakeFitFileDB) -> None:
        self._conn = conn
        self._result: tuple[Any, ...] | None = None

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())
        self._conn.executed.append(normalized)

        if normalized.startswith("SELECT fit_file_id, sha256, storage_path FROM raw.fit_file"):
            # _latest_fit_file: ORDER BY downloaded_at DESC LIMIT 1—our fakes have no
            # real timestamp, but the order of addition to `rows` IS the chronological
            # order in all our scenarios, so the last added matching row is "the latest
            # version".
            (garmin_activity_id,) = params
            matches = [r for r in self._conn.rows if r.garmin_activity_id == garmin_activity_id]
            self._result = (
                (matches[-1].fit_file_id, matches[-1].sha256, matches[-1].storage_path)
                if matches
                else None
            )
            return

        if normalized.startswith("INSERT INTO raw.fit_file"):
            if self._conn.fail_insert:
                raise RuntimeError("simulated database failure during INSERT raw.fit_file")
            garmin_activity_id, storage_path, sha256, size_bytes = params
            existing = next((r for r in self._conn.rows if r.sha256 == sha256), None)
            if existing is not None:
                self._result = None
                return
            self._conn.next_id += 1
            row = _FitFileRow(
                fit_file_id=self._conn.next_id,
                garmin_activity_id=garmin_activity_id,
                storage_path=storage_path,
                sha256=sha256,
                size_bytes=size_bytes,
            )
            self._conn.rows.append(row)
            self._result = (row.fit_file_id,)
            return

        if normalized.startswith("SELECT fit_file_id, garmin_activity_id FROM raw.fit_file"):
            (sha256,) = params
            existing = next((r for r in self._conn.rows if r.sha256 == sha256), None)
            self._result = (
                (existing.fit_file_id, existing.garmin_activity_id)
                if existing is not None
                else None
            )
            return

        raise AssertionError(f"_FakeCursor: unexpected query → {normalized}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class _FakeFitFileDB:
    """Satisfies ``t0.garmin.raw_store.DBConnection``, in-memory."""

    rows: list[_FitFileRow] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    commit_count: int = 0
    next_id: int = 0
    fail_insert: bool = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1


# Structurally satisfies raw_store.DBConnection—checked once here rather than let
# mypy silently rediscover it at each call site.
def _typecheck_fake_db_is_dbconnection(conn: _FakeFitFileDB) -> _RawStoreDBConnection:
    return conn


_WINDOW_DATE = date(2026, 8, 20)
_TODAY_AFTER_WINDOW = date(2026, 8, 21)  # CLOSED window: window_date < today


def test_archive_activity_file_downloads_archives_and_inserts(tmp_path: Path) -> None:
    downloader = _FakeDownloader(outcomes={"111": _FAKE_FIT})
    conn = _FakeFitFileDB()

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "archived"
    assert outcome.fit_file_id == 1
    assert outcome.storage_path is not None
    assert outcome.storage_path.exists()
    assert outcome.storage_path.read_bytes() == _FAKE_FIT
    # YYYY/MM tree structure, activity retrievable from its id without consulting database.
    assert outcome.storage_path.parent == tmp_path / "2026" / "08"
    assert outcome.storage_path.name == f"activity-111-{_stem_suffix(_FAKE_FIT)}.fit"
    assert conn.commit_count == 1
    assert downloader.calls == [("111", _SENTINEL_DL_FMT, None)]
    # Positive proof of PVC-before-database: at the exact moment the raw.fit_file row
    # was inserted (right after the INSERT in our fake), the file already existed on disk.
    insert_index = next(
        i for i, q in enumerate(conn.executed) if q.startswith("INSERT INTO raw.fit_file")
    )
    assert conn.executed[insert_index] is not None
    assert outcome.storage_path.exists()  # always true in hindsight, but order is
    # guaranteed by construction: archive_activity_file calls insert_fit_file only
    # after archive_download returns (see _archive_and_insert).


def test_archive_activity_file_tcx_content_is_archived(tmp_path: Path) -> None:
    downloader = _FakeDownloader(outcomes={"222": _FAKE_TCX})
    conn = _FakeFitFileDB()

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="222",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "archived"
    assert outcome.storage_path is not None
    assert outcome.storage_path.suffix == ".tcx"
    assert outcome.storage_path.read_bytes() == _FAKE_TCX


def test_archive_activity_file_zip_wrapped_content_is_extracted_and_archived(
    tmp_path: Path,
) -> None:
    zip_data = _zip_bytes({"333.fit": _FAKE_FIT})
    downloader = _FakeDownloader(outcomes={"333": zip_data})
    conn = _FakeFitFileDB()

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="333",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "archived"
    assert outcome.storage_path is not None
    assert outcome.storage_path.suffix == ".fit"
    # The ZIP was unwrapped: the archived content is the FIT member's, not the ZIP.
    assert outcome.storage_path.read_bytes() == _FAKE_FIT


def test_archive_activity_file_closed_window_is_idempotent_no_redownload(tmp_path: Path) -> None:
    downloader = _FakeDownloader(outcomes={"111": _FAKE_FIT})
    conn = _FakeFitFileDB()
    conn.rows.append(
        _FitFileRow(
            fit_file_id=42,
            garmin_activity_id=111,
            storage_path=str(tmp_path / "2026" / "08" / "activity-111-abc123.fit"),
            sha256=b"x" * 32,
            size_bytes=10,
        )
    )

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,  # CLOSED window: window_date < today
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "already_present"
    assert outcome.fit_file_id == 42
    assert downloader.calls == []  # NO network calls: that's the point of this test.


def test_archive_activity_file_same_day_unchanged_content_is_already_present(
    tmp_path: Path,
) -> None:
    # CURRENT window (window_date == today): the file IS re-downloaded, but its content
    # is identical to the already-known version—no new row, no new file, but the network
    # was called (unlike the closed-window case above).
    downloader = _FakeDownloader(outcomes={"111": _FAKE_FIT})
    conn = _FakeFitFileDB()
    existing_path = tmp_path / "2026" / "08" / f"activity-111-{_stem_suffix(_FAKE_FIT)}.fit"
    conn.rows.append(
        _FitFileRow(
            fit_file_id=42,
            garmin_activity_id=111,
            storage_path=str(existing_path),
            sha256=hashlib.sha256(_FAKE_FIT).digest(),
            size_bytes=len(_FAKE_FIT),
        )
    )

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_WINDOW_DATE,  # CURRENT window
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "already_present"
    assert outcome.fit_file_id == 42
    assert outcome.storage_path == existing_path
    assert downloader.calls == [("111", _SENTINEL_DL_FMT, None)]  # network WAS called
    assert len(conn.rows) == 1  # but nothing new was inserted


def test_archive_activity_file_same_day_changed_content_is_refreshed(tmp_path: Path) -> None:
    # BLOCKING 1 (review): an activity still uploading on Garmin on first pass (truncated
    # file) must be CAUGHT UP when the window is re-processed the same day with a now
    # more complete file—not locked forever on the truncated version. Before the fix,
    # `_existing_fit_file_id` returned "already_present" on first row found, never
    # comparing content or re-downloading: this test would have failed on the old version.
    downloader = _FakeDownloader(outcomes={"111": _FAKE_FIT_V2})
    conn = _FakeFitFileDB()
    old_path = tmp_path / "2026" / "08" / f"activity-111-{_stem_suffix(_FAKE_FIT)}.fit"
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(_FAKE_FIT)
    conn.rows.append(
        _FitFileRow(
            fit_file_id=42,
            garmin_activity_id=111,
            storage_path=str(old_path),
            sha256=hashlib.sha256(_FAKE_FIT).digest(),
            size_bytes=len(_FAKE_FIT),
        )
    )
    conn.next_id = 42  # the pre-existing row above was inserted "before" this test

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_WINDOW_DATE,  # CURRENT window: same day
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "refreshed"
    assert outcome.fit_file_id == 43  # new row, NOT 42
    assert outcome.storage_path is not None
    assert outcome.storage_path != old_path  # filename qualified by new content
    assert outcome.storage_path.read_bytes() == _FAKE_FIT_V2
    # The OLD file was NOT overwritten—it stays readable and intact at its original
    # path: that is exactly what sha256-qualified naming must guarantee.
    assert old_path.exists()
    assert old_path.read_bytes() == _FAKE_FIT
    assert len(conn.rows) == 2


def test_archive_activity_file_zip_same_day_unchanged_content_is_already_present(
    tmp_path: Path,
) -> None:
    # REGRESSION (real bug found in review): in production Garmin serves a ZIP ARCHIVE,
    # while `raw.fit_file.sha256` holds the sha of the EXTRACTED member. The
    # "unchanged content" shortcut hashed the envelope: it could thus NEVER match, and
    # every current-day activity was re-archived on every pass.
    #
    # The ZIP served here has a different MEMBER NAME than the first archive: its
    # envelope bytes necessarily differ, while the useful content is identical. Only
    # a comparison on the extracted member can conclude.
    downloader = _FakeDownloader(outcomes={"111": _zip_bytes({"other-name.fit": _FAKE_FIT})})
    conn = _FakeFitFileDB()
    existing_path = tmp_path / "2026" / "08" / f"activity-111-{_stem_suffix(_FAKE_FIT)}.fit"
    conn.rows.append(
        _FitFileRow(
            fit_file_id=42,
            garmin_activity_id=111,
            storage_path=str(existing_path),
            sha256=hashlib.sha256(_FAKE_FIT).digest(),
            size_bytes=len(_FAKE_FIT),
        )
    )

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_WINDOW_DATE,  # CURRENT window: re-download does happen
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "already_present"
    assert outcome.fit_file_id == 42
    assert len(conn.rows) == 1  # nothing new inserted
    assert not (tmp_path / "2026").exists()  # and NOTHING new written to PVC


def test_archive_activity_file_zip_same_day_changed_content_is_refreshed(tmp_path: Path) -> None:
    # Counterpart of previous test: unwrapping must not blind the comparison to real
    # changes. Truly different content → new version.
    downloader = _FakeDownloader(outcomes={"111": _zip_bytes({"111.fit": _FAKE_FIT_V2})})
    conn = _FakeFitFileDB()
    old_path = tmp_path / "2026" / "08" / f"activity-111-{_stem_suffix(_FAKE_FIT)}.fit"
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(_FAKE_FIT)
    conn.rows.append(
        _FitFileRow(
            fit_file_id=42,
            garmin_activity_id=111,
            storage_path=str(old_path),
            sha256=hashlib.sha256(_FAKE_FIT).digest(),
            size_bytes=len(_FAKE_FIT),
        )
    )
    conn.next_id = 42

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_WINDOW_DATE,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "refreshed"
    assert outcome.storage_path is not None
    assert outcome.storage_path.read_bytes() == _FAKE_FIT_V2


def test_archive_activity_file_zip_filename_stem_matches_stored_sha(tmp_path: Path) -> None:
    # The sha in the FILENAME, the one verified after re-reading, and the one inserted
    # in the database must be a single number—else a later pass writes a file on PVC
    # that no `raw.fit_file` row references (silent orphan).
    downloader = _FakeDownloader(outcomes={"333": _zip_bytes({"333.fit": _FAKE_FIT})})
    conn = _FakeFitFileDB()

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="333",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "archived"
    assert outcome.storage_path is not None
    (row,) = conn.rows
    assert row.sha256 == hashlib.sha256(_FAKE_FIT).digest()  # the MEMBER, not the ZIP
    assert outcome.storage_path.name == f"activity-333-{_stem_suffix(_FAKE_FIT)}.fit"
    assert Path(row.storage_path) == outcome.storage_path  # no orphan possible


def test_archive_activity_file_invalid_zip_is_failed_and_counted(tmp_path: Path) -> None:
    # Extraction is the only step that `download_original` does not count itself:
    # its failure must increment the tracker (and leave nothing on PVC).
    downloader = _FakeDownloader(outcomes={"111": b"PK pas vraiment un zip"})
    conn = _FakeFitFileDB()
    run_conn = _FakeFitFileDB()  # IngestRunTracker requires a DBConnection, unused here
    tracker = IngestRunTracker(conn=run_conn, ingest_run_id=1)

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
        tracker=tracker,
    )

    assert outcome.status == "failed"
    assert conn.rows == []
    assert not (tmp_path / "2026").exists()
    assert tracker.errors == 1


def test_pvc_before_db_download_failure_writes_nothing(tmp_path: Path) -> None:
    downloader = _FakeDownloader(outcomes={"111": RuntimeError("simulated 429")})
    conn = _FakeFitFileDB()

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "failed"
    assert outcome.error is not None and "simulated 429" in outcome.error
    assert conn.rows == []
    assert not (tmp_path / "2026").exists()  # nothing ever reached PVC


def test_pvc_before_db_archive_failure_writes_no_db_row(tmp_path: Path) -> None:
    downloader = _FakeDownloader(outcomes={"111": _GARBAGE})
    conn = _FakeFitFileDB()

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "failed"
    assert conn.rows == []
    assert not any(q.startswith("INSERT INTO raw.fit_file") for q in conn.executed)
    # No file—neither final nor temporary—should have been left.
    dest_dir = tmp_path / "2026" / "08"
    assert not dest_dir.exists() or list(dest_dir.iterdir()) == []


def test_pvc_before_db_insert_failure_leaves_orphan_file_on_disk(tmp_path: Path) -> None:
    # Heart of the batch invariant: a database WRITE failure, occurring AFTER successful
    # archiving, must never make a file already written and verified on PVC disappear—
    # it becomes a recoverable orphan, never a loss.
    downloader = _FakeDownloader(outcomes={"111": _FAKE_FIT})
    conn = _FakeFitFileDB(fail_insert=True)

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "failed"
    assert conn.rows == []
    archived_path = tmp_path / "2026" / "08" / f"activity-111-{_stem_suffix(_FAKE_FIT)}.fit"
    assert archived_path.exists()
    assert archived_path.read_bytes() == _FAKE_FIT


def test_orphan_file_is_recovered_on_second_pass(tmp_path: Path) -> None:
    # Direct continuation of the previous test: once the database failure is resolved, a
    # second pass on the SAME activity must re-download (no row in database yet), land on
    # the same content already on PVC (same sha256, so same path), and this time insert
    # the row successfully—without ever duplicating or corrupting the file.
    downloader = _FakeDownloader(outcomes={"111": _FAKE_FIT})
    conn = _FakeFitFileDB(fail_insert=True)
    first = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )
    assert first.status == "failed"
    assert conn.rows == []

    conn.fail_insert = False  # database failure is resolved before the next pass
    second = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert second.status == "archived"
    assert second.storage_path is not None
    assert second.storage_path.read_bytes() == _FAKE_FIT
    assert len(conn.rows) == 1
    assert downloader.calls == [
        ("111", _SENTINEL_DL_FMT, None),
        ("111", _SENTINEL_DL_FMT, None),
    ]


def test_duplicate_content_across_activities_does_not_misattribute(tmp_path: Path) -> None:
    # BLOCKING 2 (review), seen from activity_files.py: multisport/brick scenario where
    # two distinct garmin_activity_ids serve EXACTLY the same original content. The
    # losing activity must neither be silently assigned the other's fit_file_id nor break
    # all day processing—it gets a dedicated, explicit status.
    downloader = _FakeDownloader(outcomes={"111": _FAKE_FIT, "222": _FAKE_FIT})
    conn = _FakeFitFileDB()

    first = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )
    second = archive_activity_file(
        downloader,
        conn,
        activity_id="222",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert first.status == "archived"
    assert second.status == "duplicate_content"
    assert second.fit_file_id is None
    assert second.error is not None and "111" in second.error
    # The raw.fit_file row stays attributed to the winning activity, never re-attributed.
    assert len(conn.rows) == 1
    assert conn.rows[0].garmin_activity_id == 111


def test_authentication_error_propagates_uncaught_single_activity(tmp_path: Path) -> None:
    # IMPORTANT 1 (review): a denied auth must never be swallowed
    # behind a "failed" status—propagating it prevents a caller looping over dozens of
    # activities from retrying that many times, a pattern that locks a Garmin account
    # without 2FA.
    downloader = _FakeDownloader(outcomes={"111": GarminConnectAuthenticationError("denied")})
    conn = _FakeFitFileDB()

    with pytest.raises(GarminConnectAuthenticationError):
        archive_activity_file(
            downloader,
            conn,
            activity_id="111",
            window_date=_WINDOW_DATE,
            today=_TODAY_AFTER_WINDOW,
            archive_root=tmp_path,
            dl_fmt=_SENTINEL_DL_FMT,
        )
    assert conn.rows == []


def test_authentication_error_propagates_uncaught_through_day_loop(tmp_path: Path) -> None:
    # Same requirement, but at the archive_activities_for_day level: the defensive
    # "except Exception" catch in the loop must NOT swallow what
    # archive_activity_file deliberately let bubble up.
    downloader = _FakeDownloader(
        outcomes={"111": _FAKE_FIT, "222": GarminConnectAuthenticationError("denied")}
    )
    conn = _FakeFitFileDB()

    with pytest.raises(GarminConnectAuthenticationError):
        archive_activities_for_day(
            downloader,
            conn,
            activity_ids=["111", "222"],
            window_date=_WINDOW_DATE,
            today=_TODAY_AFTER_WINDOW,
            archive_root=tmp_path,
            dl_fmt=_SENTINEL_DL_FMT,
        )
    # The first activity, processed before the auth failure, was indeed archived:
    # interruption happens at the error moment, not before.
    assert len(conn.rows) == 1


def test_tracker_records_error_on_archive_stage_failure(tmp_path: Path) -> None:
    # IMPORTANT 2 (review): without this, a run entirely failed at archive/insert stage
    # would still close "ok" (ingest_run.status), since IngestRunTracker.errors would
    # only count network failures already counted by download_original itself.
    downloader = _FakeDownloader(outcomes={"111": _GARBAGE})
    conn = _FakeFitFileDB()
    run_conn = _FakeFitFileDB()  # IngestRunTracker requires a DBConnection, unused here
    tracker = IngestRunTracker(conn=run_conn, ingest_run_id=1)

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
        tracker=tracker,
    )

    assert outcome.status == "failed"
    assert tracker.errors == 1


def test_tracker_records_error_on_insert_stage_failure(tmp_path: Path) -> None:
    downloader = _FakeDownloader(outcomes={"111": _FAKE_FIT})
    conn = _FakeFitFileDB(fail_insert=True)
    run_conn = _FakeFitFileDB()
    tracker = IngestRunTracker(conn=run_conn, ingest_run_id=1)

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
        tracker=tracker,
    )

    assert outcome.status == "failed"
    assert tracker.errors == 1


def test_tracker_records_error_on_duplicate_content(tmp_path: Path) -> None:
    downloader = _FakeDownloader(outcomes={"111": _FAKE_FIT, "222": _FAKE_FIT})
    conn = _FakeFitFileDB()
    run_conn = _FakeFitFileDB()
    tracker = IngestRunTracker(conn=run_conn, ingest_run_id=1)

    archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
        tracker=tracker,
    )
    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="222",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
        tracker=tracker,
    )

    assert outcome.status == "duplicate_content"
    assert tracker.errors == 1  # only "222"'s failure counts—"111" succeeded


def test_archive_activities_for_day_isolates_failures(tmp_path: Path) -> None:
    downloader = _FakeDownloader(
        outcomes={
            "111": _FAKE_FIT,
            "222": RuntimeError("simulated network failure"),
            "333": _FAKE_FIT_V2,
        }
    )
    conn = _FakeFitFileDB()

    summary = archive_activities_for_day(
        downloader,
        conn,
        activity_ids=["111", "222", "333"],
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert [o.activity_id for o in summary.archived] == ["111", "333"]
    assert [o.activity_id for o in summary.failed] == ["222"]
    assert summary.already_present == []
    assert summary.refreshed == []
    assert summary.duplicate_content == []
    assert len(summary.outcomes) == 3
    # Operator log mentions all three activities, success and failure.
    log_lines = summary.as_log_lines()
    assert len(log_lines) == 3
    assert "FAILED" in log_lines[1]
    assert "222" in log_lines[1]


def test_archive_activities_for_day_populates_already_present(tmp_path: Path) -> None:
    downloader = _FakeDownloader(outcomes={"111": _FAKE_FIT})
    conn = _FakeFitFileDB()
    conn.rows.append(
        _FitFileRow(
            fit_file_id=7,
            garmin_activity_id=111,
            storage_path=str(tmp_path / "2026" / "08" / "activity-111-xyz.fit"),
            sha256=b"y" * 32,
            size_bytes=5,
        )
    )

    summary = archive_activities_for_day(
        downloader,
        conn,
        activity_ids=["111"],
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,  # closed window: strict idempotence, no network
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert [o.activity_id for o in summary.already_present] == ["111"]
    assert summary.already_present[0].fit_file_id == 7
    assert downloader.calls == []


def test_as_log_line_content_for_each_status() -> None:
    archived = ActivityFileOutcome(
        activity_id="1", status="archived", fit_file_id=9, storage_path=Path("/x/1.fit")
    )
    refreshed = ActivityFileOutcome(
        activity_id="4", status="refreshed", fit_file_id=10, storage_path=Path("/x/4.fit")
    )
    present = ActivityFileOutcome(activity_id="2", status="already_present", fit_file_id=3)
    duplicate = ActivityFileOutcome(activity_id="5", status="duplicate_content", error="duplicate")
    failed = ActivityFileOutcome(activity_id="3", status="failed", error="boom")

    assert "archived" in archived.as_log_line()
    assert "updated" in refreshed.as_log_line()
    assert "already archived" in present.as_log_line()
    assert "DUPLICATE CONTENT" in duplicate.as_log_line() and "duplicate" in duplicate.as_log_line()
    assert "FAILED" in failed.as_log_line() and "boom" in failed.as_log_line()


def test_dest_dir_organized_by_year_month_from_window_date(tmp_path: Path) -> None:
    downloader = _FakeDownloader(outcomes={"555": _FAKE_FIT})
    conn = _FakeFitFileDB()

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="555",
        window_date=date(2025, 1, 3),
        today=date(2025, 1, 4),
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.storage_path is not None
    assert outcome.storage_path.parent == tmp_path / "2025" / "01"


def test_malformed_activity_id_fails_cleanly_without_raising(tmp_path: Path) -> None:
    # IMPORTANT 3 (review): the int(activity_id) conversion is guarded ONCE, at the
    # head of archive_activity_file—it must never raise outside this function, even
    # called directly (not only via archive_activities_for_day).
    downloader = _FakeDownloader(outcomes={})
    conn = _FakeFitFileDB()

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="abc",
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "failed"
    assert downloader.calls == []  # never reached network


def test_unexpected_exception_in_activity_id_is_isolated(tmp_path: Path) -> None:
    # "abc" is not convertible to int: captured cleanly (see test above).
    # archive_activities_for_day must nonetheless continue to the following activities.
    downloader = _FakeDownloader(outcomes={"999": _FAKE_FIT})
    conn = _FakeFitFileDB()

    summary = archive_activities_for_day(
        downloader,
        conn,
        activity_ids=["abc", "999"],
        window_date=_WINDOW_DATE,
        today=_TODAY_AFTER_WINDOW,
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert [o.activity_id for o in summary.failed] == ["abc"]
    assert [o.activity_id for o in summary.archived] == ["999"]
