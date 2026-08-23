"""Tests statiques du lot L9 : dashboards Grafana (deploy/helm/garminator/dashboards/*.json).

Aucun réseau, aucun Grafana, aucun PostgreSQL réel. Trois familles de vérifications,
au minimum demandées par le lot :

1. Chaque fichier JSON est parsable et possède les clés structurantes attendues
   (``uid``, ``title``, ``panels``, forme de chaque panneau).
2. Toutes les requêtes SQL brutes des dashboards (panneaux, annotations, variables de
   templating) sont extraites, puis chaque table/vue ``schema.objet`` et chaque colonne
   ``alias.colonne`` référencées sont confrontées à un référentiel de colonnes dérivé
   des migrations.
3. Aucun ``uid`` de dashboard n'est dupliqué.

Construction du référentiel de colonnes (choix documenté, cf. discussion du lot) :

- Pour les tables ``core.*`` : extraction AUTOMATIQUE par une regex sur les blocs
  ``CREATE TABLE core.xxx ( ... );`` des migrations 003 à 006. C'est fiable ici car ces
  blocs sont du DDL plat, une colonne par ligne, sans sous-requête.
- Pour les vues matérialisées ``mart.*`` : PAS d'extraction générique, car leur SELECT
  final est un empilement de CTE et l'aplatissement automatique des alias de sortie
  serait plus fragile que la lecture humaine. On utilise donc une liste EXPLICITE des
  colonnes de sortie, recopiée à la main depuis la définition ``CREATE MATERIALIZED VIEW``
  de migrations/007_mart.sql, telle que redéfinie par les migrations ultérieures :
  008 pour mart.activity_load, 011 pour mart.activity_wide (chacune DROP puis recrée sa
  vue, et c'est la DERNIÈRE définition qui fait foi, jamais celle de 007). Un test dédié
  (``test_les_colonnes_mart_attendues_existent_reellement_dans_les_migrations``) vérifie que
  cette liste à la main n'a pas divergé du nom des vues réellement créées par les
  migrations, pour limiter le risque de désynchronisation silencieuse.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
DASHBOARDS_DIR = REPO_ROOT / "deploy" / "helm" / "garminator" / "dashboards"

DASHBOARD_FILES = sorted(DASHBOARDS_DIR.glob("*.json")) if DASHBOARDS_DIR.exists() else []

# --- Référentiel de colonnes -------------------------------------------------------

_CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE\s+(?P<schema>\w+)\.(?P<table>\w+)\s*\((?P<body>.*?)\n\);",
    re.DOTALL,
)
# Une ligne de colonne : démarre par un identifiant, suivie d'un type. On exclut les
# lignes de contrainte de table (PRIMARY KEY (...), UNIQUE (...), CHECK (...), FOREIGN
# KEY (...), CONSTRAINT ...) qui ne déclarent pas de nouvelle colonne.
_COLUMN_LINE_RE = re.compile(r"^\s*(?P<col>[a-z_][a-z0-9_]*)\s+\S")
_CONSTRAINT_KEYWORDS = (
    "PRIMARY KEY",
    "UNIQUE",
    "CHECK",
    "FOREIGN KEY",
    "CONSTRAINT",
    "EXCLUDE",
)


def _parse_core_tables() -> dict[str, set[str]]:
    """Extrait {"core.table": {colonnes...}} depuis les CREATE TABLE de 003 à 006."""
    tables: dict[str, set[str]] = {}
    for filename in (
        "003_core_ref.sql",
        "004_core_activity.sql",
        "005_core_series.sql",
        "006_core_wellness.sql",
    ):
        text = (MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
        for match in _CREATE_TABLE_RE.finditer(text):
            full_name = f"{match.group('schema')}.{match.group('table')}"
            columns: set[str] = set()
            # On coupe sur les virgules de haut niveau (aucune de nos définitions de
            # colonnes ne contient de parenthèses imbriquées non refermées sur la même
            # ligne significative, sauf DEFAULT/CHECK inline qu'on neutralise via la
            # détection de mot-clé de contrainte plutôt qu'un vrai parseur SQL).
            for raw_line in match.group("body").splitlines():
                line = raw_line.strip().rstrip(",")
                if not line:
                    continue
                upper = line.upper()
                if any(upper.startswith(kw) for kw in _CONSTRAINT_KEYWORDS):
                    continue
                col_match = _COLUMN_LINE_RE.match(line)
                if col_match:
                    columns.add(col_match.group("col"))
            tables[full_name] = columns
    return tables


CORE_TABLES = _parse_core_tables()

# Colonnes de sortie des vues mart.*, recopiées à la main depuis migrations/007_mart.sql
# et migrations/008_mart_activity_load_fix.sql (008 fait foi pour mart.activity_load).
MART_VIEWS: dict[str, set[str]] = {
    "mart.pmc": {
        "athlete_id",
        "local_date",
        "tss",
        "ctl",
        "atl",
        "tsb",
        "tsb_same_day",
        "ctl_reliable",
    },
    "mart.daily_load": {
        "athlete_id",
        "local_date",
        "tss",
        "n_activities",
        "duration_s",
        "has_estimated_load",
    },
    "mart.activity_load": {
        "activity_id",
        "athlete_id",
        "sport_code",
        "discipline",
        "local_date",
        "threshold_id",
        "data_quality",
        "needs_manual_rpe",
        "intensity_factor",
        "intensity_factor_method",
        "hr_tss",
        "hr_seconds",
    },
    "mart.power_curve_best": {
        "kind",
        "duration_s",
        "value",
        "activity_id",
        "local_date",
    },
    "mart.hr_zone_time": {
        # PAS d'athlete_id : la vue ne le projette pas (elle ne s'en sert qu'en jointure
        # interne sur core.threshold). Un dashboard qui écrirait z.athlete_id doit être
        # REJETÉ par ce test, pas autorisé par un référentiel trop généreux.
        "activity_id",
        "local_date",
        "sport_code",
        "zone_code",
        "zone_order",
        "seconds",
    },
    "mart.activity_wide": {
        "activity_id",
        "local_date",
        "start_time_local",
        "sport_code",
        "sport",
        "name",
        "duration_s",
        "distance_m",
        "avg_hr",
        "max_hr",
        "avg_power",
        "np_watts",
        "np_reliable",
        "ascent_m",
        "data_quality",
        "needs_manual_rpe",
        "tss",
        "tss_method",
        "tss_hr",
        "trimp",
        "intensity_factor",
        # Ajoutée par 011, pendant de tss_method : un IF issu du GAP ne s'interprète pas
        # comme un IF issu de la puissance.
        "intensity_factor_method",
        "decoupling_pct",
        "efficiency_factor",
    },
}

KNOWN_TABLES: dict[str, set[str]] = {**CORE_TABLES, **MART_VIEWS}


# --- Extraction des requêtes SQL des dashboards -------------------------------------


def _iter_sql_strings(node: Any) -> list[str]:
    """Retourne toutes les chaînes portées par les clés rawSql/query/definition.

    On descend récursivement dans tout le document JSON (panels, targets, annotations,
    templating.list) : c'est plus robuste qu'une liste de chemins figés si un panneau
    ajoute un niveau d'imbrication (ex. panels de type row contenant des sous-panels).
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("rawSql", "query", "definition") and isinstance(value, str):
                # "query" est aussi utilisé par les variables "custom" (liste statique,
                # pas du SQL) : on ne garde que les chaînes qui ressemblent à du SQL.
                if key != "query" or re.search(r"\bSELECT\b", value, re.IGNORECASE):
                    found.append(value)
            else:
                found.extend(_iter_sql_strings(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_iter_sql_strings(item))
    return found


_TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?P<schema>core|mart)\.(?P<table>\w+)(?:\s+(?:AS\s+)?(?P<alias>[a-zA-Z_]\w*))?",
    re.IGNORECASE,
)
# alias.colonne, en excluant les faux positifs "core.xxx"/"mart.xxx" déjà couverts
# ci-dessus (le schéma ne fait jamais office d'alias de colonne dans nos dashboards).
_COLUMN_REF_RE = re.compile(r"\b(?P<alias>[a-zA-Z_]\w*)\.(?P<col>[a-zA-Z_]\w*)\b")
_SQL_KEYWORDS_AS_ALIAS = {"core", "mart"}
# Fonctions/pseudo-colonnes PostgreSQL qui apparaissent en position "alias.col" dans nos
# requêtes sans être des colonnes de table (aucune ici, gardé pour extension future).
_NON_COLUMN_SUFFIXES: set[str] = set()


