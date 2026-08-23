"""Garde-fous sur les valeurs dupliquées entre le code, le SQL et le chart Helm.

Ce fichier ne teste aucun comportement : il teste des *accords*. Chaque constante
vérifiée ici existe volontairement à plusieurs endroits (pour éviter des dépendances
absurdes entre modules, ou parce qu'un chart Helm ne peut pas importer du Python), et
chaque divergence silencieuse s'est déjà payée au moins une fois :

- ``mode="scheduled"`` (lot L11) violait ``ingest_run_mode_ck`` : rigoureusement
  inopérant en production, invisible en test car la doublure était plus permissive que
  le schéma ;
- une date de début d'historique divergente entre le scheduler et
  ``ops backfill-status`` fait comparer deux périmètres différents, donc signale des
  trous qui n'en sont pas -- ou pire, en masque de vrais.
"""

from __future__ import annotations

import re
from pathlib import Path

from garminator.logging_setup import LOG_LEVEL_ENV_VAR
from garminator.ops.backfill import DEFAULT_HISTORY_START_ISO
from garminator.scheduler.catalog import DEFAULT_ENDPOINTS
from garminator.settings import Settings
from garminator.transform.runner import DEFAULT_BATCH_LIMIT
from tests.scheduler.fakes import INGEST_RUN_MODES

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VALUES_YAML = _REPO_ROOT / "deploy" / "helm" / "garminator" / "values.yaml"
_MIGRATION_RAW = _REPO_ROOT / "migrations" / "002_raw.sql"
_INGEST_CONFIGMAP = (
    _REPO_ROOT / "deploy" / "helm" / "garminator" / "templates" / "ingest-configmap.yaml"
)
_TEMPLATES = _REPO_ROOT / "deploy" / "helm" / "garminator" / "templates"

#: Variables ``GARMINATOR_*`` de la ConfigMap qui ne correspondent volontairement à
#: aucun champ de :class:`Settings`, avec la raison. Toute autre clé préfixée qui ne
#: correspond à rien est un réglage mort, et le test ci-dessous la refuse.
_HORS_SETTINGS: dict[str, str] = {
    LOG_LEVEL_ENV_VAR: (
        "lu par garminator.logging_setup, délibérément hors Settings : régler la "
        "verbosité ne doit pas exiger un identifiant Garmin ni un DSN"
    ),
}


def test_ingest_run_modes_double_matches_the_sql_check_constraint() -> None:
    """``INGEST_RUN_MODES`` doit être exactement le vocabulaire de la migration."""
    sql = _MIGRATION_RAW.read_text(encoding="utf-8")
    match = re.search(r"ingest_run_mode_ck\s+CHECK\s*\(\s*mode\s+IN\s*\(([^)]*)\)", sql)
    assert match is not None, "contrainte ingest_run_mode_ck introuvable dans 002_raw.sql"

    declared = set(re.findall(r"'([^']+)'", match.group(1)))
    assert declared == set(INGEST_RUN_MODES), (
        "Le vocabulaire des doublures de test a divergé du schéma : "
        f"SQL={sorted(declared)} vs tests={sorted(INGEST_RUN_MODES)}"
    )


def test_history_start_agrees_across_settings_ops_and_helm() -> None:
    """Les trois déclarations du premier jour d'historique doivent coïncider."""
    settings_default = Settings.model_fields["history_start"].default
    assert settings_default.isoformat() == DEFAULT_HISTORY_START_ISO

    values = _VALUES_YAML.read_text(encoding="utf-8")
    helm_values = re.findall(
        r"^\s*(?:historyStart|backfillStartDate):\s*\"?([\d-]+)\"?\s*$", values, re.M
    )
    assert helm_values, "aucune date d'historique trouvée dans values.yaml"
    assert set(helm_values) == {DEFAULT_HISTORY_START_ISO}, (
        "values.yaml a divergé du code : "
        f"Helm={sorted(set(helm_values))} vs code={DEFAULT_HISTORY_START_ISO}"
    )


