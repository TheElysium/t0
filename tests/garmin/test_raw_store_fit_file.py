"""Tests de :func:`garminator.garmin.raw_store.insert_fit_file` (lot L12).

``tests/garmin/fakes.py`` (existant, non modifié -- un autre lot peut en dépendre) ne
simule que les motifs de requête des lots précédents (``RETURNING id`` de
``raw.api_response``, ``RETURNING ingest_run_id``). ``raw.fit_file`` a un motif
différent (``ON CONFLICT ... DO NOTHING RETURNING fit_file_id``, plus une éventuelle
seconde requête de relecture consciente de l'activité), donc ce fichier définit sa
PROPRE doublure minimale, dédiée à ce module, plutôt que de modifier une doublure
partagée par d'autres lots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from garminator.garmin.raw_store import FitFileShaConflictError, insert_fit_file


@dataclass
class _Row:
    fit_file_id: int
    garmin_activity_id: int
    storage_path: str
    sha256: bytes
    size_bytes: int


class _FakeCursor:
    def __init__(self, conn: _FakeFitFileConnection) -> None:
        self._conn = conn
        self._result: tuple[Any, ...] | None = None

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())
        self._conn.executed.append((normalized, params))

        if normalized.startswith("INSERT INTO raw.fit_file"):
            garmin_activity_id, storage_path, sha256, size_bytes = params
            existing = next((row for row in self._conn.rows if row.sha256 == sha256), None)
            if existing is not None:
                # ON CONFLICT (sha256) DO NOTHING : aucune ligne "RETURNING" côté SQL réel.
                self._result = None
                return
            self._conn.next_id += 1
            row = _Row(
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
            existing = next((row for row in self._conn.rows if row.sha256 == sha256), None)
            self._result = (
                (existing.fit_file_id, existing.garmin_activity_id)
                if existing is not None
                else None
            )
            return

        raise AssertionError(f"_FakeCursor : requête inattendue -> {normalized}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class _FakeFitFileConnection:
    """Satisfait ``garminator.garmin.raw_store.DBConnection``, en mémoire."""

    rows: list[_Row] = field(default_factory=list)
    executed: list[tuple[str, Any]] = field(default_factory=list)
    commit_count: int = 0
    next_id: int = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1


_SHA_A = bytes(range(32))
_SHA_B = bytes(range(1, 33))


def test_insert_fit_file_writes_expected_columns_and_commits() -> None:
    conn = _FakeFitFileConnection()

    fit_file_id = insert_fit_file(
        conn,
        garmin_activity_id=12345,
        storage_path="/data/fit/2026/08/activity-12345.fit",
        sha256=_SHA_A,
        size_bytes=4096,
    )

    assert fit_file_id == 1
    assert conn.commit_count == 1
    # Une seule requête a eu lieu (pas de conflit) : l'INSERT a suffi, la relecture
    # n'a pas été déclenchée. Les valeurs passées comptent davantage que le texte SQL
    # exact -- une reformulation du SQL ne doit pas casser ce test (cf. revue).
    assert len(conn.executed) == 1
    _, params = conn.executed[0]
    garmin_activity_id, storage_path, sha256, size_bytes = params
    assert garmin_activity_id == 12345
    assert storage_path == "/data/fit/2026/08/activity-12345.fit"
    assert sha256 == _SHA_A
    assert size_bytes == 4096


def test_insert_fit_file_same_sha256_same_activity_is_idempotent() -> None:
    conn = _FakeFitFileConnection()
    first_id = insert_fit_file(
        conn,
        garmin_activity_id=12345,
        storage_path="/data/fit/2026/08/activity-12345.fit",
        sha256=_SHA_A,
        size_bytes=4096,
    )

    # Retéléchargement de la même activité : même contenu, donc même sha256. Ne doit ni
    # exploser (violation de fit_file_sha_uq) ni créer une seconde ligne.
    second_id = insert_fit_file(
        conn,
        garmin_activity_id=12345,
        storage_path="/data/fit/2026/08/activity-12345.fit",
        sha256=_SHA_A,
        size_bytes=4096,
    )

    assert second_id == first_id
    assert len(conn.rows) == 1
    # La seconde requête (SELECT de relecture après conflit) a bien eu lieu, et a bien
    # dû relire garmin_activity_id pour pouvoir comparer -- pas seulement fit_file_id.
    assert conn.executed[-1][0].startswith("SELECT fit_file_id, garmin_activity_id")


def test_insert_fit_file_different_sha256_creates_distinct_rows() -> None:
    conn = _FakeFitFileConnection()

    id_a = insert_fit_file(
        conn,
        garmin_activity_id=111,
        storage_path="/data/fit/2026/08/activity-111.fit",
        sha256=_SHA_A,
        size_bytes=100,
    )
    id_b = insert_fit_file(
        conn,
        garmin_activity_id=222,
        storage_path="/data/fit/2026/08/activity-222.fit",
        sha256=_SHA_B,
        size_bytes=200,
    )

    assert id_a != id_b
    assert len(conn.rows) == 2


def test_insert_fit_file_same_sha256_different_activity_raises_conflict() -> None:
    """BLOQUANT 2 (revue) -- écrit en premier, avant la correction, comme demandé.

    Scénario réel pour cet athlète (triathlète) : une séance brick/multisport peut
    produire deux ``garmin_activity_id`` distincts (p. ex. activité parente et enfant
    course-à-pied) dont le téléchargement « original » sert EXACTEMENT le même fichier
    -- même contenu, donc même sha256. Avant la correction, la relecture après conflit
    ne comparait que ``sha256`` et retournait l'ancien ``fit_file_id`` sans jamais
    vérifier à quelle activité il appartenait réellement : l'activité 222 se serait vu
    silencieusement attribuer une ligne pointant en réalité vers l'activité 111 --
    orphelin permanent pour 222, statut « archivé » mensonger, et aucune resynchronisation
    possible sans intervention manuelle. Ce test prouve que ce cas lève désormais
    :class:`FitFileShaConflictError` plutôt que de mentir.
    """
    conn = _FakeFitFileConnection()
    insert_fit_file(
        conn,
        garmin_activity_id=111,
        storage_path="/data/fit/2026/08/activity-111.fit",
        sha256=_SHA_A,
        size_bytes=100,
    )

    with pytest.raises(FitFileShaConflictError, match="111") as excinfo:
        insert_fit_file(
            conn,
            garmin_activity_id=222,
            storage_path="/data/fit/2026/08/activity-222.fit",
            sha256=_SHA_A,
            size_bytes=100,
        )

    # L'activité perdante ne doit apparaître nulle part en base : aucune ligne créée,
    # aucune ligne existante réattribuée -- l'orphelinat doit rester visible, pas masqué.
    assert len(conn.rows) == 1
    assert conn.rows[0].garmin_activity_id == 111
    assert "222" in str(excinfo.value)


def test_insert_fit_file_raises_if_conflict_reread_finds_nothing() -> None:
    # Cas d'incohérence délibérément forcé (jamais observable avec un vrai PostgreSQL,
    # la contrainte UNIQUE garantit que la ligne existe) : couvre le garde-fou
    # défensif de insert_fit_file plutôt que de le laisser mort.
    conn = _FakeFitFileConnection()
    conn.rows.append(
        _Row(
            fit_file_id=1,
            garmin_activity_id=1,
            storage_path="x",
            sha256=_SHA_A,
            size_bytes=1,
        )
    )

    class _VanishingCursor(_FakeCursor):
        def execute(self, query: str, params: Any = None) -> None:
            normalized = " ".join(query.split())
            if normalized.startswith("SELECT fit_file_id, garmin_activity_id"):
                self._result = None
                return
            super().execute(query, params)

    # Remplacement de la méthode liée `cursor` sur UNE instance, pour forcer la relecture
    # à échouer sans dupliquer toute la doublure `_FakeFitFileConnection`. mypy signale
    # normalement ce remplacement comme dangereux (une méthode liée n'est pas un simple
    # attribut réassignable en général) ; ici c'est sûr et local à ce test précis : le
    # `type: ignore` est donc conservé, mais justifié plutôt que laissé nu.
    conn.cursor = lambda: _VanishingCursor(conn)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="incohérence"):
        insert_fit_file(
            conn,
            garmin_activity_id=2,
            storage_path="y",
            sha256=_SHA_A,
            size_bytes=1,
        )
