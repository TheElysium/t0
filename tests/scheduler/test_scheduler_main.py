"""Tests du point d'entrée ``python -m garminator.scheduler`` (lot L11).

Ne construit **jamais** de vrai transport ``garminconnect`` ni de vraie connexion
PostgreSQL : ``main()`` accepte trois points d'injection (``settings``, ``connect_db``,
``build_window_fetcher``) précisément pour permettre ce genre de test -- voir le
docstring du module testé. Seuls le parsing d'arguments, le code de sortie et le mode
``--dry-run`` sont exercés ici ; l'orchestration elle-même (``run_scheduled_poll``) est
déjà testée par ``tests/scheduler/test_service.py``.
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
    """Étend le curseur de :mod:`tests.scheduler.fakes` avec les requêtes de
    ``raw.ingest_run`` -- ``main()`` ouvre lui-même un ``ingest_run(...)`` autour de
    ``run_scheduled_poll``, en plus des requêtes ``poll_window``/``payload_seen`` que
    ``FakePollDB`` sait déjà simuler. Composition plutôt que duplication : seule la
    reconnaissance des deux requêtes supplémentaires est ajoutée ici, tout le reste
    (poll_window, payload_seen) délègue au parent, éprouvé par le lot L6.
    """

    def __init__(self, db: _MainFakeDB) -> None:
        super().__init__(db)
        self._main_db = db

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())

        if normalized.startswith("INSERT INTO raw.ingest_run"):
            # La doublure doit être aussi stricte que ``ingest_run_mode_ck`` : sans cette
            # garde, elle acceptait le ``mode="scheduled"`` du lot L11, qui échouait en
            # revanche à la toute première requête en production.
            mode = params[0]
            assert mode in INGEST_RUN_MODES, (
                f"mode={mode!r} viole ingest_run_mode_ck : PostgreSQL rejetterait cet "
                f"INSERT (valeurs acceptées : {sorted(INGEST_RUN_MODES)})"
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
    """``FakePollDB`` (poll_window/payload_seen) + compteur ``ingest_run`` en mémoire :
    la seule doublure de ce fichier, réutilisée par tous les tests qui n'utilisent pas
    ``--dry-run`` (lequel ne touche jamais ``raw.ingest_run``)."""

    next_ingest_run_id: int = 0
    closed: bool = field(default=False, init=False)

    def cursor(self) -> _MainFakeCursor:
        return _MainFakeCursor(self)

    def close(self) -> None:
        self.closed = True


#: DSN passe explicitement par ``--dsn`` dans les tests. Depuis la reprise du lot,
#: ``main()`` resout le DSN comme ``garminator.ops.__main__`` (argument ou
#: ``GARMINATOR_POSTGRES_DSN``) et non plus depuis ``Settings`` : le ``--dry-run``
#: doit pouvoir tourner sans aucun secret Garmin. Les tests le fournissent donc en
#: ligne de commande, sans dependre de l'environnement de la machine qui les execute.
_DSN = "postgresql://user:pass@localhost:5432/garminator"


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "postgres_dsn": SecretStr("postgresql://user:pass@localhost:5432/garminator"),
        # ``garmin_tokens_path`` porte ``validation_alias="GARMINTOKENS"`` (settings.py) :
        # sans ``populate_by_name``, seul l'alias est accepté en construction directe.
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
    raise AssertionError(
        "build_window_fetcher appelé : --dry-run ne doit jamais construire de fetcher"
    )


def _unreachable_connect(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("connect_db appelé avec le connecteur de production réel")


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
    # Aucune requête ingest_run : le dry-run ne doit jamais écrire.
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
    # Un ingest_run a bien été ouvert puis clôturé (statut 'ok', aucune erreur).
    assert any(q.startswith("INSERT INTO raw.ingest_run") for q, _p in db.executed)
    finish_queries = [params for q, params in db.executed if q.startswith("UPDATE raw.ingest_run")]
    assert len(finish_queries) == 1
    assert finish_queries[0][0] == "ok"


def test_full_run_with_a_failing_window_returns_exit_code_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _MainFakeDB(clock=datetime(2026, 8, 22, 6, 0, 0))

    def failing_fetch_window(endpoint: str, window_date: date) -> list[FetchedWindow]:
        raise RuntimeError("panne Garmin simulée")

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
    assert "ÉCHEC" in out
    assert "panne Garmin simulée" in out
    finish_queries = [params for q, params in db.executed if q.startswith("UPDATE raw.ingest_run")]
    assert len(finish_queries) == 1
    # 'partial' : depuis la correction C3, ``_reconcile_tracker_with_summary`` comble
    # l'écart entre les fenêtres marquées 'failed' par ``run_scheduled_poll`` et les erreurs
    # déjà comptées par ``IngestRunTracker.record_error``. Ici le faux ``fetch_window`` lève
    # sans jamais passer par un ``GarminClient`` réel : aucune erreur n'a donc été comptée en
    # amont, et c'est précisément le trou que la réconciliation ferme. Sans elle,
    # ``raw.ingest_run`` afficherait 'ok' pour un run qui a perdu une fenêtre -- un mensonge
    # durable, là où le code de sortie non nul disparaît avec le pod.
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
    monkeypatch.chdir(Path(__file__).resolve().parent)  # évite un .env de dépôt fortuit

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
    assert "reportée" in out


# --- Câblage de l'archivage des fichiers originaux -------------------------------------
#
# Ces tests couvrent la JONCTION (quels identifiants, quel jour, quelle racine, quel
# format sont transmis), pas le téléchargement lui-même : celui-ci est déjà couvert de
# bout en bout par ``tests/scheduler/test_activity_files.py``. ``archive_activities_for_day``
# est donc remplacé par un espion -- il n'y a rien à gagner à réécrire ici les doublures
# de PVC et de ``raw.fit_file`` de l'autre fichier.


class _FakeDownloader:
    """Client minimal satisfaisant ``OriginalFileDownloader`` (méthode seule)."""

    def download_original(self, activity_id: str, *, dl_fmt: object, tracker: object = None) -> Any:
        raise AssertionError("non appelé : archive_activities_for_day est espionné")


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
    # Les ``activityId`` viennent des ``scope_key`` du sondage : aucune requête
    # supplémentaire n'est faite pour redemander la liste du jour.
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
    """Aucune session Garmin n'est jamais reconstruite pour l'archivage."""
    assert scheduler_main._downloader_of(lambda _e, _d: []) is None


def test_production_fetcher_still_exposes_the_client_the_wiring_relies_on() -> None:
    """Garde-fou : ``_downloader_of`` lit ``GarminWindowFetcher.client`` par nom.

    Un renommage de cet attribut désactiverait TOUT l'archivage silencieusement -- le run
    resterait vert, les fichiers .FIT cesseraient simplement d'arriver sur le PVC. Ce test
    n'instancie aucun transport : il inspecte les champs déclarés de la dataclass.
    """
    fields_by_name = {f.name for f in dataclasses.fields(GarminWindowFetcher)}
    assert "client" in fields_by_name
    assert hasattr(GarminClient, "download_original")
