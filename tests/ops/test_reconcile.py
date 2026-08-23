"""Tests de :mod:`garminator.ops.reconcile` -- le scan PVC <-> ``raw.fit_file``.

Doublure de connexion minimale plutôt que ``FakePollDB`` : ce module n'exécute qu'une
seule requête, un SELECT sur ``raw.fit_file``, et n'a besoin ni de la machinerie de
fenêtres ni d'une horloge. Aucun PostgreSQL, aucun réseau ; les fichiers sont écrits dans
``tmp_path``, jamais sur un vrai volume.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from garminator.ops.reconcile import format_reconcile_report, reconcile_archive

if TYPE_CHECKING:
    from pathlib import Path

_CONTENT = b"\x0e\x10\xc0\x00\x00\x00\x00\x00.FIT" + b"\x00" * 20


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        assert "raw.fit_file" in sql  # la seule table que ce module doit lire

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    """Connexion en lecture seule : toute tentative d'écriture ferait échouer le test."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.rows)


def _row(fit_file_id: int, path: Path, content: bytes = _CONTENT) -> tuple[Any, ...]:
    return (fit_file_id, 111, str(path), hashlib.sha256(content).digest(), len(content))


def _write(path: Path, content: bytes = _CONTENT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_inventaire_coherent_est_propre(tmp_path: Path) -> None:
    archived = _write(tmp_path / "2026" / "08" / "activity-111-abc.fit")
    report = reconcile_archive(_FakeConn([_row(1, archived)]), tmp_path)

    assert report.missing_files == ()
    assert report.orphan_files == ()
    assert report.rows_scanned == 1
    assert report.is_clean is True


def test_ligne_sans_fichier_est_une_anomalie_grave(tmp_path: Path) -> None:
    # Le sens INTERDIT par la règle d'atomicité de 002_raw.sql : la ligne affirme
    # l'existence d'un fichier source qui n'est pas là.
    absent = tmp_path / "2026" / "08" / "activity-111-jamais-ecrit.fit"
    report = reconcile_archive(_FakeConn([_row(1, absent)]), tmp_path)

    assert len(report.missing_files) == 1
    assert report.missing_files[0].fit_file_id == 1
    assert "absent" in report.missing_files[0].reason
    assert report.is_clean is False


def test_fichier_sans_ligne_est_signale_mais_reste_benin(tmp_path: Path) -> None:
    # Le sens TOLÉRÉ : archivage réussi, INSERT en échec -- ou, avant la correction du
    # hachage ZIP, un fichier réécrit à chaque passage sur la fenêtre du jour courant.
    _write(tmp_path / "2026" / "08" / "activity-999-orphelin.fit")
    report = reconcile_archive(_FakeConn([]), tmp_path)

    assert len(report.orphan_files) == 1
    assert report.orphan_files[0].path.name == "activity-999-orphelin.fit"
    assert report.missing_files == ()
    # Le point du test : un orphelin ne doit PAS faire échouer le CronJob.
    assert report.is_clean is True


def test_taille_divergente_est_grave_sans_relire_le_contenu(tmp_path: Path) -> None:
    archived = _write(tmp_path / "2026" / "08" / "activity-111-abc.fit", _CONTENT + b"tronque")
    report = reconcile_archive(_FakeConn([_row(1, archived)]), tmp_path)

    assert len(report.missing_files) == 1
    assert "taille divergente" in report.missing_files[0].reason
    assert report.is_clean is False


def test_mode_rapide_ne_voit_pas_une_corruption_a_taille_constante(tmp_path: Path) -> None:
    # Justifie l'existence de --deep : même longueur, contenu différent.
    corrompu = bytearray(_CONTENT)
    corrompu[-1] = 0xFF
    archived = _write(tmp_path / "2026" / "08" / "activity-111-abc.fit", bytes(corrompu))
    conn = _FakeConn([_row(1, archived, _CONTENT)])

    assert reconcile_archive(conn, tmp_path).missing_files == ()

    deep = reconcile_archive(conn, tmp_path, deep=True)
    assert len(deep.missing_files) == 1
    assert "sha256 divergent" in deep.missing_files[0].reason
    assert deep.is_clean is False


def test_fichiers_hors_extensions_archivees_sont_ignores(tmp_path: Path) -> None:
    # Un .tmp laissé par une écriture interrompue, ou tout autre fichier du volume, ne
    # doit pas gonfler la liste des orphelins : le scan ne connaît que .fit et .tcx.
    _write(tmp_path / "2026" / "08" / ".activity-111.tmp-1234")
    report = reconcile_archive(_FakeConn([]), tmp_path)

    assert report.orphan_files == ()


def test_volume_inexistant_ne_leve_pas(tmp_path: Path) -> None:
    # PVC pas encore monté ou vide au premier démarrage : le scan doit répondre, pas
    # planter -- c'est un outil de diagnostic.
    report = reconcile_archive(_FakeConn([]), tmp_path / "jamais-cree")

    assert report.orphan_files == ()
    assert report.is_clean is True


def test_rapport_texte_nomme_le_verdict(tmp_path: Path) -> None:
    absent = tmp_path / "2026" / "08" / "activity-111-absent.fit"
    _write(tmp_path / "2026" / "08" / "activity-999-orphelin.fit")
    rendu = format_reconcile_report(reconcile_archive(_FakeConn([_row(1, absent)]), tmp_path))

    assert "ANOMALIE GRAVE" in rendu
    assert "activity-999-orphelin.fit" in rendu
    assert "AUCUNE suppression" in rendu  # le scan promet explicitement de ne rien casser
