"""Adaptateur entre :mod:`garminator.garmin.client` et le ``WindowFetcher`` de
:mod:`garminator.scheduler.service` (lot L11).

``run_scheduled_poll`` n'importe ni ``GarminClient`` ni le catalogue -- il ne connaît
que le ``Protocol WindowFetcher``. C'est ce module qui referme la boucle : il consulte
:mod:`garminator.scheduler.catalog` pour savoir comment appeler un endpoint donné, invoque
:meth:`~garminator.garmin.client.GarminClient.fetch` (qui gère débit, backoff, et écrit
lui-même dans ``raw.api_response``), puis traduit le payload obtenu en
``Sequence[FetchedWindow]``.

Aucun ``try/except`` défensif ici, délibérément : ``run_scheduled_poll`` capture déjà
toute exception levée par le ``WindowFetcher`` injecté et marque la fenêtre ``failed`` --
en rajouter un ici ne ferait que masquer l'erreur réelle (réseau, authentification,
endpoint inconnu) derrière un état "réussi avec zéro payload" trompeur.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from garminator.garmin.raw_store import payload_sha256
from garminator.scheduler.catalog import CallShape, call_args, endpoint_spec
from garminator.scheduler.service import FetchedWindow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from garminator.garmin.client import GarminClient, IngestRunTracker


def _validate_activities_payload(payload: object, window_date: date) -> list[dict[str, object]]:
    """Valide la forme du payload de ``get_activities_by_date`` avant d'en extraire les
    ``activityId`` -- lève une erreur PARLANTE plutôt que de laisser échouer
    l'indexation, dont le message brut finirait tel quel dans
    ``raw.poll_window.last_error`` (illisible pour l'opérateur).

    Deux formes malformées réelles, observées en amont de ce lot :

    1. Garmin répond parfois par un dict d'erreur en HTTP 200 (ex.
       ``{"errorMessage": "..."}``) plutôt que la liste attendue : truthy, donc pas
       absorbé par le cas "payload vide" -- sans cette validation, itérer dessus
       donnerait des ``str`` (les clés du dict) et ``activity["activityId"]`` lèverait
       ``TypeError: string indices must be integers``, un message qui ne dit rien de la
       fenêtre en cause.
    2. Une activité de la liste sans clé ``activityId`` : sans validation,
       ``activity["activityId"]`` lève ``KeyError: 'activityId'`` -- une fois stringifié
       par ``run_scheduled_poll`` (``str(exc)``), ce message ne vaut littéralement que
       ``'activityId'``.

    Délibérément SANS ``try/except`` autour de l'appel réseau lui-même (voir le docstring
    du module) : cette fonction lève encore, elle ne fait que lever un message exploitable
    plutôt qu'une ``KeyError``/``TypeError`` opaque -- la fenêtre est toujours marquée
    ``failed`` par ``run_scheduled_poll``, et reste rejouable.
    """
    if not payload:
        return []
    if not isinstance(payload, list):
        keys_hint = f" (clés : {sorted(payload.keys())!r})" if isinstance(payload, dict) else ""
        raise ValueError(
            f"get_activities_by_date/{window_date.isoformat()} : payload inattendu -- "
            f"liste attendue, {type(payload).__name__} reçu{keys_hint}."
        )
    activities: list[dict[str, object]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(
                f"get_activities_by_date/{window_date.isoformat()} : élément {index} "
                f"inattendu -- dict attendu, {type(item).__name__} reçu."
            )
        if "activityId" not in item:
            raise ValueError(
                f"get_activities_by_date/{window_date.isoformat()} : élément {index} sans "
                f"clé 'activityId' -- clés présentes : {sorted(item.keys())!r}."
            )
        activities.append(item)
    return activities


@dataclass(slots=True)
class GarminWindowFetcher:
    """``WindowFetcher`` réel : sonde effectivement Garmin via un :class:`GarminClient`.

    ``tracker`` est optionnel pour rester utilisable hors d'un ``ingest_run(...)`` (ex.
    un script ponctuel d'opérateur) -- ``GarminClient.fetch`` accepte déjà ``tracker=None``.

    Décision de conception -- traçage brut des activités individuelles (voir le rapport
    de lot pour la discussion complète) : ``GarminClient.fetch`` n'est appelé **qu'une
    seule fois** par fenêtre, quel que soit l'endpoint, jamais une fois par activité.

    La VRAIE raison, seule et suffisante : ``get_activities_by_date`` renvoie déjà
    toutes les activités du jour en un seul appel. Rappeler ``client.fetch`` une fois par
    activité (pour obtenir une ligne ``raw.api_response`` par activité, avec
    ``scope_key = activityId``) ferait exploser le volume de requêtes contre une API
    Garmin non publique -- particulièrement sur un backfill courant depuis 2025-04-03,
    où chaque jour peut porter plusieurs activités (multisport, brick). Ce n'est PAS une
    question de frontière de responsabilité entre modules : le point d'entrée de ce lot
    importe déjà ``ingest_run`` de ``client.py``, et ``activity_files.py`` (lot L12) écrit
    déjà, lui, directement dans une autre table brute (``raw.fit_file``) -- "un seul
    module écrit du brut" n'a jamais été une règle du dépôt.

    La ligne ``raw.api_response`` unique porte donc ``scope_key=None`` et contient
    l'intégralité de la liste telle que Garmin l'a renvoyée -- rien n'est perdu du point
    de vue du stockage brut. La discrimination par activité (``scope_key`` =
    ``activityId``) n'existe qu'au niveau de ``raw.payload_seen``, via les
    ``FetchedWindow`` retournés ici : c'est le niveau exact dont a besoin
    ``run_scheduled_poll`` pour détecter qu'une activité précise a changé de contenu
    d'un run à l'autre, sans dupliquer le stockage brut ni multiplier les requêtes.

    Limite réelle assumée, à la charge d'un futur lot de transformation : ``payload_seen``
    sait dire QU'une activité a changé (son ``payload_sha256`` a changé), mais retrouver
    la ligne ``raw.api_response`` brute correspondante impose alors un filtrage ``jsonb``
    sur ``(endpoint, scope_date)`` puis une recherche de l'``activityId`` dans le tableau
    JSON -- l'index partiel ``api_response_scope_key_idx ... WHERE scope_key IS NOT NULL``
    (``migrations/002_raw.sql``) reste inutilisé pour cet endpoint, puisque la ligne
    unique porte ``scope_key=None``. Un futur lot qui a besoin de cet index devra soit
    l'accepter (le volume par jour reste faible), soit revisiter cette décision.
    """

    client: GarminClient
    tracker: IngestRunTracker | None = None

    def __call__(self, endpoint: str, window_date: date) -> Sequence[FetchedWindow]:
        spec = endpoint_spec(endpoint)
        args = call_args(spec, window_date)
        payload = self.client.fetch(
            endpoint, *args, scope_date=window_date, scope_key=None, tracker=self.tracker
        )

        if spec.call_shape is CallShape.ACTIVITIES_BY_DATE:
            activities = _validate_activities_payload(payload, window_date)
            return [
                FetchedWindow(
                    scope_key=str(activity["activityId"]),
                    payload_sha256=payload_sha256(activity),
                )
                for activity in activities
            ]

        # Jour sans donnée (repos sans capteur porté, endpoint non pertinent ce jour-là,
        # ...) : Garmin répond par ``None``, ``{}`` ou ``[]`` selon l'endpoint. Ce n'est
        # pas une erreur -- une séquence vide permet à ``run_scheduled_poll`` de marquer
        # la fenêtre ``done`` sans rien enregistrer dans ``raw.payload_seen``.
        if not payload:
            return []
        return [FetchedWindow(scope_key=None, payload_sha256=payload_sha256(payload))]