def _substitute_grafana_macros(sql: str) -> str:
    """Remplace les macros Grafana non-SQL par un équivalent SQL analysable.

    $__timeFilter(col) n'est pas du SQL valide : c'est un texte substitué côté Grafana
    avant envoi à Postgres. On le remplace par une comparaison triviale équivalente pour
    les usages qui appellent un vrai parseur SQL (pglast) ; l'extraction alias/colonne
    par regex ci-dessus n'en a pas besoin (elle voit `col` directement dans l'argument).
    """
    sql = re.sub(r"\$__timeFilter\(([^)]*)\)", r"(\1 >= now() AND \1 <= now())", sql)
    sql = re.sub(r"\$\w+", "'x'", sql)  # variables de templating ($athlete_id, $kind...)
    return sql


def _check_query_against_schema(sql: str) -> list[str]:
    """Retourne la liste des erreurs (table ou colonne inconnue) trouvées dans `sql`."""
    errors: list[str] = []
    alias_to_table: dict[str, str] = {}
    for m in _TABLE_REF_RE.finditer(sql):
        full_name = f"{m.group('schema')}.{m.group('table')}"
        if full_name not in KNOWN_TABLES:
            errors.append(f"table/vue inconnue référencée : {full_name}")
            continue
        alias = m.group("alias") or m.group("table")
        alias_to_table[alias] = full_name

    for m in _COLUMN_REF_RE.finditer(sql):
        alias = m.group("alias")
        col = m.group("col")
        if alias in _SQL_KEYWORDS_AS_ALIAS:
            continue  # déjà couvert par _TABLE_REF_RE (c'est un schéma, pas un alias)
        if alias not in alias_to_table:
            continue  # ce n'est pas un alias de table connu de cette requête (bruit)
        table = alias_to_table[alias]
        if col in _NON_COLUMN_SUFFIXES:
            continue
        if col not in KNOWN_TABLES[table]:
            errors.append(f"colonne inconnue {alias}.{col} (table {table})")
    return errors


