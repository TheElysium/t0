"""``build_core_rows`` : le chaînage pur décodé + seuils -> ``CoreActivityBundle``.

Fonction unique d'orchestration de ce lot. Elle ne fait aucun calcul « neuf » : elle
appelle, dans le bon ordre et avec les bonnes séries, les fonctions déjà pures et
testées de ``garminator.compute`` (L4) et ``garminator.load`` (L5). Rien ici n'accède
au disque, au réseau ni à PostgreSQL.

ÉCART ASSUMÉ PAR RAPPORT À LA SIGNATURE PROPOSÉE DANS LA CONSIGNE
-------------------------------------------------------------------
La consigne envisageait ``build_core_rows(decoded, *, athlete_id, fit_file_id,
threshold, all_threshold, sex)``. Ce n'est pas suffisant : ``ParsedActivitySource``
(``garminator.fit.model``) ne porte NI le sport, NI la durée chrono, NI l'heure locale
d'une activité. Pour un FIT, ces informations existent bien quelque part dans le
fichier (message ``session``), mais ``decode_fit`` les laisse volontairement dans
``SessionSummary.raw`` -- un dict non typé -- car son propre docstring l'explique :
seuls 6 champs de comparaison sont typés, le reste est « hors périmètre de L3 ».  Pour
un TCX, il n'y a même pas de message ``session`` : ``ParsedActivitySource.session`` est
``None`` (VÉRIFIÉ, cf. ``tcx/reader.py`` -- addendum §2.2), et c'est justement le
format de TOUTES les séances de home-trainer de cet athlète.

Plutôt que de deviner ces informations depuis les échantillons (deviner un sport ou
une durée chrono depuis une série de points est exactement le genre d'invention que ce
projet interdit), ce lot ajoute un paramètre explicite ``context: ActivityContext``
portant ce que seul l'appelant (le résumé JSON ``get_activity``, L2/L6, hors périmètre
de ce lot) peut connaître de façon fiable. Tout ce qui EST calculable depuis les
échantillons (moyennes, NP, GAP, distance totale...) l'est ici et n'est PAS demandé à
``ActivityContext`` -- l'écart est aussi réduit que possible.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta, timezone
from typing import TYPE_CHECKING

from garminator.compute import (
    average,
    average_nonzero,
    decoupling_pct,
    efficiency_factor,
    normalized_graded_pace_speed_mps,
    normalized_power,
    power_duration_curve,
    regularize_series,
    smooth_grade_series,
)
from garminator.compute import (
    hr_histogram as compute_hr_histogram,
)
from garminator.compute import (
    np_reliable as compute_np_reliable,
)
from garminator.load.activity_load import (
    ActivityLoadInput,
    ActivityMetricRow,
    build_activity_metric_rows,
    needs_manual_rpe,
)
from garminator.transform.rows import (
    ActivityHrHistogramRow,
    ActivityPowerCurveRow,
    ActivityRow,
    CoreActivityBundle,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from garminator.compute import RegularizedSeries
    from garminator.fit.model import ParsedActivitySource, SampleRow, SwimLengthRow
    from garminator.load.thresholds import Threshold
    from garminator.load.trimp import Sex

#: kind de core.activity_power_curve porté par la puissance, selon la discipline.
_POWER_CURVE_KIND = {"bike": "bike_power", "run": "run_power"}

#: nombre minimal d'échantillons vitesse+distance pour tenter un calcul de pente
#: lissée (``smooth_grade_series`` a besoin d'au moins deux points pour une fenêtre).
_MIN_SAMPLES_FOR_GRADE = 2

#: method_code réutilisé pour efficiency_factor / decoupling_pct, selon la discipline
#: (cf. docstring de ``_load_extra_metric_rows`` pour la justification).
_DECOUPLING_METHOD_CODE = {"bike": "bike_power", "run": "run_gap"}


@dataclass(frozen=True, slots=True)
class ActivityContext:
    """Ce que ``ParsedActivitySource`` ne sait pas dire sur une activité (voir
    docstring du module). Fourni par l'appelant, jamais deviné.

    ``distance_m`` / ``ascent_m`` / ``descent_m`` sont des recours utilisés
    UNIQUEMENT quand aucun échantillon ne porte l'information correspondante (le cas
    de la natation en bassin, où ``core.sample`` est vide par construction -- voir
    ``migrations/005_core_series.sql``, commentaire de ``core.swim_length``) : quand
    des échantillons exploitables existent, ``build_core_rows`` leur fait toujours
    confiance en priorité.
    """

    activity_id: int
    sport_code: str
    discipline: str  # 'bike' | 'run' | 'swim' | 'other'
    duration_s: int
    start_time_local: datetime
    tz_offset_s: int
    garmin_type_key: str | None = None
    elapsed_s: int | None = None
    moving_s: int | None = None
    distance_m: float | None = None
    ascent_m: float | None = None
    descent_m: float | None = None
    calories: int | None = None
    name: str | None = None
    parent_activity_id: int | None = None
    is_parent: bool = False
    leg_order: int | None = None
    primary_device_id: int | None = None
    summary_sha256: bytes | None = None


def _channel(
    samples: list[SampleRow], getter: Callable[[SampleRow], float | int | None]
) -> tuple[list[int], list[float]]:
    """Sous-série ``(elapsed_s, valeurs)`` restreinte aux échantillons où ``getter``
    renvoie une valeur exploitable -- une absence ponctuelle (capteur décroché) n'est
    JAMAIS confondue avec une valeur réelle de 0."""
    elapsed: list[int] = []
    values: list[float] = []
    for sample in samples:
        value = getter(sample)
        if value is not None:
            elapsed.append(sample.elapsed_s)
            values.append(float(value))
    return elapsed, values


def _build_power_curve_rows(
    activity_id: int, kind: str, values: tuple[float, ...]
) -> tuple[ActivityPowerCurveRow, ...]:
    if not values:
        return ()
    curve = power_duration_curve(values)
    return tuple(
        ActivityPowerCurveRow(activity_id=activity_id, kind=kind, duration_s=duration, value=value)
        for duration, value in sorted(curve.items())
        if value is not None
    )


def _run_gap_series(
    samples: list[SampleRow],
) -> tuple[list[float], list[float | None]]:
    """Vitesse (m/s) et pente lissée alignées, restreintes aux échantillons portant à
    la fois une vitesse ET une distance cumulée (``smooth_grade_series`` a besoin
    d'une série de distance sans trou pour fenêtrer -- §3.3.2 de la spec citée dans
    ``minetti.py``). L'altitude, elle, tolère des ``None`` isolés."""
    usable = [s for s in samples if s.speed_mps is not None and s.distance_m is not None]
    if len(usable) < _MIN_SAMPLES_FOR_GRADE:
        return [], []
    speed = [float(s.speed_mps) for s in usable]  # type: ignore[arg-type]  # filtré ci-dessus
    distance = [float(s.distance_m) for s in usable]  # type: ignore[arg-type]
    altitude = [s.altitude_m for s in usable]
    grade = smooth_grade_series(distance, altitude)
    return speed, grade