def _helm_endpoint_lists(values: str) -> dict[str, list[str]]:
    """Extrait les listes ``endpoints:`` de values.yaml, par bloc de premier niveau.

    Lecture par expression régulière plutôt que par un parseur YAML : cela évite
    d'ajouter PyYAML aux dépendances du projet pour ce seul test. Le format visé est
    celui, très contraint, du fichier versionné ici -- une clé ``endpoints:`` suivie
    d'éléments ``- nom`` indentés.
    """
    lists: dict[str, list[str]] = {}
    current_block: str | None = None
    collecting = False
    for line in values.splitlines():
        top_level = re.match(r"^([A-Za-z][\w]*):\s*$", line)
        if top_level:
            current_block = top_level.group(1)
            collecting = False
            continue
        if re.match(r"^\s+endpoints:\s*$", line):
            collecting = current_block is not None
            if collecting and current_block is not None:
                lists[current_block] = []
            continue
        if collecting:
            item = re.match(r"^\s+-\s+(\S+)\s*$", line)
            if item and current_block is not None:
                lists[current_block].append(item.group(1))
            elif line.strip() and not line.strip().startswith("#"):
                collecting = False
    return lists


def test_helm_endpoint_lists_match_the_code_catalog() -> None:
    """``scheduler`` et ``opsBackfill`` doivent sonder et auditer le MÊME périmètre.

    Un endpoint présent côté bilan mais absent côté sondage produit un retard éternel
    (« ces fenêtres ne seront jamais dues ») ; l'inverse masque un vrai trou. Et les deux
    doivent suivre ``DEFAULT_ENDPOINTS``, qui reste la source de vérité.
    """
    lists = _helm_endpoint_lists(_VALUES_YAML.read_text(encoding="utf-8"))

    assert set(lists) >= {
        "scheduler",
        "opsBackfill",
    }, f"blocs endpoints trouvés dans values.yaml : {sorted(lists)}"
    for block, endpoints in lists.items():
        assert endpoints == list(DEFAULT_ENDPOINTS), (
            f"values.yaml/{block}.endpoints a divergé de DEFAULT_ENDPOINTS : "
            f"Helm={endpoints} vs code={list(DEFAULT_ENDPOINTS)}"
        )


def test_every_prefixed_configmap_key_is_actually_read_by_the_code() -> None:
    """Une clé ``GARMINATOR_*`` du chart doit correspondre à un champ ``Settings``.

    DÉFAUT RÉEL. La ConfigMap exposait ``LOG_LEVEL`` sans préfixe, juste sous un
    commentaire expliquant qu'une clé sans préfixe est ignorée par ``Settings``. Aucun
    code ne l'a jamais lue : ``ingest.config.logLevel`` était un bouton débranché, et
    l'opérateur qui le tournait pour obtenir plus de détails n'obtenait rien, sans le
    moindre message. Ce test rend ce genre de réglage mort impossible à réintroduire.
    """
    text = _INGEST_CONFIGMAP.read_text(encoding="utf-8")
    keys = {
        m.group(1)
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        if (m := re.match(r"^\s{2}(GARMINATOR_[A-Z0-9_]+):", line))
    }
    assert keys, "aucune clé GARMINATOR_* trouvée dans la ConfigMap d'ingestion"

    connus = {f"GARMINATOR_{name.upper()}" for name in Settings.model_fields}
    for key in sorted(keys):
        assert key in connus or key in _HORS_SETTINGS, (
            f"{key} n'est lue par personne : ni champ de Settings, ni exception "
            f"documentée dans _HORS_SETTINGS. Réglage mort — c'est exactement le "
            f"défaut qu'a été LOG_LEVEL."
        )


def _minutes_du_jour(planning: str) -> int:
    """Convertit ``"15 5 * * *"`` en minutes depuis minuit.

    Refuse tout ce qui n'est pas un horaire quotidien fixe : la comparaison d'ordre
    ci-dessous n'aurait aucun sens sur un ``*/10`` ou un jour de semaine restreint.
    """
    minute, heure, jour_mois, mois, jour_semaine = planning.split()
    assert (jour_mois, mois, jour_semaine) == (
        "*",
        "*",
        "*",
    ), f"planning non quotidien : {planning!r}"
    return int(heure) * 60 + int(minute)


