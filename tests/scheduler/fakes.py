"""Doublure DB en mémoire pour les tests du lot L6 -- aucun PostgreSQL requis.

Contrairement à ``tests/garmin/fakes.py::FakeConnection`` (qui se contente d'enregistrer
les requêtes reçues), ``FakePollDB`` simule réellement l'effet des requêtes de
:mod:`garminator.scheduler.store` sur deux tables en mémoire : c'est nécessaire pour
tester l'orchestration de bout en bout de :func:`garminator.scheduler.service.
run_scheduled_poll` (reprise après panne, fenêtre glissante, déduplication) sans jamais
ouvrir de vraie connexion.

La reconnaissance des requêtes se fait par préfixe de texte normalisé (espaces
compressés), même motif que ``FakeCursor`` de ``tests/garmin/fakes.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

#: Vocabulaire accepté par ``ingest_run_mode_ck`` (migrations/002_raw.sql). Les doublures
#: qui simulent ``INSERT INTO raw.ingest_run`` DOIVENT le faire respecter : le lot L11 a
#: livré un ``mode="scheduled"`` rigoureusement inopérant en production (CheckViolation dès
#: le premier run) précisément parce que la doublure était plus permissive que le schéma.
#: ``tests/test_config_coherence.py`` vérifie que cette liste reste celle de la migration.
INGEST_RUN_MODES = frozenset({"backfill", "daily", "reprocess", "manual"})


@dataclass
class _PollWindowRow:
    status: str
    attempts: int
    last_attempted_at: datetime | None
    last_error: str | None
    last_ingest_run_id: int | None


class FakeCursor:
    def __init__(self, db: FakePollDB) -> None:
        self._db = db
        self._result: list[tuple[Any, ...]] = []

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())
        self._db.executed.append((normalized, params))

        if normalized.startswith("SELECT window_date"):
            (endpoint,) = params
            self._result = [
                (
                    win_date,
                    row.status,
                    row.attempts,
                    row.last_attempted_at,
                    row.last_error,
                )
                for (ep, win_date), row in sorted(self._db.poll_window.items())
                if ep == endpoint
            ]
            return

        if normalized.startswith("INSERT INTO raw.poll_window"):
            endpoint, window_date, ingest_run_id = params
            key = (endpoint, window_date)
            existing = self._db.poll_window.get(key)
            attempts = existing.attempts + 1 if existing is not None else 1
            self._db.poll_window[key] = _PollWindowRow(
                status="in_progress",
                attempts=attempts,
                last_attempted_at=self._db.now(),
                last_error=existing.last_error if existing is not None else None,
                last_ingest_run_id=ingest_run_id,
            )
            self._result = []
            return

        if normalized.startswith("UPDATE raw.poll_window SET status = 'done'"):
            endpoint, window_date = params
            row = self._db.poll_window[(endpoint, window_date)]
            row.status = "done"
            row.last_error = None
            self._result = []
            return

        if normalized.startswith("UPDATE raw.poll_window SET status = 'failed'"):
            error, endpoint, window_date = params
            row = self._db.poll_window[(endpoint, window_date)]
            row.status = "failed"
            row.last_error = error
            self._result = []
            return

        if normalized.startswith("SELECT 1 FROM raw.payload_seen"):
            seen_key: tuple[str, date, str, bytes] = params
            self._result = [(1,)] if seen_key in self._db.payload_seen else []
            return

        if normalized.startswith("INSERT INTO raw.payload_seen"):
            seen_key = params
            self._db.payload_seen[seen_key] = self._db.payload_seen.get(seen_key, 0) + 1
            self._result = []
            return

        raise AssertionError(f"FakeCursor : requête inattendue -> {normalized}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._result)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class FakePollDB:
    """Satisfait ``garminator.scheduler.store.DBConnection``, entièrement en mémoire."""

    poll_window: dict[tuple[str, date], _PollWindowRow] = field(default_factory=dict)
    payload_seen: dict[tuple[str, date, str, bytes], int] = field(default_factory=dict)
    executed: list[tuple[str, Any]] = field(default_factory=list)
    commit_count: int = 0
    clock: datetime = field(default_factory=lambda: datetime(2026, 8, 21, 6, 0, 0))

    def now(self) -> datetime:
        return self.clock

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1
