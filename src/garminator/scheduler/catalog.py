"""Catalogue déclaratif des endpoints ``garminconnect`` sondables par jour (lot L11).

``garminconnect.Garmin`` expose environ 130 méthodes, sans convention de signature
commune -- certaines prennent un jour unique, d'autres une plage ``[start, end]``,
d'autres encore renvoient une liste plutôt qu'un objet agrégé au jour. Plutôt qu'une
cascade de ``if`` par nom d'endpoint (ce que ce module refuse délibérément), le
catalogue ci-dessous suit le même principe que ``core.metric_method_priority``
(:mod:`garminator.load.method_priority`) : une table de données, une petite poignée de
formes d'appel déclarées, et une fonction pure qui les traduit en arguments positionnels.

Les trois formes retenues ont été vérifiées par introspection de la bibliothèque
installée (``inspect.signature(garminconnect.Garmin.<méthode>)``), jamais supposées à la
lecture de la documentation -- c'est exactement ce que revérifie
``tests/scheduler/test_catalog.py`` à chaque exécution des tests, pour détecter toute
dérive d'une mise à jour de ``garminconnect`` sans jamais appeler Garmin.

Ce module n'importe pas ``garminconnect`` : il décrit un contrat, il ne le consomme pas.
Seul le test d'introspection l'importe (avec ``skipif`` si absent).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # ``date`` n'apparaît que dans l'annotation de paramètre de `call_args` ci-dessous --
    # jamais évaluée à l'exécution grâce à `from __future__ import annotations`. Import
    # sous `TYPE_CHECKING`, sans `noqa` : contrairement à ce qu'affirmait l'ancien
    # commentaire, aucune signature de ce module n'exécute réellement ce type.
    from datetime import date


class CallShape(Enum):
    """Formes d'appel distinctes observées parmi les méthodes de ``garminconnect.Garmin``
    pertinentes pour un sondage journalier. Voir :func:`call_args` pour leur traduction
    en arguments positionnels, et ``tests/scheduler/test_catalog.py`` pour la preuve que
    chaque forme correspond bien à la signature réelle."""

    #: ``f(cdate: str) -> dict | list | None`` -- un seul jour, agrégé.
    DAY = "day"
    #: ``f(startdate: str, enddate: str | None = None) -> dict | list`` -- une plage ;
    #: appelée ici avec ``startdate == enddate == window_date`` pour n'interroger qu'un
    #: seul jour, cette API n'offrant pas de forme "un seul jour" dédiée.
    RANGE = "range"
    #: Même forme d'appel que ``RANGE``, mais le résultat est une liste d'objets
    #: indépendants (une activité chacun) plutôt qu'un objet agrégé au jour -- traité à
    #: part parce que c'est la seule forme dont le "scope_key" se dérive du payload lui
    #: même plutôt que d'être toujours ``None``. Voir
    #: :class:`garminator.scheduler.fetcher.GarminWindowFetcher`.
    ACTIVITIES_BY_DATE = "activities_by_date"


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    """Une entrée du catalogue : le nom exact de la méthode ``garminconnect.Garmin``
    (résolue dynamiquement par :meth:`garminator.garmin.client.GarminClient.fetch` via
    ``getattr``) et sa forme d'appel déclarée."""

    name: str
    call_shape: CallShape


#: Endpoints agrégés au jour, forme ``f(cdate: str)`` -- vérifiés par introspection le
#: 2026-08-23 contre la version de ``garminconnect`` installée dans ``.venv``.
_DAY_ENDPOINTS: tuple[str, ...] = (
    "get_sleep_data",
    "get_stats",
    "get_user_summary",
    "get_hrv_data",
    "get_rhr_day",
    "get_stress_data",
    "get_all_day_stress",
    "get_respiration_data",
    "get_spo2_data",
    "get_max_metrics",
    "get_training_readiness",
    "get_training_status",
    "get_floors",
    "get_steps_data",
    "get_intensity_minutes_data",
    "get_body_battery_events",
    "get_daily_weigh_ins",
)

