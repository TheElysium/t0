"""Tests for entry point ``python -m garminator.scheduler`` (batch L11).

Never builds real ``garminconnect`` transport or real PostgreSQL connection:
``main()`` accepts three injection points (``settings``, ``connect_db``,
``build_window_fetcher``) precisely to enable this kind of test -- see the tested
module's docstring. Only argument parsing, exit codes, and ``--dry-run`` mode are
exercised here ; actual orchestration (``run_scheduled_poll``) is already tested by
``tests/scheduler/test_service.py``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import garminconnect
from pydantic import SecretStr

from garminator.garmin.client import GarminClient
from garminator.scheduler import __main__ as scheduler_main
from garminator.scheduler.activity_files import DayArchiveSummary
from garminator.scheduler.fetcher import GarminWindowFetcher
from garminator.scheduler.service import FetchedWindow
from garminator.settings import Settings
from tests.scheduler.fakes import INGEST_RUN_MODES, FakePollDB
from tests.scheduler.fakes import FakeCursor as _PollFakeCursor

if TYPE_CHECKING:
    import pytest

    from garminator.garmin.client import IngestRunTracker
    from garminator.scheduler.service import WindowFetcher
    from garminator.scheduler.store import DBConnection


class _MainFakeCursor(_PollFakeCursor):
    """Extends :mod:`tests.scheduler.fakes` cursor with ``raw.ingest_run`` queries --
    ``main()`` itself wraps ``run_scheduled_poll`` in an ``ingest_run(...)`` call, in
    addition to ``poll_window``/``payload_seen`` queries that ``FakePollDB`` already
    handles. Composition over duplication: only recognition of the two extra queries
    is added here, everything else (poll_window, payload_seen) delegates to parent,
    proven by batch L6.
    """

    def __init__(self, db: _MainFakeDB) -> None:
        super().__init__(db)
        self._main_db = db

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())

        if normalized.startswith("INSERT INTO raw.ingest_run"):
            # Stub must be as strict as ``ingest_run_mode_ck``: without this guard, it
            # accepted batch L11's ``mode="scheduled"``, which failed on first production query.
            mode = params[0]
            assert mode in INGEST_RUN_MODES, (
                f"mode={mode!r} violates ingest_run_mode_ck : PostgreSQL would reject this "
                f"INSERT (accepted values: {sorted(INGEST_RUN_MODES)})"
            )
            self._main_db.executed.append((normalized, params))
            self._main_db.next_ingest_run_id += 1
            self._result = [(self._main_db.next_ingest_run_id,)]
            return

        if normalized.startswith("UPDATE raw.ingest_run"):
            self._main_db.executed.append((normalized, params))
            self._result = []
            return

        super().execute(query, params)


@dataclass
class _MainFakeDB(FakePollDB):
    """``FakePollDB`` (poll_window/payload_seen) + in-memory ``ingest_run`` counter :
    the only stub in this file, reused by all tests not using ``--dry-run``
    (which never touches ``raw.ingest_run``)."""

    next_ingest_run_id: int = 0
    closed: bool = field(default=False, init=False)

    def cursor(self) -> _MainFakeCursor:
        return _MainFakeCursor(self)

    def close(self) -> None:
        self.closed = True


#: DSN explicitly passed via ``--dsn`` in tests. Since batch recovery, ``main()`` resolves
#: DSN like ``garminator.ops.__main__`` (argument or ``GARMINATOR_POSTGRES_DSN``), not
#: from ``Settings`` : ``--dry-run`` must run without any Garmin secrets. Tests provide
#: it on the command line, independent of the machine's environment.
_DSN = "postgresql://user:pass@localhost:5432/garminator"


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "postgres_dsn": SecretStr("postgresql://user:pass@localhost:5432/garminator"),
        # ``garmin_tokens_path`` has ``validation_alias="GARMINTOKENS"`` (settings.py):
        # without ``populate_by_name``, only the alias is accepted in direct construction.
        "GARMINTOKENS": Path("/dev/null"),
        "fit_archive_path": Path("/dev/null"),
        "garmin_username": SecretStr("athlete@example.invalid"),
        "garmin_password": SecretStr("unused-in-these-tests"),
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _unreachable_build_window_fetcher(
    _settings: Settings, _conn: DBConnection, _tracker: IngestRunTracker
) -> WindowFetcher:
    raise AssertionError("build_window_fetcher called: --dry-run must never build a fetcher")


def _unreachable_connect(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("connect_db called with real production connector")


def test_unknown_endpoint_returns_exit_code_2_without_touching_settings_or_db() -> None:
    exit_code = scheduler_main.main(
        ["--endpoint", "get_totally_made_up_endpoint"],
        settings=_settings(),
        connect_db=_unreachable_connect,
        build_window_fetcher=_unreachable_build_window_fetcher,
    )

    assert exit_code == 2


def test_dry_run_lists_due_windows_without_building_a_fetcher(
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _MainFakeDB(clock=datetime(2026, 8, 22, 6, 0, 0))

    def fake_connect(dsn: str) -> _MainFakeDB:
        assert dsn == _DSN
        return db

    exit_code = scheduler_main.main(
        [
            "--dsn",
            _DSN,
            "--dry-run",
            "--endpoint",
            "get_sleep_data",
            "--history-start",
            "2026-08-20",
            "--today",
            "2026-08-22",
        ],
        settings=_settings(),
        connect_db=fake_connect,
        build_window_fetcher=_unreachable_build_window_fetcher,
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "get_sleep_data" in out
    assert "2026-08-20" in out
    assert "2026-08-22" in out
    # No ingest_run queries: dry-run must never write.
    assert all("ingest_run" not in q for q, _params in db.executed)


def test_dry_run_uses_settings_defaults_when_cli_overrides_absent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _MainFakeDB(clock=datetime(2026, 8, 22, 6, 0, 0))
    settings = _settings(history_start=date(2026, 8, 21), refresh_lookback_days=1)

    exit_code = scheduler_main.main(
        ["--dsn", _DSN, "--dry-run", "--endpoint", "get_hrv_data", "--today", "2026-08-22"],
        settings=settings,
        connect_db=lambda _dsn: db,
        build_window_fetcher=_unreachable_build_window_fetcher,
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "get_hrv_data" in out
    assert "2026-08-21" in out


def test_full_run_all_windows_succeed_returns_exit_code_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _MainFakeDB(clock=datetime(2026, 8, 22, 6, 0, 0))

    def fake_fetch_window(endpoint: str, window_date: date) -> list[FetchedWindow]:
        return [FetchedWindow(scope_key=None, payload_sha256=b"\x00" * 32)]

    def fake_build_window_fetcher(
        _settings: Settings, _conn: DBConnection, _tracker: IngestRunTracker
    ) -> WindowFetcher:
        return fake_fetch_window

    exit_code = scheduler_main.main(
        [
            "--dsn",
            _DSN,
            "--endpoint",
            "get_sleep_data",
            "--history-start",
            "2026-08-20",
            "--today",
            "2026-08-20",
        ],
        settings=_settings(),
        connect_db=lambda _dsn: db,
        build_window_fetcher=fake_build_window_fetcher,
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "get_sleep_data" in out
    assert db.closed
    # One ingest_run was properly opened then closed (status 'ok', no errors).
    assert any(q.startswith("INSERT INTO raw.ingest_run") for q, _p in db.executed)
    finish_queries = [params for q, params in db.executed if q.startswith("UPDATE raw.ingest_run")]
    assert len(finish_queries) == 1
    assert finish_queries[0][0] == "ok"


def test_full_run_with_a_failing_window_returns_exit_code_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _MainFakeDB(clock=datetime(2026, 8, 22, 6, 0, 0))

    def failing_fetch_window(endpoint: str, window_date: date) -> list[FetchedWindow]:
        raise RuntimeError("simulated Garmin failure")

    def build_failing_window_fetcher(
        _settings: Settings, _conn: DBConnection, _tracker: IngestRunTracker
    ) -> WindowFetcher:
        return failing_fetch_window

    exit_code = scheduler_main.main(
        [
            "--dsn",
            _DSN,
            "--endpoint",
            "get_sleep_data",
            "--history-start",
            "2026-08-20",
            "--today",
            "2026-08-20",
        ],
        settings=_settings(),
        connect_db=lambda _dsn: db,
        build_window_fetcher=build_failing_window_fetcher,
    )

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "simulated Garmin failure" in out
    finish_queries = [params for q, params in db.executed if q.startswith("UPDATE raw.ingest_run")]
    assert len(finish_queries) == 1
    # 'partial': since fix C3, ``_reconcile_tracker_with_summary`` closes the gap between
    # windows marked 'failed' by ``run_scheduled_poll`` and errors already counted by
    # ``IngestRunTracker.record_error``. Here the fake ``fetch_window`` raises without
    # ever hitting real ``GarminClient``: no error was counted upstream, and that's
    # exactly the gap reconciliation closes. Without it, ``raw.ingest_run`` would show 'ok'
    # for a run that lost a window -- a lasting lie, vs exit code non-zero that dies with pod.
    assert finish_queries[0][0] == "partial"


def test_invalid_settings_returns_exit_code_2(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GARMINATOR_POSTGRES_DSN",
        "GARMINTOKENS",
        "GARMINATOR_FIT_ARCHIVE_PATH",
        "GARMINATOR_GARMIN_USERNAME",
        "GARMINATOR_GARMIN_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(Path(__file__).resolve().parent)  # avoid a stray repository .env

    exit_code = scheduler_main.main(
        [],
        connect_db=_unreachable_connect,
        build_window_fetcher=_unreachable_build_window_fetcher,
    )

    assert exit_code == 2


def test_max_windows_per_endpoint_bounds_dry_run_listing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _MainFakeDB(clock=datetime(2026, 8, 25, 6, 0, 0))

    exit_code = scheduler_main.main(
        [
            "--dsn",
            _DSN,
            "--dry-run",
            "--endpoint",
            "get_sleep_data",
            "--history-start",
            "2026-08-20",
            "--today",
            "2026-08-25",
            "--max-windows-per-endpoint",
            "2",
        ],
        settings=_settings(),
        connect_db=lambda _dsn: db,
        build_window_fetcher=_unreachable_build_window_fetcher,
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "deferred to next run" in out


# --- Wiring of original-file archiving -------------------------------------------------
#
# These tests cover the JUNCTION (which identifiers, which day, which root, which
# format are passed through), not the download itself: that is already covered
# end to end by ``tests/scheduler/test_activity_files.py``. ``archive_activities_for_day``
# is therefore replaced by a spy -- there is nothing to gain from rewriting the
# other file's PVC and ``raw.fit_file`` doubles here.


class _FakeDownloader:
    """Minimal client satisfying ``OriginalFileDownloader`` (method only)."""

    def download_original(self, activity_id: str, *, dl_fmt: object, tracker: object = None) -> Any:
        raise AssertionError("not called: archive_activities_for_day is spied on")


@dataclass
class _FetcherWithClient:
    """Fetcher factice portant un ``client``, comme ``GarminWindowFetcher``."""

    client: _FakeDownloader

    def __call__(self, endpoint: str, window_date: date) -> list[FetchedWindow]:
        if endpoint != "get_activities_by_date":
            return [FetchedWindow(scope_key=None, payload_sha256=b"\x00" * 32)]
        return [
            FetchedWindow(scope_key="111", payload_sha256=b"\x01" * 32),
            FetchedWindow(scope_key="222", payload_sha256=b"\x02" * 32),
        ]


def _run_main_with_activities(
    monkeypatch: pytest.MonkeyPatch, extra_argv: list[str], *, archive_root: Path
) -> tuple[int, list[dict[str, Any]]]:
    db = _MainFakeDB(clock=datetime(2026, 8, 22, 6, 0, 0))
    calls: list[dict[str, Any]] = []

    def spy_archive(_client: object, _conn: object, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return DayArchiveSummary()

    monkeypatch.setattr(scheduler_main, "archive_activities_for_day", spy_archive)

    exit_code = scheduler_main.main(
        [
            "--dsn",
            _DSN,
            "--endpoint",
            "get_activities_by_date",
            "--history-start",
            "2026-08-20",
            "--today",
            "2026-08-20",
            *extra_argv,
        ],
        settings=_settings(fit_archive_path=archive_root),
        connect_db=lambda _dsn: db,
        build_window_fetcher=lambda *_a: _FetcherWithClient(client=_FakeDownloader()),
    )
    return exit_code, calls


def test_activity_files_are_archived_with_the_activity_ids_just_polled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exit_code, calls = _run_main_with_activities(monkeypatch, [], archive_root=tmp_path)

    assert exit_code == 0
    assert len(calls) == 1
    # The ``activityId`` come from the polling ``scope_key``: no extra request
    # is made to ask for the day's list again.
    assert list(calls[0]["activity_ids"]) == ["111", "222"]
    assert calls[0]["window_date"] == date(2026, 8, 20)
    assert calls[0]["archive_root"] == tmp_path
    assert calls[0]["dl_fmt"] is garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL
    assert calls[0]["tracker"] is not None


def test_no_activity_files_flag_skips_downloads_entirely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exit_code, calls = _run_main_with_activities(
        monkeypatch, ["--no-activity-files"], archive_root=tmp_path
    )

    assert exit_code == 0
    assert calls == []


def test_a_fetcher_without_client_skips_archiving_instead_of_improvising_a_session() -> None:
    """No Garmin session is ever rebuilt for archiving."""
    assert scheduler_main._downloader_of(lambda _e, _d: []) is None


def test_production_fetcher_still_exposes_the_client_the_wiring_relies_on() -> None:
    """Garde-fou : ``_downloader_of`` lit ``GarminWindowFetcher.client`` par nom.

    Renaming this attribute would silently disable ALL archiving -- the run
    would stay green, the .FIT files would simply stop arriving on the PVC. This test
    instantiates no transport: it inspects the declared fields of the dataclass.
    """
    fields_by_name = {f.name for f in dataclasses.fields(GarminWindowFetcher)}
    assert "client" in fields_by_name
    assert hasattr(GarminClient, "download_original")