# --- Tests : structure des fichiers --------------------------------------------------


def test_le_dossier_dashboards_contient_des_fichiers() -> None:
    assert DASHBOARD_FILES, f"aucun fichier .json trouvé sous {DASHBOARDS_DIR}"


@pytest.mark.parametrize("path", DASHBOARD_FILES, ids=lambda p: p.name)
def test_chaque_dashboard_est_un_json_valide_avec_les_cles_attendues(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))

    assert isinstance(data.get("uid"), str) and data["uid"], f"{path.name} : uid manquant"
    assert isinstance(data.get("title"), str) and data["title"], f"{path.name} : title manquant"
    assert (
        isinstance(data.get("panels"), list) and data["panels"]
    ), f"{path.name} : panels doit être une liste non vide"

    for panel in data["panels"]:
        assert "id" in panel, f"{path.name} : un panneau n'a pas d'id"
        assert "type" in panel, f"{path.name} : panneau {panel.get('id')} sans type"
        if panel["type"] == "text":
            continue  # panneau texte/markdown : pas de gridPos/targets obligatoires ici
        assert "gridPos" in panel, f"{path.name} : panneau {panel.get('id')} sans gridPos"
        assert panel.get("targets"), f"{path.name} : panneau {panel.get('id')} sans targets"
        for target in panel["targets"]:
            assert target.get(
                "rawSql"
            ), f"{path.name} : panneau {panel.get('id')} a un target sans rawSql"
            assert target.get("format") in (
                "time_series",
                "table",
            ), f"{path.name} : panneau {panel.get('id')} a un format inattendu"