def _load_extra_metric_rows(
    *,
    activity_id: int,
    discipline: str,
    output_full: float | None,
    output_half1: float | None,
    output_half2: float | None,
    avg_hr_full: float | None,
    avg_hr_half1: float | None,
    avg_hr_half2: float | None,
) -> tuple[ActivityMetricRow, ...]:
    """Lignes ``efficiency_factor`` (activité entière) et ``decoupling_pct`` (deux
    moitiés de durée égale), émises directement ici plutôt que par
    :func:`garminator.load.activity_load.build_activity_metric_rows`.

    CHOIX JUSTIFIÉ : ``ActivityLoadInput`` (contrat de L5) ne reçoit que des scalaires
    déjà agrégés sur l'activité entière -- c'est délibéré, cf. son docstring. Le
    découplage a BESOIN des deux moitiés de la série, une donnée que L5 ne connaît
    structurellement pas et ne devrait pas connaître (il resterait pur si on le lui
    ajoutait, mais son contrat mélangerait alors deux niveaux de granularité).
    Calculer les deux moitiés est le travail naturel de CE module, qui a déjà les
    échantillons en main pour NP/GAP -- ajouter ``efficiency_factor``/
    ``decoupling_pct`` ici évite un aller-retour de données déjà calculées.

    ``method_code`` : aucun code dédié n'existe dans ``core.load_method`` (seuls 7
    codes sont semés par migrations/003, aucune migration n'est permise dans ce lot).
    Choix conservateur et documenté : réutiliser le method_code de la grandeur qui SERT
    de "output" au calcul (``bike_power`` pour la puissance vélo, ``run_gap`` pour la
    vitesse ajustée à la pente course -- jamais ``run_power``, car le rFTP est NULL par
    décision explicite de l'athlète et le GAP est la seule méthode réellement
    disponible pour la course, cf. vigilance points du lot). Seuls vélo et course sont
    couverts : aucune formule de découplage n'est spécifiée pour la natation dans ce
    lot, et en inventer une measure de sortie ne serait pas justifié -- descope
    explicite, à signaler.
    """
    if discipline not in _DECOUPLING_METHOD_CODE:
        return ()
    method_code = _DECOUPLING_METHOD_CODE[discipline]

    ef_full = efficiency_factor(output_full, avg_hr_full)
    ef1 = efficiency_factor(output_half1, avg_hr_half1)
    ef2 = efficiency_factor(output_half2, avg_hr_half2)
    decoupling = decoupling_pct(ef1, ef2)

    return (
        ActivityMetricRow(
            activity_id=activity_id,
            metric="efficiency_factor",
            method_code=method_code,
            value=ef_full,
            is_canonical=ef_full is not None,
            threshold_id=None,  # aucun seuil n'entre dans ce calcul (sortie brute / FC).
        ),
        ActivityMetricRow(
            activity_id=activity_id,
            metric="decoupling_pct",
            method_code=method_code,
            value=decoupling,
            is_canonical=decoupling is not None,
            threshold_id=None,
        ),
    )