#: Endpoints en plage, forme ``f(startdate: str, enddate: str | None = None)``.
_RANGE_ENDPOINTS: tuple[str, ...] = (
    "get_body_battery",
    "get_body_composition",
    "get_endurance_score",
    "get_hill_score",
)

#: Catalogue complet -- une entrée par endpoint connu de ce lot.
CATALOG: tuple[EndpointSpec, ...] = (
    *(EndpointSpec(name, CallShape.DAY) for name in _DAY_ENDPOINTS),
    *(EndpointSpec(name, CallShape.RANGE) for name in _RANGE_ENDPOINTS),
    EndpointSpec("get_activities_by_date", CallShape.ACTIVITIES_BY_DATE),
)

#: Index par nom, construit une seule fois -- ``CATALOG`` reste la source de vérité
#: ordonnée (utile pour un affichage stable), cet index sert aux lectures ponctuelles.
_CATALOG_BY_NAME: dict[str, EndpointSpec] = {spec.name: spec for spec in CATALOG}

#: Sous-ensemble par défaut pertinent pour un triathlète (cf. profil-athlete dans la
#: mémoire du projet) : sommeil, HRV, FC repos, stress, Body Battery, readiness,
#: training status, composition corporelle, activités. Volontairement plus restreint
#: que ``CATALOG`` entier -- chaque endpoint ajouté ici multiplie le nombre d'appels
#: réseau par run (cf. ``DEFAULT_MAX_WINDOWS_PER_ENDPOINT`` dans ``service.py``), un
#: opérateur qui veut le catalogue complet le passe explicitement via ``--endpoint``.
#:
#: Poids/composition corporelle -- arbitrage TRANCHÉ, ne pas rouvrir : ``get_body_composition``
#: est retenu, ``get_daily_weigh_ins`` non. Lequel des deux porte réellement de la donnée
#: dépend de la façon dont le poids entre dans Garmin (balance Index contre saisie manuelle) ;
#: l'athlète a explicitement indiqué que la question était sans enjeu pour lui. On garde donc
#: le choix conservateur : un appel quotidien éventuellement vide coûte moins qu'un trou dans
#: l'historique, et le second endpoint reste accessible via ``--endpoint`` sans modifier ce code.
DEFAULT_ENDPOINTS: tuple[str, ...] = (
    "get_sleep_data",
    "get_hrv_data",
    "get_rhr_day",
    "get_stress_data",
    "get_body_battery",
    "get_training_readiness",
    "get_training_status",
    "get_body_composition",
    "get_max_metrics",
    "get_activities_by_date",
)


def is_known_endpoint(name: str) -> bool:
    """``True`` si ``name`` est déclaré dans :data:`CATALOG`."""
    return name in _CATALOG_BY_NAME


def endpoint_spec(name: str) -> EndpointSpec:
    """L'entrée du catalogue pour ``name``.

    Raises:
        ValueError: si ``name`` n'est pas un endpoint connu -- erreur de configuration
            (faute de frappe côté opérateur, ou endpoint jamais ajouté au catalogue),
            pas un cas silencieux à absorber.
    """
    try:
        return _CATALOG_BY_NAME[name]
    except KeyError:
        raise ValueError(
            f"endpoint inconnu du catalogue : {name!r} -- endpoints déclarés : "
            f"{sorted(_CATALOG_BY_NAME)}"
        ) from None


def call_args(spec: EndpointSpec, window_date: date) -> tuple[str, ...]:
    """Traduit ``window_date`` en arguments positionnels pour l'endpoint de ``spec``.

    - :attr:`CallShape.DAY` : un seul argument ``cdate``.
    - :attr:`CallShape.RANGE` et :attr:`CallShape.ACTIVITIES_BY_DATE` : ``startdate`` et
      ``enddate`` identiques -- ces deux formes n'offrent aucune variante "un seul jour"
      dédiée, la plage réduite à une seule date obtient le même effet.
    """
    iso = window_date.isoformat()
    if spec.call_shape is CallShape.DAY:
        return (iso,)
    return (iso, iso)