@pytest.mark.parametrize("path", DASHBOARD_FILES, ids=lambda p: p.name)
def test_chaque_requete_sql_du_dashboard_ne_reference_que_des_objets_connus(
    path: Path,
) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    sql_strings = _iter_sql_strings(data)
    assert sql_strings, f"{path.name} : aucune requête SQL trouvée (inattendu)"

    all_errors: list[str] = []
    for sql in sql_strings:
        for err in _check_query_against_schema(sql):
            all_errors.append(f"{err}\n  requête : {sql}")

    assert not all_errors, f"{path.name} :\n" + "\n".join(all_errors)


def test_aucun_uid_de_dashboard_nest_duplique() -> None:
    uids = [json.loads(p.read_text(encoding="utf-8"))["uid"] for p in DASHBOARD_FILES]
    assert len(uids) == len(set(uids)), f"uids dupliqués parmi {uids}"


def _projection_finale(view_name: str, migrations_text: str) -> str:
    """Retourne la LISTE DE PROJECTION du SELECT final de la vue `view_name`.

    Ni regex ni pglast ne peuvent donner les colonnes de sortie d'une vue empilant des
    CTE sans catalogue. Mais on n'a pas besoin de tout résoudre : il suffit d'isoler le
    SELECT final — dans nos migrations, le seul dont le mot-clé est en COLONNE 0, ceux
    des CTE étant systématiquement indentés — et de regarder les identifiants qu'il
    projette. Une colonne de sortie y figure forcément, soit par son nom, soit par son
    alias AS.
    """
    bloc_re = re.compile(
        rf"CREATE MATERIALIZED VIEW\s+{re.escape(view_name)}\s+AS(?P<corps>.*?);\s*$",
        re.DOTALL | re.MULTILINE | re.IGNORECASE,
    )
    match = bloc_re.search(migrations_text)
    assert match, f"aucune définition trouvée pour {view_name}"
    corps = match.group("corps")

    debuts = [m.start() for m in re.finditer(r"^SELECT", corps, re.MULTILINE)]
    assert debuts, f"{view_name} : aucun SELECT en colonne 0 (mise en forme inattendue)"
    final = corps[debuts[-1] :]

    fin = re.search(r"^\s*FROM", final, re.MULTILINE)
    return final[: fin.start()] if fin else final


