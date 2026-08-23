"""Runner de migrations SQL pour garminator (lot L1).

Principes qui tiennent ce module :

1. **Autonome vis-à-vis de :mod:`garminator.settings`.** Le DSN PostgreSQL est lu
   indépendamment (variable d'environnement dédiée ou argument explicite), sans jamais
   construire l'objet :class:`~garminator.settings.Settings` complet — celui-ci exige des
   identifiants Garmin qu'une tâche de migration (ou un ``CronJob`` Kubernetes qui ne fait
   que migrer le schéma) n'a aucune raison de posséder.
2. **Avancer seulement.** Aucun rollback automatique n'est inventé : une migration appliquée
   ne peut pas être annulée par ce module. Un incident se corrige par une migration corrective
   *suivante*, jamais par une réécriture de l'historique.
3. **Une migration = une transaction.** Le fichier entier est envoyé en une seule requête
   (protocole simple de psycopg, actif tant qu'aucun paramètre n'est lié — cf. la
   documentation « Differences from psycopg2 »), donc toutes les instructions qu'il contient
   réussissent ou échouent ensemble.
4. **Refus sur dérive.** Si l'empreinte (SHA-256) d'une migration déjà appliquée a changé
   depuis son application, ou si son fichier a disparu du répertoire de migrations, le runner
   refuse de démarrer — avant même de tenter d'appliquer une migration en attente.
5. **Un seul runner à la fois.** Un verrou d'avis PostgreSQL de session
   (:data:`ADVISORY_LOCK_KEY`) est pris avant toute lecture d'état. Sans lui, deux runners
   concurrents (deux ``CronJob``, ou un déploiement pendant une migration manuelle) lisent le
   même état « en attente » et tentent d'appliquer la même migration : la seconde échoue au
   milieu du DDL, sur une violation de clé primaire de ``public.schema_migration`` ou sur un
   ``CREATE TABLE`` en doublon, dans un état partiellement appliqué. Le verrou est pris en mode
   *try* (non bloquant) : un second runner échoue immédiatement et proprement plutôt que
   d'attendre indéfiniment dans un conteneur.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg

if TYPE_CHECKING:
    from datetime import datetime

#: Journal du module. Ne consigne JAMAIS le DSN : celui-ci porte le mot de passe PostgreSQL.
logger = logging.getLogger(__name__)

#: Nom de la variable d'environnement portant le DSN PostgreSQL utilisé par ce runner.
#: Choisi identique au nom de variable que lit ``garminator.settings.Settings`` (préfixe
#: ``GARMINATOR_``) pour qu'un opérateur n'ait qu'une seule valeur à définir, mais ce module
#: la lit lui-même, sans jamais importer ni construire ``Settings``.
DSN_ENV_VAR = "GARMINATOR_POSTGRES_DSN"

#: Répertoire de migrations par défaut : ``<racine du dépôt>/migrations``.
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

#: Table de suivi des migrations appliquées. Volontairement dans ``public`` (schéma toujours
#: présent, y compris avant que la migration 001 ne crée ``raw``/``core``/``mart``) plutôt que
#: dans l'un des schémas applicatifs.
_TRACKING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migration (
    version     integer      PRIMARY KEY,
    name        text         NOT NULL,
    checksum    text         NOT NULL,
    applied_at  timestamptz  NOT NULL DEFAULT now()
);
"""

_FILENAME_RE = re.compile(r"^(?P<version>\d+)_(?P<name>.+)\.sql$")

#: Clé du verrou d'avis PostgreSQL protégeant l'ensemble de la séquence de migration.
#: Valeur arbitraire mais STABLE : deux processus ne se coordonnent que s'ils emploient la même.
#: Portée session (``pg_advisory_lock`` et non ``pg_advisory_xact_lock``) car le runner
#: enchaîne plusieurs transactions — une par migration — et le verrou doit couvrir la séquence
#: entière, pas chaque transaction isolément.
ADVISORY_LOCK_KEY = 4_071_128_301_055_216_401


class MigrationError(RuntimeError):
    """Erreur générique du runner de migrations."""


class MigrationDriftError(MigrationError):
    """Une migration déjà appliquée a une empreinte différente du fichier sur disque.

    Levée *avant* toute tentative d'appliquer une migration en attente : le runner refuse
    de démarrer tant que la dérive n'est pas résolue par un humain (fichier corrigé,
    ou incident documenté).
    """


class MigrationLockError(MigrationError):
    """Un autre runner de migrations détient déjà le verrou d'avis.

    Levée immédiatement, sans attente : un second processus ne doit ni appliquer les mêmes
    migrations en parallèle, ni rester bloqué indéfiniment dans un conteneur.
    """