@dataclass(frozen=True, slots=True)
class _PowerChannel:
    """Résultat intermédiaire du canal puissance -- factorisé hors de
    :func:`build_core_rows` pour rester sous le seuil de complexité de ce module
    (PLR0915), sans rien changer au calcul lui-même."""

    elapsed: list[int]
    values: list[float]
    regularized: RegularizedSeries
    np_watts: float | None
    np_reliable: bool | None
    avg_power: float | None
    avg_power_nonzero: float | None
    max_power: float | None


def _power_channel(samples: list[SampleRow]) -> _PowerChannel:
    # -- Canal puissance : seul canal régularisé de ce lot (cf. commentaire de
    # core.activity.imputed_seconds, explicitement à propos de la puissance).
    elapsed, values = _channel(samples, lambda s: s.power)
    reg = regularize_series(elapsed, values, fill_value=0.0)
    np_watts = normalized_power(reg.values)
    return _PowerChannel(
        elapsed=elapsed,
        values=values,
        regularized=reg,
        np_watts=np_watts,
        np_reliable=compute_np_reliable(reg.total_seconds) if np_watts is not None else None,
        avg_power=average(values) if values else None,
        avg_power_nonzero=average_nonzero(values) if values else None,
        max_power=max(values) if values else None,
    )


@dataclass(frozen=True, slots=True)
class _HrChannel:
    """Résultat intermédiaire du canal fréquence cardiaque (même raison que
    :class:`_PowerChannel`)."""

    elapsed: list[int]
    values: list[float]
    avg_hr_raw: float | None  # non arrondi, réutilisé tel quel par l'EF/découplage.
    avg_hr: int | None
    max_hr: int | None


