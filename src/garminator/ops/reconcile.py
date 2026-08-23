"""Réconciliation PVC <-> ``raw.fit_file`` : le scan que le DDL promet, en lecture seule.

``migrations/002_raw.sql`` pose la règle d'atomicité (le fichier est écrit et fsync sur le
PVC AVANT l'INSERT de sa ligne) et qualifie la seule anomalie tolérée : « Un fichier
orphelin (sans ligne) est inoffensif et rattrapable par un scan ; une ligne orpheline
(sans fichier) rend la reconstructibilité de core fausse. » Ce scan n'existait pas. Ce
module EST ce scan.

Les deux anomalies ne sont pas symétriques, et ce module ne les traite pas de la même
façon :

- **Fichier orphelin** (sur le PVC, aucune ligne) -- bénin. Se produit légitimement quand
  un archivage a réussi et que l'INSERT qui suit a échoué : la règle d'atomicité a
  précisément pour but de rendre CE sens-là possible plutôt que l'autre. Se produisait
  aussi, jusqu'à la correction du hachage ZIP dans
  :mod:`garminator.scheduler.activity_files`, à chaque passage sur la fenêtre du jour
  courant -- un déploiement antérieur à cette correction en a très probablement accumulé.
  Rien à réparer en base : le fichier est simplement inutilisé.
- **Ligne orpheline** (en base, aucun fichier) -- grave. La ligne affirme qu'un fichier
  source existe ; toute reconstruction de ``core`` à partir de ``raw`` s'appuiera dessus
  et échouera, ou pire, sautera silencieusement l'activité.
- **Taille divergente** -- le fichier existe mais ne fait plus la taille inscrite en base.
  Traité comme grave : c'est soit une troncature, soit un fichier écrasé, dans les deux
  cas la ligne ne décrit plus son fichier.

Ce module ne recalcule PAS les sha256 par défaut : sur ~1 090 fichiers de plusieurs Mo,
relire l'intégralité du PVC pour un contrôle de routine coûte cher pour une anomalie que
la comparaison de taille attrape déjà dans l'écrasante majorité des cas. ``--deep`` force
la vérification par contenu quand on soupçonne une corruption silencieuse.

**Lecture seule, sans exception.** Aucune fonction d'ici ne supprime de fichier ni ne
modifie de ligne. Supprimer un « orphelin » sur la foi d'un scan est irréversible et
suppose que le scan a raison ; le rapport nomme les fichiers, l'opérateur décide.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    import psycopg

#: Extensions des fichiers source archivés (cf. ``garminator.fit.archive.sniff_format``).
#: Tout autre fichier du PVC est ignoré par le scan plutôt que rapporté orphelin : le
#: volume peut légitimement porter des fichiers temporaires ou des répertoires vides.
ARCHIVE_SUFFIXES: frozenset[str] = frozenset({".fit", ".tcx"})

#: Lecture par blocs pour le sha256 en mode ``--deep`` : un .FIT d'ultra peut peser
#: plusieurs dizaines de Mo, et le scan les parcourt tous.
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class MissingFile:
    """Ligne ``raw.fit_file`` dont le fichier est absent, ou ne correspond plus."""

    fit_file_id: int
    garmin_activity_id: int | None
    storage_path: str
    reason: str


@dataclass(frozen=True, slots=True)
class OrphanFile:
    """Fichier présent sur le PVC qu'aucune ligne ``raw.fit_file`` ne référence."""

    path: Path
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Résultat d'un scan. ``is_clean`` distingue le bénin du grave."""

    rows_scanned: int
    files_scanned: int
    missing_files: tuple[MissingFile, ...]
    orphan_files: tuple[OrphanFile, ...]
    deep: bool

    @property
    def is_clean(self) -> bool:
        """Vrai si aucune anomalie GRAVE. Les fichiers orphelins n'en sont pas une.

        C'est cette propriété que le code de sortie du CLI suit : faire échouer un CronJob
        parce que le PVC porte un fichier inutilisé entraînerait à ignorer ses alertes,
        et c'est l'alerte « ligne orpheline » qu'on ne peut pas se permettre d'ignorer.
        """
        return not self.missing_files