@dataclass(frozen=True, slots=True)
class Migration:
    """Une migration versionnée telle que trouvée sur disque."""

    version: int
    name: str
    path: Path
    checksum: str

    @property
    def sql(self) -> str:
        """Contenu SQL complet du fichier, lu à chaque accès (pas de cache en mémoire)."""
        return self.path.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """Une ligne de ``public.schema_migration``."""

    version: int
    name: str
    checksum: str
    applied_at: datetime


def compute_checksum(path: Path) -> str:
    """Empreinte SHA-256 (hex) des octets bruts du fichier."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_migrations(migrations_dir: Path = DEFAULT_MIGRATIONS_DIR) -> list[Migration]:
    """Liste les migrations du répertoire, triées par numéro de version.

    Les fichiers doivent suivre le motif ``NNN_nom.sql`` (``NNN`` numérique, zéro-préfixé
    ou non). Tout fichier ``.sql`` qui ne suit pas ce motif est ignoré silencieusement (il
    n'appartient pas à la chaîne de migrations : par exemple un fixture de test).

    Raises:
        MigrationError: si deux fichiers déclarent le même numéro de version.
    """
    migrations: dict[int, Migration] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if match is None:
            continue
        version = int(match.group("version"))
        if version in migrations:
            raise MigrationError(
                f"Version de migration {version} présente deux fois : "
                f"{migrations[version].path.name} et {path.name}."
            )
        migrations[version] = Migration(
            version=version,
            name=match.group("name"),
            path=path,
            checksum=compute_checksum(path),
        )
    return [migrations[v] for v in sorted(migrations)]


def detect_drift(migrations: list[Migration], applied: list[AppliedMigration]) -> list[str]:
    """Compare les migrations appliquées en base aux fichiers présents sur disque.

    Retourne la liste des messages de dérive (vide si tout concorde). Une dérive couvre
    deux cas : l'empreinte a changé, ou le fichier d'une migration appliquée a disparu du
    répertoire — dans les deux cas on ne peut plus garantir que ce qui a été appliqué
    correspond à ce que le dépôt affirme aujourd'hui.
    """
    by_version = {m.version: m for m in migrations}
    issues: list[str] = []
    for row in applied:
        current = by_version.get(row.version)
        if current is None:
            issues.append(
                f"Migration {row.version} ({row.name}) appliquée en base le "
                f"{row.applied_at.isoformat()} mais son fichier a disparu du répertoire "
                "de migrations."
            )
        elif current.checksum != row.checksum:
            issues.append(
                f"Migration {row.version} ({row.name}) : empreinte modifiée depuis son "
                f"application ({row.checksum[:12]}… en base, {current.checksum[:12]}… sur "
                f"disque). Une migration appliquée ne doit plus être modifiée : corrigez "
                "par une nouvelle migration."
            )
    return issues


def plan_pending(migrations: list[Migration], applied_versions: set[int]) -> list[Migration]:
    """Migrations non encore appliquées, dans l'ordre où elles doivent l'être."""
    return [m for m in migrations if m.version not in applied_versions]


def resolve_dsn(dsn: str | None) -> str:
    """Résout le DSN à utiliser : argument explicite, sinon variable d'environnement.

    Ne lit jamais ``garminator.settings`` : ce module doit pouvoir fonctionner sur une
    machine où seul PostgreSQL est configuré (pas d'identifiants Garmin).

    Raises:
        MigrationError: si aucun DSN n'est disponible par aucune des deux voies.
    """
    if dsn:
        return dsn
    value = os.environ.get(DSN_ENV_VAR)
    if not value:
        raise MigrationError(
            f"Aucun DSN PostgreSQL fourni : passez --dsn, ou définissez {DSN_ENV_VAR}."
        )
    return value


def connect(dsn: str) -> psycopg.Connection[tuple[Any, ...]]:
    """Ouvre une connexion psycopg 3 en mode transactionnel explicite (autocommit=False)."""
    return psycopg.connect(dsn, autocommit=False)


def acquire_lock(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    """Prend le verrou d'avis de session, ou échoue immédiatement s'il est déjà détenu.

    Raises:
        MigrationLockError: si un autre runner détient le verrou.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s);", (ADVISORY_LOCK_KEY,))
        row = cur.fetchone()
    conn.commit()
    if row is None or not row[0]:
        raise MigrationLockError(
            "Un autre runner de migrations détient déjà le verrou d'avis "
            f"{ADVISORY_LOCK_KEY} sur cette base. Aucune migration n'a été appliquée."
        )


def release_lock(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    """Relâche le verrou d'avis. Sans effet si le verrou n'est pas détenu.

    Appelée depuis un ``finally`` : elle ne doit jamais masquer l'exception d'origine. La
    transaction courante est d'abord annulée (après une migration en échec, la connexion peut
    être en état « transaction avortée », où toute commande échouerait), et un échec de
    déverrouillage est ignoré — la fermeture de la connexion relâche de toute façon le verrou
    de session côté serveur.
    """
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s);", (ADVISORY_LOCK_KEY,))
        conn.commit()
    except psycopg.Error:
        logger.warning(
            "Déverrouillage du verrou d'avis %s impossible ; il sera relâché à la fermeture "
            "de la connexion.",
            ADVISORY_LOCK_KEY,
        )


def ensure_tracking_table(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    """Crée ``public.schema_migration`` si elle n'existe pas, dans sa propre transaction."""
    with conn.cursor() as cur:
        cur.execute(_TRACKING_TABLE_DDL)
    conn.commit()


def fetch_applied(conn: psycopg.Connection[tuple[Any, ...]]) -> list[AppliedMigration]:
    """Lit l'état actuel de ``public.schema_migration``, trié par version."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, name, checksum, applied_at FROM public.schema_migration "
            "ORDER BY version;"
        )
        rows = cur.fetchall()
    return [AppliedMigration(version=v, name=n, checksum=c, applied_at=a) for v, n, c, a in rows]


def apply_migration(conn: psycopg.Connection[tuple[Any, ...]], migration: Migration) -> None:
    """Applique une migration dans sa propre transaction (BEGIN implicite / COMMIT / ROLLBACK).

    Le fichier entier est envoyé sans paramètres liés : psycopg 3 utilise alors le protocole
    simple et exécute toutes les instructions du fichier, séparées par ``;``, comme un seul
    lot — c'est ce qui donne « une migration = une transaction » sans découpage manuel des
    instructions.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(migration.sql)
            cur.execute(
                "INSERT INTO public.schema_migration (version, name, checksum) "
                "VALUES (%s, %s, %s);",
                (migration.version, migration.name, migration.checksum),
            )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def migrate(
    dsn: str | None = None, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> list[Migration]:
    """Applique toutes les migrations en attente, dans l'ordre, après vérification de dérive.

    Returns:
        La liste des migrations effectivement appliquées lors de cet appel (vide si la base
        était déjà à jour — c'est ce qui rend une ré-exécution « zéro changement »).

    Raises:
        MigrationLockError: si un autre runner détient déjà le verrou d'avis. Rien n'est
            appliqué : le second processus échoue immédiatement au lieu d'attendre.
        MigrationDriftError: si une migration déjà appliquée ne correspond plus au fichier
            sur disque. Rien n'est appliqué dans ce cas : le refus est total, pas partiel.
    """
    resolved_dsn = resolve_dsn(dsn)
    migrations = discover_migrations(migrations_dir)
    conn = connect(resolved_dsn)
    try:
        acquire_lock(conn)
        try:
            ensure_tracking_table(conn)
            applied = fetch_applied(conn)
            drift = detect_drift(migrations, applied)
            if drift:
                raise MigrationDriftError("Le runner refuse de démarrer : " + " | ".join(drift))
            applied_versions = {row.version for row in applied}
            pending = plan_pending(migrations, applied_versions)
            for migration in pending:
                apply_migration(conn, migration)
            return pending
        finally:
            release_lock(conn)
    finally:
        conn.close()


@dataclass(frozen=True, slots=True)
class Status:
    """État courant des migrations, pour la commande ``status``."""

    applied: list[AppliedMigration]
    pending: list[Migration]
    drift: list[str]


def status(dsn: str | None = None, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR) -> Status:
    """Rapporte l'état courant sans rien appliquer ni modifier."""
    resolved_dsn = resolve_dsn(dsn)
    migrations = discover_migrations(migrations_dir)
    conn = connect(resolved_dsn)
    try:
        ensure_tracking_table(conn)
        applied = fetch_applied(conn)
        drift = detect_drift(migrations, applied)
        applied_versions = {row.version for row in applied}
        pending = plan_pending(migrations, applied_versions)
        return Status(applied=applied, pending=pending, drift=drift)
    finally:
        conn.close()


def format_status(report: Status) -> str:
    """Rendu texte simple de :class:`Status`, utilisé par la CLI."""
    lines = ["Migrations appliquées :"]
    if report.applied:
        for row in report.applied:
            lines.append(f"  [{row.version:03d}] {row.name} — {row.applied_at.isoformat()}")
    else:
        lines.append("  (aucune)")
    lines.append("Migrations en attente :")
    if report.pending:
        for m in report.pending:
            lines.append(f"  [{m.version:03d}] {m.name}")
    else:
        lines.append("  (aucune)")
    if report.drift:
        lines.append("Dérives détectées :")
        for issue in report.drift:
            lines.append(f"  ! {issue}")
    return "\n".join(lines)
