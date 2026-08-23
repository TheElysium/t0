"""Tests de :mod:`garminator.scheduler.activity_files` (lot L12).

Aucun réseau, aucun PostgreSQL réel : ``_FakeDownloader`` (ci-dessous) satisfait
``activity_files.OriginalFileDownloader`` et ``_FakeFitFileDB`` satisfait
``garminator.garmin.raw_store.DBConnection``, toutes deux dédiées à ce fichier --
``tests/scheduler/fakes.py`` (existant, non modifié) simule ``raw.poll_window`` /
``raw.payload_seen``, pas ``raw.fit_file``, donc n'est pas réutilisable ici. Les octets
FIT/TCX/ZIP sont synthétiques, construits à la main comme dans ``tests/fit/test_archive.py``
-- jamais un extrait des fichiers réels de l'athlète.
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

from garminator.garmin.client import IngestRunTracker, OriginalActivityFile
from garminator.scheduler.activity_files import (
    ActivityFileOutcome,
    archive_activities_for_day,
    archive_activity_file,
)

if TYPE_CHECKING:
    from garminator.garmin.raw_store import DBConnection as _RawStoreDBConnection

# Signature FIT minimale valide, identique dans l'esprit à tests/fit/test_archive.py :
# après un en-tête de 8 octets, la chaîne ".FIT" aux octets 8-11.
_FAKE_FIT = b"\x0e\x10\xc0\x00\x00\x00\x00\x00.FIT" + b"\x00" * 20
_FAKE_FIT_V2 = (
    b"\x0e\x10\xc0\x00\x00\x00\x00\x00.FIT" + b"\x01" * 40
)  # contenu DIFFERENT, "plus complet"
_FAKE_TCX = b'<?xml version="1.0"?><TrainingCenterDatabase/>'
_GARBAGE = b"ceci n'est ni un FIT ni un TCX"

_SENTINEL_DL_FMT = object()  # tient lieu de garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _stem_suffix(content: bytes) -> str:
    """Extrait de sha256 attendu dans le nom de fichier -- voir activity_files.py."""
    return hashlib.sha256(content).hexdigest()[:12]


@dataclass
class _FakeDownloader:
    """Satisfait ``activity_files.OriginalFileDownloader``, sans réseau."""

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
            # Reproduit le comportement réel de GarminClient.download_original : le
            # tracker compte l'échec avant que l'exception ne remonte (voir client.py).
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
            # _latest_fit_file : ORDER BY downloaded_at DESC LIMIT 1 -- nos fakes n'ont
            # pas d'horodatage réel, mais l'ordre d'ajout à `rows` EST l'ordre
            # chronologique dans tous nos scénarios, donc le dernier ajouté qui
            # correspond est "la plus récente version".
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
                raise RuntimeError("panne simulée de la base pendant INSERT raw.fit_file")
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

        raise AssertionError(f"_FakeCursor : requête inattendue -> {normalized}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class _FakeFitFileDB:
    """Satisfait ``garminator.garmin.raw_store.DBConnection``, en mémoire."""

    rows: list[_FitFileRow] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    commit_count: int = 0
    next_id: int = 0
    fail_insert: bool = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1


# Satisfait structurellement raw_store.DBConnection -- vérifié une fois ici plutôt que
# de laisser mypy le redécouvrir silencieusement à chaque site d'appel.
def _typecheck_fake_db_is_dbconnection(conn: _FakeFitFileDB) -> _RawStoreDBConnection:
    return conn


_WINDOW_DATE = date(2026, 8, 20)
_TODAY_AFTER_WINDOW = date(2026, 8, 21)  # fenêtre CLOSE : window_date < today


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
    # Arborescence AAAA/MM, activité retrouvable depuis son id sans consulter la base.
    assert outcome.storage_path.parent == tmp_path / "2026" / "08"
    assert outcome.storage_path.name == f"activity-111-{_stem_suffix(_FAKE_FIT)}.fit"
    assert conn.commit_count == 1
    assert downloader.calls == [("111", _SENTINEL_DL_FMT, None)]
    # Preuve positive PVC-avant-base : au moment précis où la ligne raw.fit_file a été
    # insérée (juste après l'INSERT dans notre fake), le fichier existait déjà sur disque.
    insert_index = next(
        i for i, q in enumerate(conn.executed) if q.startswith("INSERT INTO raw.fit_file")
    )
    assert conn.executed[insert_index] is not None
    assert outcome.storage_path.exists()  # toujours vrai après coup, mais l'ordre est
    # garanti par construction : archive_activity_file n'appelle insert_fit_file qu'après
    # le retour de archive_download (voir _archive_and_insert).


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
    # Le ZIP a été désenveloppé : le contenu archivé est celui du membre FIT, pas le ZIP.
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
        today=_TODAY_AFTER_WINDOW,  # fenêtre CLOSE : window_date < today
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "already_present"
    assert outcome.fit_file_id == 42
    assert downloader.calls == []  # AUCUN appel réseau : c'est le point de ce test.


def test_archive_activity_file_same_day_unchanged_content_is_already_present(
    tmp_path: Path,
) -> None:
    # Fenêtre EN COURS (window_date == today) : le fichier EST retéléchargé, mais son
    # contenu est identique à la version déjà connue -- pas de nouvelle ligne, pas de
    # nouveau fichier, mais le réseau a bien été sollicité (contrairement au cas fenêtre
    # close ci-dessus).
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
        today=_WINDOW_DATE,  # fenêtre EN COURS
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "already_present"
    assert outcome.fit_file_id == 42
    assert outcome.storage_path == existing_path
    assert downloader.calls == [("111", _SENTINEL_DL_FMT, None)]  # le réseau A été appelé
    assert len(conn.rows) == 1  # mais rien de neuf n'a été inséré


def test_archive_activity_file_same_day_changed_content_is_refreshed(tmp_path: Path) -> None:
    # BLOQUANT 1 (revue) : une activité encore en cours d'enregistrement chez Garmin au
    # premier passage (fichier tronqué) doit être RATTRAPÉE quand la fenêtre est
    # retraitée le même jour avec un fichier désormais plus complet -- pas verrouillée
    # à vie sur la version tronquée. Avant la correction, `_existing_fit_file_id`
    # renvoyait "already_present" dès la première ligne trouvée, sans jamais comparer le
    # contenu ni retélécharger : ce test aurait échoué sur l'ancienne implémentation.
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
    conn.next_id = 42  # la ligne pré-existante ci-dessus a été insérée "avant" ce test

    outcome = archive_activity_file(
        downloader,
        conn,
        activity_id="111",
        window_date=_WINDOW_DATE,
        today=_WINDOW_DATE,  # fenêtre EN COURS : le jour même
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "refreshed"
    assert outcome.fit_file_id == 43  # nouvelle ligne, PAS 42
    assert outcome.storage_path is not None
    assert outcome.storage_path != old_path  # nom qualifié par le nouveau contenu
    assert outcome.storage_path.read_bytes() == _FAKE_FIT_V2
    # L'ANCIEN fichier n'a PAS été écrasé -- il reste lisible et intact à son chemin
    # d'origine : c'est précisément ce que le nom qualifié par sha256 doit garantir.
    assert old_path.exists()
    assert old_path.read_bytes() == _FAKE_FIT
    assert len(conn.rows) == 2


def test_archive_activity_file_zip_same_day_unchanged_content_is_already_present(
    tmp_path: Path,
) -> None:
    # RÉGRESSION (défaut réel trouvé en revue) : en production Garmin sert une ARCHIVE ZIP,
    # alors que `raw.fit_file.sha256` porte le sha du membre EXTRAIT. Le court-circuit
    # "contenu inchangé" hachait l'enveloppe : il ne pouvait donc JAMAIS coïncider, et
    # chaque activité du jour courant était réarchivée à chaque passage.
    #
    # Le ZIP servi ici porte un NOM DE MEMBRE différent de celui du premier archivage :
    # ses octets d'enveloppe diffèrent nécessairement, alors que le contenu utile est
    # identique. Seule une comparaison sur le membre extrait peut conclure.
    downloader = _FakeDownloader(outcomes={"111": _zip_bytes({"autre-nom.fit": _FAKE_FIT})})
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
        today=_WINDOW_DATE,  # fenêtre EN COURS : le retéléchargement a bien lieu
        archive_root=tmp_path,
        dl_fmt=_SENTINEL_DL_FMT,
    )

    assert outcome.status == "already_present"
    assert outcome.fit_file_id == 42
    assert len(conn.rows) == 1  # rien de neuf inséré
    assert not (tmp_path / "2026").exists()  # et RIEN de neuf écrit sur le PVC


def test_archive_activity_file_zip_same_day_changed_content_is_refreshed(tmp_path: Path) -> None:
    # Contrepartie du test précédent : le désenveloppage ne doit pas rendre la comparaison
    # aveugle aux vrais changements. Contenu réellement différent -> nouvelle version.
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
    # Le sha du NOM de fichier, celui vérifié après relecture, et celui inséré en base
    # doivent être un seul et même nombre -- sinon un passage ultérieur écrit sur le PVC
    # un fichier qu'aucune ligne `raw.fit_file` ne référence (orphelin silencieux).
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
    assert row.sha256 == hashlib.sha256(_FAKE_FIT).digest()  # le MEMBRE, pas le ZIP
    assert outcome.storage_path.name == f"activity-333-{_stem_suffix(_FAKE_FIT)}.fit"
    assert Path(row.storage_path) == outcome.storage_path  # aucun orphelin possible


def test_archive_activity_file_invalid_zip_is_failed_and_counted(tmp_path: Path) -> None:
    # L'extraction est la seule étape que `download_original` ne compte pas lui-même :
    # son échec doit incrémenter le tracker (et ne rien laisser sur le PVC).
    downloader = _FakeDownloader(outcomes={"111": b"PK pas vraiment un zip"})
    conn = _FakeFitFileDB()
    run_conn = _FakeFitFileDB()  # IngestRunTracker exige un DBConnection, non utilisé ici
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
    downloader = _FakeDownloader(outcomes={"111": RuntimeError("429 simulé")})
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
    assert outcome.error is not None and "429 simulé" in outcome.error
    assert conn.rows == []
    assert not (tmp_path / "2026").exists()  # rien n'a jamais atteint le PVC


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
    # Aucun fichier -- ni final, ni temporaire -- ne doit avoir été laissé.
    dest_dir = tmp_path / "2026" / "08"
    assert not dest_dir.exists() or list(dest_dir.iterdir()) == []


def test_pvc_before_db_insert_failure_leaves_orphan_file_on_disk(tmp_path: Path) -> None:
    # Coeur de l'invariant du lot : un échec d'écriture EN BASE, survenant APRÈS un
    # archivage réussi, ne doit jamais faire disparaître le fichier déjà écrit et
    # vérifié sur le PVC -- il devient un orphelin récupérable, jamais une perte.
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
    # Suite directe du test précédent : une fois la panne base résolue, un second passage
    # sur la MÊME activité doit retélécharger (aucune ligne en base encore), retomber sur
    # le même contenu déjà présent sur le PVC (même sha256, donc même chemin), et cette
    # fois insérer la ligne avec succès -- sans jamais dupliquer ni corrompre le fichier.
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

    conn.fail_insert = False  # la panne base est résolue avant le passage suivant
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
    # BLOQUANT 2 (revue), vu depuis activity_files.py : scénario multisport/brick où deux
    # garmin_activity_id distincts servent EXACTEMENT le même contenu original. L'activité
    # perdante ne doit ni se voir attribuer silencieusement le fit_file_id de l'autre, ni
    # planter tout le traitement du jour -- elle obtient un statut dédié, explicite.
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
    # La ligne raw.fit_file reste attribuée à l'activité gagnante, jamais réattribuée.
    assert len(conn.rows) == 1
    assert conn.rows[0].garmin_activity_id == 111


def test_authentication_error_propagates_uncaught_single_activity(tmp_path: Path) -> None:
    # IMPORTANT 1 (revue) : une authentification refusée ne doit jamais être ravalée
    # derrière un statut "failed" -- la propager est ce qui évite qu'un appelant en
    # boucle sur des dizaines d'activités ne réessaie autant de fois, schéma qui fait
    # bloquer un compte Garmin sans 2FA.
    downloader = _FakeDownloader(outcomes={"111": GarminConnectAuthenticationError("refusé")})
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
    # Même exigence, mais au niveau de archive_activities_for_day : le filet défensif
    # "except Exception" de la boucle ne doit PAS ravaler ce que
    # archive_activity_file a délibérément laissé remonter.
    downloader = _FakeDownloader(
        outcomes={"111": _FAKE_FIT, "222": GarminConnectAuthenticationError("refusé")}
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
    # La première activité, traitée avant l'échec d'authentification, a bien été
    # archivée : l'interruption survient au moment de l'erreur, pas avant.
    assert len(conn.rows) == 1


def test_tracker_records_error_on_archive_stage_failure(tmp_path: Path) -> None:
    # IMPORTANT 2 (revue) : sans ceci, un run entièrement en échec au stade archivage/
    # insertion se clôturerait quand même "ok" (ingest_run.status), car
    # IngestRunTracker.errors ne compterait que les échecs réseau déjà comptés par
    # download_original lui-même.
    downloader = _FakeDownloader(outcomes={"111": _GARBAGE})
    conn = _FakeFitFileDB()
    run_conn = _FakeFitFileDB()  # IngestRunTracker exige un DBConnection, non utilisé ici
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
    assert tracker.errors == 1  # seul l'échec de "222" compte -- "111" a réussi


def test_archive_activities_for_day_isolates_failures(tmp_path: Path) -> None:
    downloader = _FakeDownloader(
        outcomes={
            "111": _FAKE_FIT,
            "222": RuntimeError("panne réseau simulée"),
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
    # Le journal opérateur mentionne bien les trois activités, succès et échec.
    log_lines = summary.as_log_lines()
    assert len(log_lines) == 3
    assert "ÉCHEC" in log_lines[1]
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
        today=_TODAY_AFTER_WINDOW,  # fenêtre close : idempotence stricte, sans réseau
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
    duplicate = ActivityFileOutcome(activity_id="5", status="duplicate_content", error="doublon")
    failed = ActivityFileOutcome(activity_id="3", status="failed", error="boum")

    assert "archivée" in archived.as_log_line()
    assert "mise à jour" in refreshed.as_log_line()
    assert "déjà archivée" in present.as_log_line()
    assert "DUPLIQUÉ" in duplicate.as_log_line() and "doublon" in duplicate.as_log_line()
    assert "ÉCHEC" in failed.as_log_line() and "boum" in failed.as_log_line()


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
    # IMPORTANT 3 (revue) : la conversion int(activity_id) est protégée UNE fois, en tête
    # de archive_activity_file -- elle ne doit jamais lever hors de cette fonction, même
    # appelée directement (pas seulement via archive_activities_for_day).
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
    assert downloader.calls == []  # jamais atteint le réseau


def test_unexpected_exception_in_activity_id_is_isolated(tmp_path: Path) -> None:
    # "abc" n'est pas convertible en int : capturé proprement (voir test ci-dessus).
    # archive_activities_for_day doit malgré tout continuer sur les activités suivantes.
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