def _hr_channel(samples: list[SampleRow]) -> _HrChannel:
    elapsed, values = _channel(samples, lambda s: s.heart_rate)
    avg_hr_raw = average(values) if values else None
    return _HrChannel(
        elapsed=elapsed,
        values=values,
        avg_hr_raw=avg_hr_raw,
        avg_hr=round(avg_hr_raw) if avg_hr_raw is not None else None,
        max_hr=round(max(values)) if values else None,
    )


@dataclass(frozen=True, slots=True)
class _RunGap:
    """Résultat intermédiaire GAP/NGP course (même raison que :class:`_PowerChannel`)."""

    ngp_speed_mps: float | None
    gap_applied: bool
    speed: list[float]
    grade: list[float | None]


def _run_gap(discipline: str, samples: list[SampleRow]) -> _RunGap:
    # -- GAP / NGP : course uniquement (§3.3.4 de la spec, cf. minetti.py).
    if discipline != "run":
        return _RunGap(None, False, [], [])
    speed, grade = _run_gap_series(samples)
    if not speed:
        return _RunGap(None, False, speed, grade)
    ngp = normalized_graded_pace_speed_mps(speed, grade)
    # "Appliqué" signifie qu'une altitude exploitable a effectivement pesé sur le
    # résultat -- pas seulement qu'un calcul a eu lieu (une pente entièrement None
    # retombe sur f(0)=1 partout, cf. minetti.py).
    gap_applied = ngp is not None and any(g is not None for g in grade)
    return _RunGap(ngp, gap_applied, speed, grade)


def _swim_time(
    context: ActivityContext, swim_lengths: list[SwimLengthRow]
) -> tuple[float | None, str | None]:
    # -- Natation : temps de nage actif reconstruit depuis les longueurs FIT (§4.3).
    # Un swim SANS longueur (eau libre) n'a aucune donnée seconde par seconde
    # exploitable -- ce n'est PAS une erreur (core.sample vide est un état légitime,
    # cf. migrations/004, commentaire de core.swim_length) : on retombe alors sur le
    # temps chrono total, moins fiable, d'où le plafond de qualité "fair" appliqué par
    # l'appelant.
    if context.discipline != "swim":
        return None, None
    active_lengths = [length for length in swim_lengths if length.length_type == "active"]
    if active_lengths:
        return sum(length.duration_s for length in active_lengths), "fit_lengths"
    return float(context.duration_s), "elapsed"


