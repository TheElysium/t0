"""Tests statiques du lot L1 : ne nécessitent aucun PostgreSQL, aucun réseau.

Deux familles de vérifications :

1. Propriétés pures du runner (:mod:`garminator.db.runner`) : découverte de migrations,
   empreinte, détection de dérive, planification — testées sur des fichiers ``.sql``
   jetables dans ``tmp_path``.
2. Régressions sur le contenu réel de ``migrations/`` du dépôt : nommage, absence de
   doublon de version, présence de ``ON CONFLICT DO NOTHING`` sur les seeds de
   référentiels (exigence explicite du lot, absente du DDL figé lui-même).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from garminator.db.runner import (
    ADVISORY_LOCK_KEY,
    AppliedMigration,
    MigrationError,
    MigrationLockError,
    acquire_lock,
    compute_checksum,
    detect_drift,
    discover_migrations,
    plan_pending,
    release_lock,
)

if TYPE_CHECKING:
    from datetime import datetime

    from garminator.db.runner import Migration

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
THRESHOLDS_FIXTURE = FIXTURES_DIR / "thresholds_example.sql"


def _write(tmp_path: Path, filename: str, content: str = "SELECT 1;") -> Path:
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    return path


# --- Propriétés pures -------------------------------------------------------------


def test_discover_migrations_orders_by_numeric_version(tmp_path: Path) -> None:
    _write(tmp_path, "002_second.sql")
    _write(tmp_path, "010_tenth.sql")
    _write(tmp_path, "001_first.sql")

    migrations = discover_migrations(tmp_path)

    assert [m.version for m in migrations] == [1, 2, 10]
    assert [m.name for m in migrations] == ["first", "second", "tenth"]


def test_discover_migrations_ignores_files_not_matching_the_pattern(tmp_path: Path) -> None:
    _write(tmp_path, "001_schemas.sql")
    _write(tmp_path, "README.sql")  # pas de préfixe numérique
    _write(tmp_path, "notes.txt")

    migrations = discover_migrations(tmp_path)

    assert [m.name for m in migrations] == ["schemas"]


def test_discover_migrations_rejects_duplicate_version(tmp_path: Path) -> None:
    _write(tmp_path, "001_a.sql")
    _write(tmp_path, "001_b.sql")

    with pytest.raises(MigrationError, match="présente deux fois"):
        discover_migrations(tmp_path)


def test_checksum_is_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    path_a = _write(tmp_path, "001_a.sql", "SELECT 1;")
    path_b = _write(tmp_path, "002_b.sql", "SELECT 1;")
    path_c = _write(tmp_path, "003_c.sql", "SELECT 2;")

    assert compute_checksum(path_a) == compute_checksum(path_b)
    assert compute_checksum(path_a) != compute_checksum(path_c)
    # stable d'un appel à l'autre
    assert compute_checksum(path_a) == compute_checksum(path_a)


def test_detect_drift_is_empty_when_everything_matches(tmp_path: Path) -> None:
    path = _write(tmp_path, "001_schemas.sql")
    migrations = discover_migrations(tmp_path)
    applied = [
        AppliedMigration(
            version=1,
            name="schemas",
            checksum=compute_checksum(path),
            applied_at=_now(),
        )
    ]

    assert detect_drift(migrations, applied) == []


def test_detect_drift_flags_a_modified_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "001_schemas.sql", "SELECT 1;")
    original_checksum = compute_checksum(path)
    # une migration appliquée ne devrait jamais être modifiée -- on simule l'incident
    path.write_text("SELECT 2;", encoding="utf-8")
    migrations = discover_migrations(tmp_path)
    applied = [
        AppliedMigration(version=1, name="schemas", checksum=original_checksum, applied_at=_now())
    ]

    issues = detect_drift(migrations, applied)

    assert len(issues) == 1
    assert "1" in issues[0]
    assert "empreinte modifiée" in issues[0]


def test_detect_drift_flags_a_deleted_file(tmp_path: Path) -> None:
    migrations: list[Migration] = []  # rien sur disque
    applied = [AppliedMigration(version=1, name="schemas", checksum="deadbeef", applied_at=_now())]

    issues = detect_drift(migrations, applied)

    assert len(issues) == 1
    assert "disparu" in issues[0]


def test_plan_pending_excludes_already_applied_versions(tmp_path: Path) -> None:
    _write(tmp_path, "001_a.sql")
    _write(tmp_path, "002_b.sql")
    _write(tmp_path, "003_c.sql")
    migrations = discover_migrations(tmp_path)

    pending = plan_pending(migrations, applied_versions={1})

    assert [m.version for m in pending] == [2, 3]


def test_plan_pending_is_empty_when_everything_is_applied(tmp_path: Path) -> None:
    _write(tmp_path, "001_a.sql")
    migrations = discover_migrations(tmp_path)

    pending = plan_pending(migrations, applied_versions={1})

    assert pending == []


def _now() -> datetime:
    from datetime import UTC, datetime

    return datetime.now(UTC)


# --- Régressions sur le contenu réel de migrations/ -------------------------------


EXPECTED_MIGRATION_FILES = [
    "001_schemas.sql",
    "002_raw.sql",
    "003_core_ref.sql",
    "004_core_activity.sql",
    "005_core_series.sql",
    "006_core_wellness.sql",
    "007_mart.sql",
    "008_mart_activity_load_fix.sql",
    "009_scheduler.sql",
    "010_raw_fit_file_sha_comment.sql",
    "011_arbitrage_intensity_factor.sql",
    "012_raw_fit_file_manual_upload.sql",
    "013_threshold_power_source_home_trainer.sql",
]


def test_repo_migrations_directory_matches_the_expected_split() -> None:
    """Verrouille la liste EXACTE des migrations du depot, et leur contiguite depuis 1.

    Volontairement sans enumeration commentee des migrations : une docstring qui les liste
    perime a chaque ajout, et une docstring perimee ment plus qu'elle n'informe. Chaque
    fichier porte son propre POURQUOI dans son en-tete, qui lui ne peut pas diverger.
    """
    migrations = discover_migrations(REPO_MIGRATIONS_DIR)

    assert [m.path.name for m in migrations] == EXPECTED_MIGRATION_FILES
    assert [m.version for m in migrations] == list(range(1, len(EXPECTED_MIGRATION_FILES) + 1))


def test_repo_migrations_have_no_checksum_collision() -> None:
    migrations = discover_migrations(REPO_MIGRATIONS_DIR)
    checksums = [m.checksum for m in migrations]

    assert len(checksums) == len(set(checksums))


@pytest.mark.parametrize(
    "filename",
    ["003_core_ref.sql", "006_core_wellness.sql"],
)
def test_seed_inserts_are_idempotent(filename: str) -> None:
    """Exigence explicite du lot : les seeds doivent être réapplicables sans effet.

    Le DDL figé lui-même (research/05-schema-postgres.md §3) n'a pas cette clause sur ses
    INSERT de seed -- c'est un ajout volontaire et documenté de ce lot, vérifié ici par
    régression : chaque INSERT INTO d'un fichier de seed doit être suivi (avant le point-virgule
    de fin) par ON CONFLICT DO NOTHING.
    """
    content = (REPO_MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
    insert_blocks = re.findall(r"INSERT INTO.*?;", content, flags=re.S)

    assert insert_blocks, f"aucun INSERT trouvé dans {filename}"
    for block in insert_blocks:
        assert (
            "ON CONFLICT DO NOTHING" in block
        ), f"seed non idempotent dans {filename} :\n{block[:120]}..."


def test_all_31_tables_and_views_are_commented_in_the_concatenated_migrations() -> None:
    """Contrôle de couverture des COMMENT ON, sans dépendre de pglast (pas une dépendance
    d'exécution du lot) : comptage textuel des CREATE TABLE / CREATE MATERIALIZED VIEW d'un
    côté, des COMMENT ON TABLE / COMMENT ON MATERIALIZED VIEW de l'autre.

    31 objets du DDL figé (research/05-schema-postgres.md) + 1 (raw.poll_window, lot L6,
    migration 009) = 32.
    """
    full_text = "\n".join(
        (REPO_MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        for name in EXPECTED_MIGRATION_FILES
    )
    created = re.findall(r"CREATE (?:TABLE|MATERIALIZED VIEW)\s+([a-z_]+\.[a-z_]+)", full_text)
    commented = re.findall(
        r"COMMENT ON (?:TABLE|MATERIALIZED VIEW)\s+([a-z_]+\.[a-z_]+)", full_text
    )

    assert len(set(created)) == 32, f"attendu 32 objets, trouvé {len(set(created))} : {created}"
    missing = set(created) - set(commented)
    assert not missing, f"objets créés sans COMMENT ON correspondant : {missing}"


# --- La fixture de seuils n'est pas une migration ----------------------------------


def test_thresholds_fixture_is_never_discovered_as_a_migration(tmp_path: Path) -> None:
    """Point non négociable : la fixture de seuils ne doit JAMAIS être appliquée automatiquement.

    Trois angles, parce que « le nom ne correspond pas au motif » se vérifie par l'exécution et
    non par la lecture :
    1. le répertoire migrations/ du dépôt ne contient que les 7 migrations attendues ;
    2. scanner le répertoire de fixtures ne découvre rien ;
    3. même *copiée dans* un répertoire de migrations, la fixture reste ignorée -- c'est le
       nom de fichier qui décide, pas l'emplacement.
    """
    assert THRESHOLDS_FIXTURE.is_file()
    assert [m.path.name for m in discover_migrations(REPO_MIGRATIONS_DIR)] == (
        EXPECTED_MIGRATION_FILES
    )
    assert discover_migrations(FIXTURES_DIR) == []

    (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / THRESHOLDS_FIXTURE.name).write_text(
        THRESHOLDS_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert [m.path.name for m in discover_migrations(tmp_path)] == ["001_first.sql"]


def test_thresholds_fixture_leaves_rftp_null_and_matches_the_communicated_values() -> None:
    """Régression sur les valeurs de seuils : un seuil faux ne lève aucune erreur.

    Il produit des nombres plausibles et faux sur tout l'historique de charge. Les valeurs
    proviennent de l'addendum §5 (communiquées par l'athlète le 2026-08-19, la LTHR le
    2026-08-21) ; rftp_watts doit rester NULL -- ni dérivé du FTP vélo, ni estimé depuis
    l'allure seuil.

    La borne basse de validité est le **2026-05-19** : rétrodatage de trois mois tranché par
    l'athlète le 2026-08-21. C'est une hypothèse assumée, pas une mesure ; la changer déplace
    silencieusement le début de toute la charge d'entraînement, d'où cette garde.
    """
    content = THRESHOLDS_FIXTURE.read_text(encoding="utf-8")
    statements = re.findall(r"INSERT INTO.*?;", content, flags=re.S)
    by_discipline = {
        d: next(b for b in statements if f"'{d}'," in b) for d in ("bike", "run", "swim", "all")
    }

    assert "220, 190, 55, 70.0," in by_discipline["bike"]
    # 4:30 min/km = 270 s/km ; rFTP explicitement NULL dans la liste de valeurs.
    assert "270, NULL, 190, 55, 70.0," in by_discipline["run"]
    # 1:50 min/100 m = 1,000000 m/s, mesurée (css_estimated = false).
    assert "0.909091, false, 190, 55, 70.0," in by_discipline["swim"]
    # LTHR 170 bpm, portée par la ligne transversale : condition d'existence du hrTSS.
    assert "170, 190, 55, 70.0," in by_discipline["all"]
    for block in by_discipline.values():
        assert "daterange('2026-05-19', NULL, '[)')" in block
    assert not re.search(
        r"rftp_watts\s*(=|,)?\s*\d", content
    ), "rftp_watts doit rester NULL : aucune valeur numérique ne doit lui être affectée"


def test_thresholds_fixture_athlete_insert_is_really_idempotent() -> None:
    """core.athlete n'a AUCUNE contrainte unique ni d'exclusion dans le DDL figé.

    ``ON CONFLICT DO NOTHING`` y serait donc inopérant : chaque exécution ajouterait un athlète
    de plus, et les INSERT de seuils (qui résolvent athlete_id par display_name) créeraient un
    seuil par doublon. L'idempotence doit passer par ``WHERE NOT EXISTS``.
    """
    content = THRESHOLDS_FIXTURE.read_text(encoding="utf-8")
    athlete_insert = next(
        b for b in re.findall(r"INSERT INTO.*?;", content, flags=re.S) if "core.athlete (" in b
    )

    assert "WHERE NOT EXISTS" in athlete_insert
    assert "ON CONFLICT" not in athlete_insert
    schema_ddl = (REPO_MIGRATIONS_DIR / "003_core_ref.sql").read_text(encoding="utf-8")
    athlete_ddl = re.search(
        r"CREATE TABLE core\.athlete\s*\(.*?\n\);",
        schema_ddl,
        flags=re.S,
    )
    assert athlete_ddl is not None
    assert "UNIQUE" not in athlete_ddl.group(
        0
    ), "core.athlete a gagné une contrainte unique : ce test et la fixture sont à revoir"


# --- Verrou d'avis ----------------------------------------------------------------


class _FakeCursor:
    def __init__(self, result: bool | None) -> None:
        self._result = result
        self.executed: list[tuple[str, object]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[bool] | None:
        return None if self._result is None else (self._result,)


class _FakeConnection:
    def __init__(self, result: bool | None) -> None:
        self.cursor_obj = _FakeCursor(result)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_acquire_lock_uses_a_non_blocking_session_scoped_lock() -> None:
    """Non bloquant (``pg_try_advisory_lock``) : un second runner dans un conteneur doit
    échouer tout de suite, pas attendre indéfiniment. Portée session et non transaction :
    le runner enchaîne une transaction par migration, le verrou doit couvrir la séquence.
    """
    conn = _FakeConnection(result=True)

    acquire_lock(cast("Any", conn))

    sql, params = conn.cursor_obj.executed[0]
    assert "pg_try_advisory_lock" in sql
    assert "pg_advisory_xact_lock" not in sql
    assert params == (ADVISORY_LOCK_KEY,)


def test_acquire_lock_raises_when_another_runner_holds_it() -> None:
    conn = _FakeConnection(result=False)

    with pytest.raises(MigrationLockError):
        acquire_lock(cast("Any", conn))


def test_release_lock_unlocks_the_same_key() -> None:
    conn = _FakeConnection(result=True)

    release_lock(cast("Any", conn))

    # rollback() d'abord : après une migration en échec la connexion est en transaction
    # avortée, où toute commande -- y compris le déverrouillage -- échouerait.
    assert conn.rollbacks == 1
    sql, params = conn.cursor_obj.executed[0]
    assert "pg_advisory_unlock" in sql
    assert params == (ADVISORY_LOCK_KEY,)


def test_migration_008_corrige_les_trois_defauts_de_activity_load() -> None:
    """Garde sur les trois corrections de la migration 008.

    Elles sont invisibles à l'exécution : un ``hr_tss`` NULL et un ``intensity_factor``
    NULL ne lèvent aucune erreur, ils vident silencieusement des colonnes entières. Si
    quelqu'un recrée un jour ``mart.activity_load`` sans ces deux points, rien ne
    protestera -- d'où ce test.
    """
    sql = (REPO_MIGRATIONS_DIR / "008_mart_activity_load_fix.sql").read_text(encoding="utf-8")

    # Défaut 1 : la LTHR n'existe que sur la ligne de seuil transversale.
    assert "th_all.discipline = 'all'" in sql
    assert "coalesce(th.lthr_bpm, th_all.lthr_bpm)" in sql

    # Défaut 2 : sans branche GAP, l'IF course est NULL tant que rftp_watts l'est.
    assert "r.ngp_speed_mps * r.ftp_pace_s_per_km / 1000.0" in sql
    assert "'run_gap'" in sql
    # La branche puissance reste PRIORITAIRE : le jour où un rFTP validé arrive, il gagne.
    assert sql.index("'run_power'") < sql.index("'run_gap'")

    # Défaut 3 : la cascade est LUE dans core.metric_method_priority, jamais recopiée.
    # Sans ce garde-fou, une branche GAP appliquée à toute la discipline `run` produirait
    # sur `treadmill` un IF que la matrice de priorité, elle, n'autorise pas.
    assert "FROM core.metric_method_priority p" in sql
    assert "p.metric = 'tss'" in sql
    for method in ("bike_power", "run_power", "run_gap", "swim_css"):
        assert re.search(rf"'{method}' +=+ ANY\(b\.allowed_methods\)", sql), method

    # La valeur et la méthode ne peuvent pas diverger : un seul CASE les résout, le second
    # se contente de brancher SUR ce résultat.
    assert "CASE r.intensity_factor_method" in sql

    # La migration 007 est figée : 008 corrige en remplaçant la vue, sans la réécrire.
    sql_007 = (REPO_MIGRATIONS_DIR / "007_mart.sql").read_text(encoding="utf-8")
    assert "th_all" not in sql_007
    assert "DROP MATERIALIZED VIEW mart.activity_load;" in sql


def _sql_sans_commentaires_ni_litteraux(sql: str) -> str:
    """Retire les commentaires ``--`` et le contenu des littéraux ``'...'``.

    Les lignes sont conservées (aucune n'est supprimée) pour que les messages d'erreur
    restent lisibles. Les quotes doublées ``''`` à l'intérieur d'un littéral sont gérées
    naturellement : elles ferment puis rouvrent le littéral, ce qui laisse le contenu
    neutralisé dans les deux cas.
    """
    lignes: list[str] = []
    dans_litteral = False
    for ligne in sql.splitlines():
        sortie: list[str] = []
        i = 0
        while i < len(ligne):
            car = ligne[i]
            if dans_litteral:
                if car == "'":
                    dans_litteral = False
                    sortie.append("'")
                i += 1
                continue
            if car == "'":
                dans_litteral = True
                sortie.append("'")
                i += 1
                continue
            if ligne.startswith("--", i):
                break
            sortie.append(car)
            i += 1
        lignes.append("".join(sortie))
    return chr(10).join(lignes)


def test_migration_008_nentraine_aucune_vue_dependante() -> None:
    """``DROP MATERIALIZED VIEW`` sans CASCADE échouerait si une autre vue lisait
    ``mart.activity_load``. Vérifié : daily_load, pmc, activity_wide et power_curve_best
    lisent toutes ``core.*``, jamais ``mart.activity_load``. Ce test échoue si une future
    migration crée cette dépendance sans adapter 008."""
    others = [
        p for p in REPO_MIGRATIONS_DIR.glob("*.sql") if p.name != "008_mart_activity_load_fix.sql"
    ]
    for path in others:
        # On cherche une dépendance SQL RÉELLE, pas une mention. Commentaires `--` et
        # littéraux entre quotes (les COMMENT ON, qui citent volontiers les vues voisines
        # pour expliquer un arbitrage) sont donc neutralisés AVANT la recherche : sans
        # cela, documenter mart.activity_load fait échouer un test qui parle de
        # dépendances de vues, et le réflexe devient de retirer la documentation.
        code = _sql_sans_commentaires_ni_litteraux(path.read_text(encoding="utf-8"))
        reads = [
            line
            for line in code.splitlines()
            if "mart.activity_load" in line
            and "CREATE MATERIALIZED VIEW" not in line
            and "CREATE UNIQUE INDEX" not in line
            and "COMMENT ON" not in line
        ]
        assert not reads, f"{path.name} lit mart.activity_load : {reads}"


def test_la_garde_de_dependance_ignore_bien_commentaires_et_litteraux() -> None:
    """Contre-épreuve du neutraliseur : il doit taire la prose ET voir le vrai SELECT.

    Une garde qui se contente d'ignorer les commentaires sans plus rien voir passerait
    inaperçue — elle ne dirait plus jamais rien.
    """
    prose = "-- mart.activity_load est cité ici\nSELECT 1;\n"
    litteral = "COMMENT ON VIEW x IS 'voir mart.activity_load pour l''audit';\n"
    vrai = "SELECT a.* FROM mart.activity_load a;\n"

    assert "mart.activity_load" not in _sql_sans_commentaires_ni_litteraux(prose)
    assert "mart.activity_load" not in _sql_sans_commentaires_ni_litteraux(litteral)
    assert "mart.activity_load" in _sql_sans_commentaires_ni_litteraux(vrai)