def _file_sha256(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.digest()


def _iter_archive_files(archive_root: Path) -> Iterable[Path]:
    if not archive_root.exists():
        return ()
    return (
        path
        for path in archive_root.rglob("*")
        if path.is_file() and path.suffix.lower() in ARCHIVE_SUFFIXES
    )


def reconcile_archive(
    conn: psycopg.Connection,
    archive_root: Path,
    *,
    deep: bool = False,
) -> ReconcileReport:
    """Compare l'inventaire de ``raw.fit_file`` au contenu réel de ``archive_root``.

    Args:
        conn: connexion PostgreSQL. Utilisée en LECTURE seule.
        archive_root: racine du volume d'archivage (le ``fit-archive`` du PVC).
        deep: recalcule le sha256 de chaque fichier présent au lieu de comparer sa taille.
            Coûteux : relit l'intégralité du volume.

    Returns:
        Le rapport. Un chemin en base est résolu tel quel : les lignes portent un chemin
        absolu, et un ``archive_root`` monté ailleurs qu'à l'écriture ferait rapporter
        TOUTES les lignes manquantes -- symptôme volontairement bruyant plutôt que
        silencieux, un PVC monté au mauvais endroit étant exactement ce qu'il faut voir.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fit_file_id, garmin_activity_id, storage_path, sha256, size_bytes "
            "FROM raw.fit_file ORDER BY fit_file_id"
        )
        rows = cur.fetchall()

    missing: list[MissingFile] = []
    referenced: set[Path] = set()

    for fit_file_id, activity_id, storage_path, sha256, size_bytes in rows:
        path = Path(storage_path)
        referenced.add(path)
        if not path.is_file():
            missing.append(
                MissingFile(fit_file_id, activity_id, storage_path, "fichier absent du volume")
            )
            continue
        if deep:
            try:
                actual = _file_sha256(path)
            except OSError as exc:
                missing.append(
                    MissingFile(fit_file_id, activity_id, storage_path, f"illisible : {exc}")
                )
                continue
            if actual != bytes(sha256):
                missing.append(
                    MissingFile(
                        fit_file_id,
                        activity_id,
                        storage_path,
                        f"sha256 divergent (base {bytes(sha256).hex()[:12]}, "
                        f"disque {actual.hex()[:12]})",
                    )
                )
            continue
        actual_size = path.stat().st_size
        if actual_size != size_bytes:
            missing.append(
                MissingFile(
                    fit_file_id,
                    activity_id,
                    storage_path,
                    f"taille divergente (base {size_bytes}, disque {actual_size})",
                )
            )

    orphans = tuple(
        OrphanFile(path, path.stat().st_size)
        for path in sorted(_iter_archive_files(archive_root))
        if path not in referenced
    )

    return ReconcileReport(
        rows_scanned=len(rows),
        files_scanned=len(orphans) + (len(rows) - len(missing)),
        missing_files=tuple(missing),
        orphan_files=orphans,
        deep=deep,
    )


#: Même borne d'affichage que le bilan de santé, pour la même raison : un rapport qui
#: déroule des centaines de lignes dans les logs d'un CronJob cesse d'être lu.
_MAX_LIGNES_LISTEES = 20


def format_reconcile_report(report: ReconcileReport) -> str:
    """Rendu texte, même esprit que :func:`garminator.ops.health.format_health_report`."""
    mode = "profond (sha256 recalculé)" if report.deep else "rapide (taille comparée)"
    lines = [
        f"Réconciliation PVC <-> raw.fit_file -- mode {mode}",
        f"  lignes examinées : {report.rows_scanned}",
        "",
        f"GRAVE -- lignes sans fichier valide ({len(report.missing_files)}) :",
    ]
    if not report.missing_files:
        lines.append("  (aucune -- toute ligne raw.fit_file a bien son fichier)")
    for mf in report.missing_files[:_MAX_LIGNES_LISTEES]:
        activity = mf.garmin_activity_id if mf.garmin_activity_id is not None else "upload manuel"
        lines.append(f"  #{mf.fit_file_id} (activité {activity}) {mf.storage_path} : {mf.reason}")
    if len(report.missing_files) > _MAX_LIGNES_LISTEES:
        lines.append(f"  ... et {len(report.missing_files) - _MAX_LIGNES_LISTEES} de plus.")

    lines.append("")
    lines.append(f"BÉNIN -- fichiers orphelins sur le volume ({len(report.orphan_files)}) :")
    if not report.orphan_files:
        lines.append("  (aucun)")
    for of in report.orphan_files[:_MAX_LIGNES_LISTEES]:
        lines.append(f"  {of.path} ({of.size_bytes} octets)")
    if len(report.orphan_files) > _MAX_LIGNES_LISTEES:
        lines.append(f"  ... et {len(report.orphan_files) - _MAX_LIGNES_LISTEES} de plus.")
    if report.orphan_files:
        lines.append(
            "  Ces fichiers ne sont référencés par aucune ligne : inoffensifs, ils "
            "n'occupent que de l'espace. AUCUNE suppression n'est faite par ce scan."
        )

    lines.append("")
    lines.append("Verdict : " + ("RAS" if report.is_clean else "ANOMALIE GRAVE, voir ci-dessus"))
    return "\n".join(lines)