def test_les_colonnes_mart_attendues_existent_reellement_dans_les_migrations() -> None:
    """Confronte MART_VIEWS, recopié à la main, aux définitions réelles des vues.

    Vérifier seulement que les NOMS de vues existent ne suffisait pas : le référentiel
    a d'abord attribué un athlete_id à mart.hr_zone_time, que cette vue ne projette pas.
    Une colonne fantôme dans le référentiel est pire que pas de référentiel du tout —
    elle AUTORISE la requête cassée que ce fichier est censé interdire, et le panneau
    échoue à l'affichage, chez l'athlète, des mois plus tard.

    Le sens inverse (colonne réellement projetée mais absente de MART_VIEWS) n'est pas
    vérifié : il est inoffensif, car il fait échouer le test à la première utilisation.
    """
    # Balayage de TOUTES les migrations, dans l'ordre des versions, et non des seules
    # 007/008 : une vue redéfinie plus tard (011 pour mart.activity_wide) doit être lue
    # dans sa DERNIÈRE définition. Coder en dur les deux fichiers d'origine aurait fait
    # valider le référentiel contre une version périmée de la vue, donc rendu la garde
    # muette sur exactement le cas qu'elle existe pour attraper.
    sources = {
        path.name: path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
    }

    definitions: dict[str, str] = {}
    for _, text in sorted(sources.items()):
        for view_name in re.findall(
            r"CREATE (?:OR REPLACE )?MATERIALIZED VIEW\s+(mart\.\w+)", text, re.IGNORECASE
        ):
            definitions[view_name] = text

    for view_name, colonnes in MART_VIEWS.items():
        assert view_name in definitions, (
            f"{view_name} listé dans MART_VIEWS mais aucun CREATE MATERIALIZED VIEW "
            "correspondant trouvé dans les migrations : la liste à la main a divergé"
        )
        # On retient le CREATE le plus récent, jamais une simple mention : les migrations
        # citent d'autres vues dans leurs commentaires, et s'y fier ferait chercher une
        # définition dans un fichier qui n'en contient pas.
        source = definitions[view_name]
        projection = _projection_finale(view_name, source)
        identifiants = set(re.findall(r"[a-z_][a-z0-9_]*", projection))
        manquantes = sorted(c for c in colonnes if c not in identifiants)
        assert not manquantes, (
            f"{view_name} : {manquantes} figure(nt) dans MART_VIEWS mais n'apparaî(ssen)t "
            f"pas dans la projection finale de la vue. "
            f"Projection lue : {projection}"
        )


def test_la_garde_des_colonnes_mart_detecte_vraiment_une_colonne_fantome() -> None:
    """Contre-épreuve : sans elle, on ne saurait pas si la garde ci-dessus mord.

    On rejoue sa logique sur une colonne délibérément inventée. Si l'extraction de la
    projection finale cassait (regex trop permissive, mauvais SELECT retenu), elle
    accepterait n'importe quoi et la garde deviendrait décorative sans que rien n'échoue.
    """
    text_007 = (MIGRATIONS_DIR / "007_mart.sql").read_text(encoding="utf-8")
    projection = _projection_finale("mart.hr_zone_time", text_007)
    identifiants = set(re.findall(r"[a-z_][a-z0-9_]*", projection))

    assert "zone_code" in identifiants  # réellement projetée
    assert "athlete_id" not in identifiants  # utilisée en jointure, JAMAIS projetée
    assert "colonne_totalement_inventee" not in identifiants


def test_le_referentiel_core_nest_pas_vide() -> None:
    """Garde-fou : si la regex DDL casse silencieusement (0 table extraite), le test
    de vérification des colonnes ne détecterait plus rien — on le rend explicite."""
    assert len(CORE_TABLES) >= 15, f"seulement {len(CORE_TABLES)} tables core.* extraites"


def test_les_macros_grafana_sont_substituees_avant_toute_analyse_sql_stricte() -> None:
    """Documente et vérifie le remplacement de $__timeFilter avant un usage pglast.

    $__timeFilter(col) n'est pas du SQL standard : c'est une macro texte de Grafana.
    Ce test fige le comportement de _substitute_grafana_macros, utilisé par quiconque
    voudrait plus tard valider la syntaxe des requêtes de ce dépôt avec pglast (import
    pglast ; pglast.parse_sql(...)) sans faire échouer le parseur sur ces macros.
    """
    raw = (
        "SELECT 1 FROM mart.pmc p "
        "WHERE $__timeFilter(p.local_date) AND p.athlete_id = $athlete_id"
    )
    substituted = _substitute_grafana_macros(raw)

    assert "$__timeFilter" not in substituted
    assert "$athlete_id" not in substituted

    # pglast n'est PAS une dépendance de .venv (interdiction d'installer un paquet pour
    # ce lot) : il n'est disponible que via l'interpréteur global `python3.11` mentionné
    # dans le brief. On importe donc en best-effort et on saute si absent plutôt que
    # d'échouer — la substitution elle-même est déjà vérifiée ci-dessus sans pglast.
    pglast = pytest.importorskip("pglast", reason="pglast n'est disponible que hors .venv")
    pglast.parse_sql(substituted)  # ne doit pas lever