def _planning_helm(bloc: str) -> str:
    """Lit ``<bloc>.schedule`` dans values.yaml sans dépendre d'un parseur YAML."""
    texte = _VALUES_YAML.read_text(encoding="utf-8")
    m = re.search(
        rf"^{bloc}:\n(?:[ \t].*\n|\n)*?  schedule:\s*\"([^\"]+)\"",
        texte,
        re.MULTILINE,
    )
    assert m is not None, f"aucun `schedule` trouvé sous le bloc `{bloc}:` de values.yaml"
    return m.group(1)


def test_la_transformation_est_planifiee_entre_le_sondage_et_le_refresh() -> None:
    """L'ordre des trois CronJobs quotidiens est une exigence, pas une préférence.

    Le sondage écrit ``raw.*``, la transformation en dérive ``core.*``, le refresh
    reconstruit ``mart.*``. Inverser deux d'entre eux ne casse RIEN de visible :
    rafraîchir des vues au-dessus de tables ``core.*`` pas encore alimentées réussit --
    il recopie du vide -- et les tableaux de bord restent blancs une journée entière
    sans qu'aucun voyant ne s'allume. Un seul chiffre changé dans values.yaml suffit à
    provoquer cela ; d'où ce garde-fou.
    """
    sondage = _minutes_du_jour(_planning_helm("scheduler"))
    transformation = _minutes_du_jour(_planning_helm("transform"))
    refresh = _minutes_du_jour(_planning_helm("opsRefresh"))

    assert sondage < transformation < refresh, (
        f"ordre quotidien rompu : sondage {sondage}, transformation {transformation}, "
        f"refresh {refresh} (minutes depuis minuit). La transformation doit suivre le "
        f"sondage et précéder le rafraîchissement des vues."
    )


def test_la_taille_de_lot_du_chart_correspond_au_defaut_du_code() -> None:
    """``transform.batchLimit`` est passé en argument : il doit rester le défaut du code.

    Le CronJob passe explicitement ``--limit`` plutôt que de laisser le défaut
    s'appliquer, pour que la valeur soit lisible dans le manifest. Le prix est une
    duplication : si le défaut du code évolue sans que le chart suive, la production
    tourne sur l'ancienne valeur alors que tout le monde lit la nouvelle.
    """
    texte = _VALUES_YAML.read_text(encoding="utf-8")
    m = re.search(r"^transform:\n(?:[ \t].*\n|\n)*?  batchLimit:\s*(\d+)", texte, re.MULTILINE)
    assert m is not None, "aucun `batchLimit` sous le bloc `transform:` de values.yaml"
    assert int(m.group(1)) == DEFAULT_BATCH_LIMIT


def test_tous_les_cronjobs_declarent_le_meme_fuseau() -> None:
    """Sans ``timeZone``, kube-controller-manager applique UTC.

    Le test d'ordonnancement ci-dessus compare des minutes depuis minuit : cette
    comparaison n'a de sens que si les CronJobs sont lus dans le MEME fuseau. Un seul
    manifest sans ``timeZone`` suffirait a rendre l'ordre reel different de celui
    qu'annonce values.yaml -- et, en heure d'ete, a le decaler une seconde fois.
    """
    manifests = sorted(_TEMPLATES.glob("*-cronjob.yaml"))
    assert manifests, "aucun manifest de CronJob trouve : le glob a du deriver"

    sans_fuseau = [
        m.name
        for m in manifests
        if "timeZone: {{ .Values.ingest.config.timezone | quote }}"
        not in m.read_text(encoding="utf-8")
    ]
    assert not sans_fuseau, (
        f"CronJob(s) sans `timeZone` : {sans_fuseau}. Ils tourneraient en UTC alors que "
        f"les autres suivent `ingest.config.timezone`."
    )
