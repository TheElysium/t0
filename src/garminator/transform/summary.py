"""Traduit le résumé JSON d'une activité Garmin Connect (``get_activities_by_date``,
stocké tel quel dans ``raw.api_response``) en :class:`~garminator.transform.activity.
ActivityContext`, et charge les seuils (``core.threshold``) dont ce contexte a besoin.

CHAÎNON MANQUANT identifié par grep (aucune occurrence de ``typeKey`` dans ``src/`` avant
ce lot) : ``build_core_rows`` (``garminator.transform.activity``) sait tout faire à
partir d'un ``ActivityContext``, mais rien dans le dépôt ne savait encore le construire
depuis ce que Garmin Connect renvoie réellement. Ce module ferme cet écart, et lui seul :
aucun calcul de charge, aucune écriture ``core.*`` n'a lieu ici.

RÈGLE NON NÉGOCIABLE DE CE MODULE : un ``typeKey`` Garmin inconnu de la table de
correspondance n'est JAMAIS deviné -- ni replié sur ``running`` (le sport le plus
fréquent, ce serait le pire silencieux), ni sur ``other`` (qui ferait disparaître
l'activité de tout calcul de charge sans que personne ne s'en aperçoive). Garmin ajoute
des ``typeKey`` sans préavis (nouveaux sports de montre, catégories réorganisées) ; le
seul traitement sûr d'un cas non couvert est de s'arrêter et de nommer précisément ce qui
manque, pour qu'un humain ajoute la ligne à :data:`SPORT_CODE_BY_TYPE_KEY` en connaissance
de cause -- classer par erreur une sortie vélo en course fausserait silencieusement le
TSS de la séance, donc tout le PMC qui en dépend en aval.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from garminator.garmin.raw_store import payload_sha256
from garminator.load.thresholds import Threshold, threshold_for_date
from garminator.transform.activity import ActivityContext

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

#: Format des deux horodatages ``startTimeLocal``/``startTimeGMT`` renvoyés par
#: ``get_activities_by_date`` -- VÉRIFIÉ dans la consigne du lot, pas déduit.
_GARMIN_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class SportInconnuError(ValueError):
    """``activityType.typeKey`` absent de :data:`SPORT_CODE_BY_TYPE_KEY`.

    Nomme le ``typeKey`` reçu et l'``activityId`` d'origine pour que l'opérateur puisse
    ajouter la ligne manquante à la table de correspondance -- voir le docstring du
    module pour la raison pour laquelle ce cas ne se devine jamais."""

    def __init__(self, type_key: str, activity_id: int) -> None:
        self.type_key = type_key
        self.activity_id = activity_id
        super().__init__(
            f"typeKey Garmin inconnu {type_key!r} pour l'activité {activity_id} : "
            "aucun sport_code correspondant dans SPORT_CODE_BY_TYPE_KEY "
            "(garminator.transform.summary). Ajouter la ligne manquante plutôt que de "
            "deviner -- un mauvais classement fausserait silencieusement le TSS."
        )


#: Correspondance ``activityType.typeKey`` (Garmin Connect) -> ``core.sport.sport_code``
#: (seed de migrations/003_core_ref.sql, ligne 38 -- les 12 seuls sport_code existants).
#: Liste non exhaustive côté Garmin par construction (cf. docstring du module) : chaque
#: entrée absente doit lever :class:`SportInconnuError`, jamais retomber sur un défaut.
#:
#: Décisions non évidentes, documentées ici plutôt que dans le code (rien à vérifier au
#: runtime, seulement à relire) :
#: - ``indoor_running`` -> ``treadmill`` : Garmin distingue "Indoor Running" (générique,
#:   sans tapis identifié) de "Treadmill Running", mais ``core.sport`` n'a qu'un seul code
#:   indoor pour la course (``treadmill``, ``is_indoor=true``) -- le plus proche sans en
#:   inventer un second.
#: - ``track_running`` -> ``running`` : piste en extérieur, ni indoor ni trail ; la seule
#:   discipline "run" extérieure standard disponible est ``running``.
#: - ``indoor_cycling`` -> ``virtual_cycling`` : home-trainer sans simulation (Garmin les
#:   distingue de ``virtual_ride``/Zwift), mais ``core.sport`` n'a qu'un seul code vélo
#:   indoor (``virtual_cycling``, ``is_indoor=true``) -- cohérent avec le profil de cet
#:   athlète (puissance vélo confirmée UNIQUEMENT en home-trainer).
#: - ``gravel_cycling`` / ``mountain_biking`` -> ``cycling`` : aucun code dédié gravel/VTT
#:   dans le seed ; ``cycling`` (extérieur, non indoor) est la seule discipline "bike"
#:   extérieure compatible, sans deviner un code qui n'existe pas en base.
SPORT_CODE_BY_TYPE_KEY: dict[str, str] = {
    "running": "running",
    "trail_running": "trail_running",
    "treadmill_running": "treadmill",
    "indoor_running": "treadmill",
    "track_running": "running",
    "cycling": "cycling",
    "road_biking": "cycling",
    "indoor_cycling": "virtual_cycling",
    "virtual_ride": "virtual_cycling",
    "gravel_cycling": "cycling",
    "mountain_biking": "cycling",
    "lap_swimming": "lap_swimming",
    "open_water_swimming": "open_water",
    "hiking": "hiking",
    "resort_skiing_snowboarding_ws": "resort_skiing",
    "multi_sport": "multisport",
    "transition": "transition",
    "strength_training": "strength",
}

#: Discipline par sport_code, recopiée EXACTEMENT du seed de migrations/003_core_ref.sql
#: (ligne 38) -- jamais redéduite ici (« la colonne discipline est structurante, pas
#: décorative », cf. COMMENT ON TABLE core.sport dans la migration).
DISCIPLINE_BY_SPORT_CODE: dict[str, str] = {
    "running": "run",
    "trail_running": "run",
    "treadmill": "run",
    "cycling": "bike",
    "virtual_cycling": "bike",
    "lap_swimming": "swim",
    "open_water": "swim",
    "hiking": "other",
    "resort_skiing": "other",
    "multisport": "other",
    "transition": "other",
    "strength": "other",
}


def _sport_code_for_type_key(type_key: str, activity_id: int) -> str:
    sport_code = SPORT_CODE_BY_TYPE_KEY.get(type_key)
    if sport_code is None:
        raise SportInconnuError(type_key, activity_id)
    return sport_code


def _optional_int(value: Any) -> int | None:
    """``None`` reste ``None`` -- jamais remplacé par 0 (règle centrale du schéma,
    cf. docstring de ``garminator.transform.rows``).

    ARRONDI et non troncature, et ce n'est pas un détail de style : Garmin renvoie
    ``duration``/``elapsedDuration``/``movingDuration`` en secondes FLOTTANTES. Or
    ``duration_s`` est arrondi plus bas (``int(round(...))``). Tronquer ici produirait,
    sur une séance de 3599,6 s, un ``elapsed_s`` de 3599 face à un ``duration_s`` de
    3600 -- soit un temps écoulé INFÉRIEUR au temps de séance, ce qui est physiquement
    impossible et n'a d'autre cause que deux conversions incohérentes. Une seule
    convention pour tous les champs de durée.
    """
    return None if value is None else int(round(float(value)))


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _parse_garmin_time(raw: Any, *, field_name: str, activity_id: int) -> datetime:
    if raw is None:
        msg = (
            f"activité {activity_id} : champ '{field_name}' absent du résumé Garmin -- "
            "impossible de calculer tz_offset_s sans deviner un fuseau (cf. docstring "
            "du module)."
        )
        raise ValueError(msg)
    try:
        return datetime.strptime(str(raw), _GARMIN_TIME_FORMAT)
    except ValueError as exc:
        msg = (
            f"activité {activity_id} : champ '{field_name}'={raw!r} ne respecte pas le "
            f"format Garmin attendu {_GARMIN_TIME_FORMAT!r}."
        )
        raise ValueError(msg) from exc


def build_activity_context(summary: Mapping[str, Any]) -> ActivityContext:
    """Construit un ``ActivityContext`` à partir d'un résumé JSON tel que renvoyé par
    ``get_activities_by_date`` (et stocké tel quel dans ``raw.api_response.payload``).

    N'accède à AUCUN échantillon : tout ce que ce résumé ne porte pas reste ``None``,
    c'est ``build_core_rows`` (``garminator.transform.activity``) qui décide ensuite s'il
    existe un recours calculé depuis les échantillons (cf. docstring d'``ActivityContext``
    sur ``distance_m``/``ascent_m``/``descent_m``).
    """
    activity_id_raw = summary.get("activityId")
    if activity_id_raw is None:
        msg = "résumé Garmin sans 'activityId' : impossible de construire un ActivityContext."
        raise ValueError(msg)
    activity_id = int(activity_id_raw)

    activity_type = summary.get("activityType") or {}
    type_key = activity_type.get("typeKey")
    if type_key is None:
        msg = f"activité {activity_id} : 'activityType.typeKey' absent du résumé Garmin."
        raise ValueError(msg)
    sport_code = _sport_code_for_type_key(str(type_key), activity_id)
    discipline = DISCIPLINE_BY_SPORT_CODE[sport_code]

    start_time_local = _parse_garmin_time(
        summary.get("startTimeLocal"), field_name="startTimeLocal", activity_id=activity_id
    )
    start_time_gmt = _parse_garmin_time(
        summary.get("startTimeGMT"), field_name="startTimeGMT", activity_id=activity_id
    )
    # Arrondi à la seconde : les deux horodatages Garmin n'ont pas de sous-seconde, un
    # écart flottant ne serait qu'un artefact d'arithmétique datetime, pas un vrai
    # décalage de fuseau.
    tz_offset_s = round((start_time_local - start_time_gmt).total_seconds())

    duration_raw = summary.get("duration")
    if duration_raw is None:
        msg = f"activité {activity_id} : 'duration' absent du résumé Garmin."
        raise ValueError(msg)
    duration_s = int(round(float(duration_raw)))
    if duration_s <= 0:
        msg = (
            f"activité {activity_id} : duration={duration_raw!r} donne duration_s="
            f"{duration_s} <= 0 (champ 'duration' du résumé Garmin, converti en secondes)."
        )
        raise ValueError(msg)

    parent_raw = summary.get("parentId")
    if parent_raw is None:
        parent_raw = summary.get("parentActivityId")
    parent_activity_id = _optional_int(parent_raw)

    return ActivityContext(
        activity_id=activity_id,
        sport_code=sport_code,
        discipline=discipline,
        duration_s=duration_s,
        start_time_local=start_time_local,
        tz_offset_s=tz_offset_s,
        garmin_type_key=str(type_key),
        elapsed_s=_optional_int(summary.get("elapsedDuration")),
        moving_s=_optional_int(summary.get("movingDuration")),
        distance_m=_optional_float(summary.get("distance")),
        ascent_m=_optional_float(summary.get("elevationGain")),
        descent_m=_optional_float(summary.get("elevationLoss")),
        calories=_optional_int(summary.get("calories")),
        name=summary.get("activityName"),
        parent_activity_id=parent_activity_id,
        # multisport est le seul sport_code produces_records=false ET regroupant
        # explicitement des enfants dans le seed (COMMENT ON COLUMN
        # core.sport.produces_records, migrations/003) -- c'est le signal fiable
        # d'une activité parente, pas une supposition sur la forme du JSON.
        is_parent=sport_code == "multisport",
        # dict(summary) : json.dumps ne sait sérialiser nativement qu'un dict, pas un
        # Mapping quelconque -- payload_sha256 utilise `default=str` qui, sur un objet
        # non reconnu, le remplacerait tout entier par sa repr() au lieu d'en hacher le
        # contenu clé par clé. La conversion explicite évite ce piège silencieux.
        summary_sha256=payload_sha256(dict(summary)),
    )


class _Cursor(Protocol):
    def execute(self, query: str, params: Any = ...) -> object: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Vue structurelle minimale sur une connexion psycopg 3, pour l'injection en test --
    même motif que :class:`garminator.garmin.raw_store.DBConnection` (cf. son docstring
    pour la justification complète : aucun utilitaire de requête générique en L1, et une
    connexion réelle psycopg 3 satisfait ce protocole sans conversion)."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...


def load_thresholds(conn: DBConnection, *, athlete_id: int) -> tuple[Threshold, ...]:
    """Lit tous les seuils de ``athlete_id`` dans ``core.threshold`` et les renvoie comme
    autant de :class:`~garminator.load.thresholds.Threshold`.

    ``lower(validity)``/``upper(validity)`` extraient les bornes du ``daterange`` demi-
    ouvert EN SQL plutôt que de parser sa représentation texte côté Python (ex.
    ``'[2026-01-01,)'``) : PostgreSQL connaît déjà ce type nativement, le reparser à la
    main serait une réimplémentation fragile d'un problème déjà résolu. Une borne haute
    NULL (celle que ``upper()`` renvoie pour un intervalle non borné) désigne le seuil
    courant, cohérent avec ``Threshold.valid_to: date | None``.

    Lecture seule : ne fait jamais de résolution de date ici -- c'est
    :func:`select_thresholds` (ou directement :func:`garminator.load.thresholds.
    threshold_for_date`) qui applique la règle « une date non couverte renvoie None,
    jamais un seuil deviné »."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT threshold_id, discipline, lower(validity), upper(validity),
                   ftp_watts, rftp_watts, ftp_pace_s_per_km, css_m_s,
                   lthr_bpm, hr_max_bpm, hr_rest_bpm
              FROM core.threshold
             WHERE athlete_id = %s
             ORDER BY discipline, lower(validity);
            """,
            (athlete_id,),
        )
        rows = cur.fetchall()
    return tuple(
        Threshold(
            threshold_id=int(row[0]),
            discipline=row[1],
            valid_from=row[2],
            valid_to=row[3],
            ftp_watts=row[4],
            rftp_watts=row[5],
            ftp_pace_s_per_km=row[6],
            css_m_s=row[7],
            lthr_bpm=row[8],
            hr_max_bpm=row[9],
            hr_rest_bpm=row[10],
        )
        for row in rows
    )


def select_thresholds(
    thresholds: Sequence[Threshold], discipline: str, on_date: date
) -> tuple[Threshold | None, Threshold | None]:
    """Renvoie ``(threshold, all_threshold)`` pour ``discipline`` à ``on_date`` -- les
    deux arguments exacts attendus séparément par ``build_core_rows`` (``threshold`` porte
    FTP/CSS/allure seuil selon la discipline, ``all_threshold`` porte LTHR et FC
    max/repos, communs a toutes les disciplines).

    ``core.threshold.weight_kg`` existe en base (migrations/003) mais n'est PAS
    charge ici : aucun calcul de charge ne le lit aujourd'hui. Le porter dans
    ``Threshold`` sans lecteur en ferait un champ mort qu'on croirait a jour.

    Simple composition de :func:`garminator.load.thresholds.threshold_for_date`, appelée
    deux fois : aucune logique de résolution n'est dupliquée ici, seulement assemblée
    pour éviter à l'appelant du runner de le faire lui-même à chaque activité."""
    return (
        threshold_for_date(thresholds, discipline, on_date),
        threshold_for_date(thresholds, "all", on_date),
    )