def _decoupling_metric_rows(
    context: ActivityContext,
    power: _PowerChannel,
    hr: _HrChannel,
    run: _RunGap,
) -> tuple[ActivityMetricRow, ...]:
    # -- Découplage / EF : deux moitiés de durée égale sur la série "output" adaptée à
    # la discipline (puissance régularisée pour le vélo, vitesse GAP pour la course).
    # Découpage par INDEX (et non par seconde absolue) : power.regularized.values / la
    # série course filtrée sont déjà des séries "temps chrono" à 1 échantillon par
    # seconde utile (pauses excisées) -- un découpage par index y est donc un
    # découpage par durée égale, sans nouvelle hypothèse de calendrier.
    if context.discipline == "bike" and power.regularized.values:
        values = power.regularized.values
        mid = len(values) // 2
        split_t = (
            power.elapsed[len(power.elapsed) // 2] if power.elapsed else context.duration_s / 2
        )
        hr1, hr2 = _split_hr_by_time(hr, split_t)
        return _load_extra_metric_rows(
            activity_id=context.activity_id,
            discipline="bike",
            output_full=power.np_watts,
            output_half1=normalized_power(values[:mid]),
            output_half2=normalized_power(values[mid:]),
            avg_hr_full=hr.avg_hr_raw,
            avg_hr_half1=average(hr1) if hr1 else None,
            avg_hr_half2=average(hr2) if hr2 else None,
        )
    if context.discipline == "run" and run.speed:
        mid = len(run.speed) // 2
        split_t = context.duration_s / 2
        hr1, hr2 = _split_hr_by_time(hr, split_t)
        return _load_extra_metric_rows(
            activity_id=context.activity_id,
            discipline="run",
            output_full=run.ngp_speed_mps,
            output_half1=normalized_graded_pace_speed_mps(run.speed[:mid], run.grade[:mid]),
            output_half2=normalized_graded_pace_speed_mps(run.speed[mid:], run.grade[mid:]),
            avg_hr_full=hr.avg_hr_raw,
            avg_hr_half1=average(hr1) if hr1 else None,
            avg_hr_half2=average(hr2) if hr2 else None,
        )
    return ()


def _split_hr_by_time(hr: _HrChannel, split_t: float) -> tuple[list[float], list[float]]:
    pairs = zip(hr.elapsed, hr.values, strict=True)
    half1 = [v for e, v in pairs if e < split_t]
    half2 = [v for e, v in zip(hr.elapsed, hr.values, strict=True) if e >= split_t]
    return half1, half2


def build_core_rows(
    decoded: ParsedActivitySource,
    context: ActivityContext,
    *,
    athlete_id: int,
    fit_file_id: int | None,
    threshold: Threshold | None,
    all_threshold: Threshold | None,
    sex: Sex,
) -> CoreActivityBundle:
    """Chaîne le décodé (L3) et les seuils (L5) en un ``CoreActivityBundle`` complet.

    ``context.activity_id`` DOIT être cohérent avec l'``activity_id`` déjà porté par
    ``decoded`` (fourni à ``decode_fit``/``decode_tcx`` par l'appelant) : une
    incohérence signifierait qu'on assemble le résumé d'une activité avec le fichier
    d'une autre, l'un des bugs les plus coûteux possibles ici -- elle est donc détectée
    plutôt que silencieusement ignorée.

    ``sex`` est SANS VALEUR PAR DEFAUT, deliberement : il selectionne le coefficient du
    TRIMP de Banister (0.64/1.92 homme, 0.86/1.67 femme). Se tromper decale toute la
    charge d'entrainement d'environ 15 %, uniformement et sans lever la moindre
    erreur : l'appelant doit le fournir. ``transform.runner`` le lit dans
    ``core.athlete``, ou une contrainte CHECK le restreint a male/female.
    """
    if context.duration_s <= 0:
        msg = f"context.duration_s doit être > 0 (recu {context.duration_s})"
        raise ValueError(msg)
    samples = decoded.samples
    for sample in samples:
        if sample.activity_id != context.activity_id:
            msg = (
                f"incoherence activity_id : contexte={context.activity_id} "
                f"echantillon={sample.activity_id}"
            )
            raise ValueError(msg)

    power = _power_channel(samples)
    hr = _hr_channel(samples)
    run = _run_gap(context.discipline, samples)
    swim_time_s, swim_time_source = _swim_time(context, decoded.swim_lengths)

    cadence_elapsed, cadence_values = _channel(samples, lambda s: s.cadence)
    avg_cadence = average(cadence_values) if cadence_values else None
    speed_elapsed, speed_values = _channel(samples, lambda s: s.speed_mps)
    avg_speed_mps = average(speed_values) if speed_values else None
    distance_candidates = [s.distance_m for s in samples if s.distance_m is not None]
    distance_m = distance_candidates[-1] if distance_candidates else context.distance_m

    data_quality = power.regularized.data_quality
    if context.discipline == "swim" and swim_time_source == "elapsed" and data_quality == "good":
        # Plafond explicite de la spec (04 §4.3) : sans longueurs, le temps de nage
        # est une approximation, jamais "good".
        data_quality = "fair"

    # `core.activity.start_time` est un timestamptz. `context.start_time_local` est un
    # datetime NAIF en heure locale : l'ecrire tel quel le ferait interpreter comme de
    # l'UTC par PostgreSQL, decalant la seance de tout le decalage horaire sans qu'aucune
    # contrainte ne proteste. Le repli est atteignable (un TCX sans horodatage de
    # trackpoint laisse `decoded.start_time` a None), donc on reconstruit l'instant
    # absolu a partir du decalage que le resume Garmin fournit deja.
    if decoded.start_time is not None:
        activity_start_time = decoded.start_time
    else:
        activity_start_time = context.start_time_local.replace(
            tzinfo=timezone(timedelta(seconds=context.tz_offset_s))
        )
    activity_row = ActivityRow(
        activity_id=context.activity_id,
        athlete_id=athlete_id,
        sport_code=context.sport_code,
        garmin_type_key=context.garmin_type_key,
        start_time=activity_start_time,
        start_time_local=context.start_time_local,
        local_date=context.start_time_local.date(),
        tz_offset_s=context.tz_offset_s,
        duration_s=context.duration_s,
        elapsed_s=context.elapsed_s,
        moving_s=context.moving_s,
        distance_m=distance_m,
        ascent_m=context.ascent_m,
        descent_m=context.descent_m,
        avg_hr=hr.avg_hr,
        max_hr=hr.max_hr,
        avg_power=power.avg_power,
        max_power=power.max_power,
        avg_power_nonzero=power.avg_power_nonzero,
        np_watts=power.np_watts,
        np_reliable=power.np_reliable,
        avg_speed_mps=avg_speed_mps,
        avg_cadence=avg_cadence,
        calories=context.calories,
        ngp_speed_mps=run.ngp_speed_mps,
        gap_applied=run.gap_applied,
        swim_time_s=swim_time_s,
        swim_time_source=swim_time_source,
        total_samples=len(samples),
        imputed_seconds=power.regularized.imputed_seconds,
        excised_seconds=power.regularized.excised_seconds,
        data_quality=data_quality,
        parent_activity_id=context.parent_activity_id,
        is_parent=context.is_parent,
        leg_order=context.leg_order,
        primary_device_id=context.primary_device_id,
        fit_file_id=fit_file_id,
        summary_sha256=context.summary_sha256,
        name=context.name,
        needs_manual_rpe=False,  # calcule apres needs_manual_rpe(), corrige plus bas.
    )

    hr_hist = compute_hr_histogram([s.heart_rate for s in samples])
    hr_histogram_rows = tuple(
        ActivityHrHistogramRow(activity_id=context.activity_id, hr_bpm=bpm, seconds=seconds)
        for bpm, seconds in sorted(hr_hist.items())
    )

    power_curve_rows: tuple[ActivityPowerCurveRow, ...] = ()
    curve_kind = _POWER_CURVE_KIND.get(context.discipline)
    if curve_kind is not None:
        power_curve_rows += _build_power_curve_rows(
            context.activity_id, curve_kind, power.regularized.values
        )
    hr_reg = regularize_series(hr.elapsed, hr.values, fill_value=0.0)
    power_curve_rows += _build_power_curve_rows(context.activity_id, "heart_rate", hr_reg.values)

    load_input = ActivityLoadInput(
        activity_id=context.activity_id,
        sport_code=context.sport_code,
        discipline=context.discipline,
        duration_s=float(context.duration_s),
        distance_m=distance_m,
        swim_time_s=swim_time_s,
        np_watts=power.np_watts,
        ngp_speed_mps=run.ngp_speed_mps,
        hr_histogram=hr_hist,
        threshold=threshold,
        all_threshold=all_threshold,
        sex=sex,
    )
    metric_rows = build_activity_metric_rows(load_input)
    metric_rows += _decoupling_metric_rows(context, power, hr, run)

    activity_row = replace(activity_row, needs_manual_rpe=needs_manual_rpe(metric_rows))

    return CoreActivityBundle(
        activity=activity_row,
        samples=tuple(samples),
        laps=tuple(decoded.laps),
        swim_lengths=tuple(decoded.swim_lengths),
        hr_histogram=hr_histogram_rows,
        power_curve=power_curve_rows,
        metrics=metric_rows,
    )
