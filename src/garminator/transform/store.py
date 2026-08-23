"""Écriture idempotente d'un ``CoreActivityBundle`` dans les tables ``core.*``.

Motif repris SANS DÉVIATION de ``garminator.garmin.raw_store`` (voir sa docstring de
module pour la justification complète) : les fonctions ci-dessous acceptent une
connexion typée par le protocole structurel :class:`DBConnection`, jamais
``psycopg.Connection`` concrètement -- ``garminator.db`` n'expose aucun utilitaire de
requête générique, et un Protocol structurel laisse les tests injecter une fausse
connexion en mémoire (aucun PostgreSQL disponible ici) tout en restant vérifié par
mypy, puisque ``psycopg.Connection`` le satisfait structurellement. Ce module n'ouvre
lui-même aucune connexion et n'importe ``psycopg`` qu'au niveau des types (``Jsonb``,
utilisée pour les colonnes ``jsonb``).

STRATÉGIE D'IDEMPOTENCE, tranchée délibérément différemment selon la table :

- ``core.activity`` (une ligne par activité, avec des colonnes qui accueillent des
  décisions humaines a posteriori -- ``needs_manual_rpe``, un futur ``name`` retouché)
  utilise un ``INSERT ... ON CONFLICT (activity_id) DO UPDATE``, en préservant
  ``ingested_at`` (``DEFAULT now()``, jamais réécrite) et en forçant ``updated_at =
  now()`` explicitement à chaque passage.
- Toutes les tables de séries (``core.sample``, ``core.activity_lap``,
  ``core.swim_length``, ``core.activity_hr_histogram``, ``core.activity_power_curve``,
  ``core.activity_metric``) utilisent un remplacement complet par activité : ``DELETE
  ... WHERE activity_id = %s`` puis réinsertion en lot. C'est la seule stratégie
  honnête ici, pour deux raisons qui ne sont pas symétriques :

  1. Un ``ON CONFLICT DO UPDATE`` générique par clé primaire ne supprime JAMAIS une
     ligne devenue orpheline : si un décodeur corrigé produit moins d'échantillons ou
     moins de tours qu'avant (un bug corrigé qui recolle deux tours, par exemple), les
     anciennes lignes en trop resteraient en base pour toujours, invisibles dans le
     diff du bundle mais bien réelles dans les vues ``mart``.
  2. ``core.activity_metric`` porte en plus l'index unique PARTIEL
     ``activity_metric_canonical_uq ON (activity_id, metric) WHERE is_canonical`` --
     distinct de sa clé primaire ``(activity_id, metric, method_code)``. Un
     ``ON CONFLICT`` ciblant la PK ne protège PAS de ce second index : si la méthode
     canonique d'une métrique change entre deux passages (ex. le hrTSS devient un TSS
     puissance parce qu'un capteur a été ajouté), la nouvelle ligne canonique et
     l'ancienne (que la PK ne considère pas en conflit, car ``method_code`` diffère)
     violeraient ensemble l'index partiel. Le DELETE préalable est la seule voie sûre
     qui ne suppose rien sur la stabilité de la méthode canonique d'un passage à
     l'autre.

TOUT le bundle s'écrit dans UNE seule transaction (un seul ``conn.commit()``, à la
fin) : ``core.sample`` porte ``ON DELETE CASCADE`` vers ``core.activity``, donc la
ligne ``core.activity`` doit exister avant que ses séries soient insérées -- mais
l'inverse compte tout autant. Une activité à moitié écrite (échantillons sans
métriques de charge, ou l'inverse) serait pire qu'une activité absente : elle devient
visible dans ``core.activity`` -- donc potentiellement dans un JOIN -- tout en étant
incomplète dans les vues ``mart`` qui dépendent des tables non encore écrites, sans
qu'aucune erreur ne le signale. Si une étape échoue, l'absence de ``commit()`` laisse
PostgreSQL annuler implicitement la transaction ouverte à la prochaine connexion (ou à
un ``rollback()`` explicite de l'appelant) -- rien de partiel n'est jamais visible.

``NULL`` n'est jamais recodé en 0 nulle part dans ce module : les paramètres passés à
``execute``/``executemany`` sont des tuples construits par simple projection des
dataclasses de ``garminator.transform.rows`` / ``garminator.fit.model``, jamais
retouchés. C'est la règle centrale de ``core.activity_metric.value`` (migrations/
005_core_series.sql) -- un ``trimp``/``hrtss`` non calculable (FC absente, capteur de
puissance seul) doit rester ``NULL``, jamais devenir un zéro qui ferait chuter
artificiellement une moyenne de charge.

Les contraintes ``CHECK`` de la DDL (FC 20-260, lat/lon bornées, puissance >= 0,
``seconds > 0`` sur l'histogramme, ``kind IN (...)`` sur la courbe de puissance) ne
sont PAS redoublées ici : PostgreSQL les vérifie déjà à l'écriture, et les dupliquer
en Python créerait une seconde source de vérité qui pourrait diverger de la DDL au
premier changement de migration. Ce module ne filtre ni ne clippe aucune valeur avant
insertion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from collections.abc import Sequence

    from garminator.fit.model import LapRow, SampleRow, SwimLengthRow
    from garminator.transform.rows import (
        ActivityHrHistogramRow,
        ActivityMetricRow,
        ActivityPowerCurveRow,
        ActivityRow,
        CoreActivityBundle,
    )


class _Cursor(Protocol):
    def execute(self, query: str, params: Any = ...) -> object: ...
    def executemany(self, query: str, params_seq: Sequence[Any]) -> object: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Vue structurelle minimale sur une connexion psycopg 3, pour l'injection en test."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...


def _jsonb_or_none(extra: dict[str, Any]) -> Jsonb | None:
    """``{}`` -> ``NULL`` plutôt qu'un objet JSON vide : ``extra`` n'existe que pour ne
    rien perdre des champs FIT non colonnés -- un dict vide n'a AUCUN champ à
    préserver, l'écrire porterait un bruit visuel identique sur des milliers de lignes
    ``core.sample`` sans jamais rien y ajouter d'exploitable.
    """
    return Jsonb(extra) if extra else None


@dataclass(frozen=True, slots=True)
class StoreResult:
    """Décompte des lignes écrites par :func:`store_activity_bundle`, table par table."""

    activity_id: int
    samples: int
    laps: int
    swim_lengths: int
    hr_histogram: int
    power_curve: int
    metrics: int


def _upsert_activity(cur: _Cursor, a: ActivityRow) -> None:
    """``INSERT ... ON CONFLICT (activity_id) DO UPDATE``.

    ``ingested_at`` est absente à dessein de la liste des colonnes ``SET`` : sa
    colonne porte ``DEFAULT now()`` en base et ne doit être posée qu'à la toute
    première écriture. ``updated_at`` est en revanche systématiquement remise à
    ``now()`` via l'expression SQL littérale (pas ``EXCLUDED.updated_at``, qui
    n'existe même pas : ``ActivityRow`` ne porte pas ce champ, cf. sa docstring).
    """
    cur.execute(
        """
        INSERT INTO core.activity (
            activity_id, athlete_id, sport_code, garmin_type_key,
            start_time, start_time_local, local_date, tz_offset_s,
            duration_s, elapsed_s, moving_s, distance_m, ascent_m, descent_m,
            avg_hr, max_hr, avg_power, max_power, avg_power_nonzero,
            np_watts, np_reliable, avg_speed_mps, avg_cadence, calories,
            ngp_speed_mps, gap_applied, swim_time_s, swim_time_source,
            total_samples, imputed_seconds, excised_seconds, data_quality,
            parent_activity_id, is_parent, leg_order,
            primary_device_id, fit_file_id, summary_sha256, name, needs_manual_rpe
        )
        VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        ON CONFLICT (activity_id) DO UPDATE SET
            athlete_id = EXCLUDED.athlete_id,
            sport_code = EXCLUDED.sport_code,
            garmin_type_key = EXCLUDED.garmin_type_key,
            start_time = EXCLUDED.start_time,
            start_time_local = EXCLUDED.start_time_local,
            local_date = EXCLUDED.local_date,
            tz_offset_s = EXCLUDED.tz_offset_s,
            duration_s = EXCLUDED.duration_s,
            elapsed_s = EXCLUDED.elapsed_s,
            moving_s = EXCLUDED.moving_s,
            distance_m = EXCLUDED.distance_m,
            ascent_m = EXCLUDED.ascent_m,
            descent_m = EXCLUDED.descent_m,
            avg_hr = EXCLUDED.avg_hr,
            max_hr = EXCLUDED.max_hr,
            avg_power = EXCLUDED.avg_power,
            max_power = EXCLUDED.max_power,
            avg_power_nonzero = EXCLUDED.avg_power_nonzero,
            np_watts = EXCLUDED.np_watts,
            np_reliable = EXCLUDED.np_reliable,
            avg_speed_mps = EXCLUDED.avg_speed_mps,
            avg_cadence = EXCLUDED.avg_cadence,
            calories = EXCLUDED.calories,
            ngp_speed_mps = EXCLUDED.ngp_speed_mps,
            gap_applied = EXCLUDED.gap_applied,
            swim_time_s = EXCLUDED.swim_time_s,
            swim_time_source = EXCLUDED.swim_time_source,
            total_samples = EXCLUDED.total_samples,
            imputed_seconds = EXCLUDED.imputed_seconds,
            excised_seconds = EXCLUDED.excised_seconds,
            data_quality = EXCLUDED.data_quality,
            parent_activity_id = EXCLUDED.parent_activity_id,
            is_parent = EXCLUDED.is_parent,
            leg_order = EXCLUDED.leg_order,
            primary_device_id = EXCLUDED.primary_device_id,
            fit_file_id = EXCLUDED.fit_file_id,
            summary_sha256 = EXCLUDED.summary_sha256,
            name = EXCLUDED.name,
            needs_manual_rpe = EXCLUDED.needs_manual_rpe,
            updated_at = now();
        """,
        (
            a.activity_id,
            a.athlete_id,
            a.sport_code,
            a.garmin_type_key,
            a.start_time,
            a.start_time_local,
            a.local_date,
            a.tz_offset_s,
            a.duration_s,
            a.elapsed_s,
            a.moving_s,
            a.distance_m,
            a.ascent_m,
            a.descent_m,
            a.avg_hr,
            a.max_hr,
            a.avg_power,
            a.max_power,
            a.avg_power_nonzero,
            a.np_watts,
            a.np_reliable,
            a.avg_speed_mps,
            a.avg_cadence,
            a.calories,
            a.ngp_speed_mps,
            a.gap_applied,
            a.swim_time_s,
            a.swim_time_source,
            a.total_samples,
            a.imputed_seconds,
            a.excised_seconds,
            a.data_quality,
            a.parent_activity_id,
            a.is_parent,
            a.leg_order,
            a.primary_device_id,
            a.fit_file_id,
            a.summary_sha256,
            a.name,
            a.needs_manual_rpe,
        ),
    )


def _replace_samples(cur: _Cursor, activity_id: int, rows: Sequence[SampleRow]) -> int:
    cur.execute("DELETE FROM core.sample WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    # executemany plutôt qu'un execute() par ligne : une séance longue produit des
    # milliers de core.sample (jusqu'à un aller simple ~2 h en course, donc ~7000
    # lignes). Un aller-retour réseau par ligne dominerait totalement le temps
    # d'écriture face à un lot unique -- ce n'est pas une micro-optimisation, c'est la
    # différence entre une ingestion en dessous de la seconde et plusieurs secondes
    # par activité, multipliées par des centaines d'activités à chaque rejeu complet.
    cur.executemany(
        """
        INSERT INTO core.sample (
            activity_id, t, elapsed_s, latitude_deg, longitude_deg, altitude_m,
            distance_m, speed_mps, heart_rate, cadence, power, temperature,
            vertical_oscillation, stance_time, step_length, vertical_ratio, extra
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        [
            (
                s.activity_id,
                s.t,
                s.elapsed_s,
                s.latitude_deg,
                s.longitude_deg,
                s.altitude_m,
                s.distance_m,
                s.speed_mps,
                s.heart_rate,
                s.cadence,
                s.power,
                s.temperature,
                s.vertical_oscillation,
                s.stance_time,
                s.step_length,
                s.vertical_ratio,
                _jsonb_or_none(s.extra),
            )
            for s in rows
        ],
    )
    return len(rows)


def _replace_laps(cur: _Cursor, activity_id: int, rows: Sequence[LapRow]) -> int:
    cur.execute("DELETE FROM core.activity_lap WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO core.activity_lap (
            activity_id, lap_index, start_time, duration_s, elapsed_s, distance_m,
            avg_speed_mps, avg_hr, max_hr, avg_power, max_power, avg_cadence,
            ascent_m, descent_m, intensity, lap_trigger, swim_stroke, num_lengths, extra
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        [
            (
                lap.activity_id,
                lap.lap_index,
                lap.start_time,
                lap.duration_s,
                lap.elapsed_s,
                lap.distance_m,
                lap.avg_speed_mps,
                lap.avg_hr,
                lap.max_hr,
                lap.avg_power,
                lap.max_power,
                lap.avg_cadence,
                lap.ascent_m,
                lap.descent_m,
                lap.intensity,
                lap.lap_trigger,
                lap.swim_stroke,
                lap.num_lengths,
                _jsonb_or_none(lap.extra),
            )
            for lap in rows
        ],
    )
    return len(rows)


def _replace_swim_lengths(cur: _Cursor, activity_id: int, rows: Sequence[SwimLengthRow]) -> int:
    cur.execute("DELETE FROM core.swim_length WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO core.swim_length (
            activity_id, length_index, lap_index, start_time, duration_s,
            length_type, swim_stroke, total_strokes, avg_cadence
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        [
            (
                length.activity_id,
                length.length_index,
                length.lap_index,
                length.start_time,
                length.duration_s,
                length.length_type,
                length.swim_stroke,
                length.total_strokes,
                length.avg_cadence,
            )
            for length in rows
        ],
    )
    return len(rows)


def _replace_hr_histogram(
    cur: _Cursor, activity_id: int, rows: Sequence[ActivityHrHistogramRow]
) -> int:
    cur.execute("DELETE FROM core.activity_hr_histogram WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO core.activity_hr_histogram (activity_id, hr_bpm, seconds)
        VALUES (%s, %s, %s);
        """,
        [(h.activity_id, h.hr_bpm, h.seconds) for h in rows],
    )
    return len(rows)


def _replace_power_curve(
    cur: _Cursor, activity_id: int, rows: Sequence[ActivityPowerCurveRow]
) -> int:
    cur.execute("DELETE FROM core.activity_power_curve WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO core.activity_power_curve (
            activity_id, kind, duration_s, value, spans_pause
        )
        VALUES (%s, %s, %s, %s, %s);
        """,
        [(p.activity_id, p.kind, p.duration_s, p.value, p.spans_pause) for p in rows],
    )
    return len(rows)


def _replace_metrics(cur: _Cursor, activity_id: int, rows: Sequence[ActivityMetricRow]) -> int:
    """Remplacement complet -- voir la docstring du module pour l'index unique partiel.

    Un simple ``ON CONFLICT`` sur la PK ``(activity_id, metric, method_code)`` ne
    protégerait pas ``activity_metric_canonical_uq`` (partiel, sur
    ``(activity_id, metric) WHERE is_canonical``) quand la méthode canonique change
    entre deux passages : les deux lignes -- l'ancienne canonique et la nouvelle --
    portent des ``method_code`` différents, donc la PK ne les voit jamais en conflit,
    et l'INSERT violerait l'index partiel. Supprimer d'abord toutes les lignes de
    l'activité rend cette collision structurellement impossible, quel que soit
    l'historique des méthodes déjà écrites.
    """
    cur.execute("DELETE FROM core.activity_metric WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO core.activity_metric (
            activity_id, metric, method_code, value, is_canonical, threshold_id
        )
        VALUES (%s, %s, %s, %s, %s, %s);
        """,
        [
            (m.activity_id, m.metric, m.method_code, m.value, m.is_canonical, m.threshold_id)
            for m in rows
        ],
    )
    return len(rows)


def store_activity_bundle(conn: DBConnection, bundle: CoreActivityBundle) -> StoreResult:
    """Écrit tout ``bundle`` (produit par ``garminator.transform.activity.build_core_rows``)
    dans les sept tables ``core.*`` qu'il couvre, en UNE transaction (un seul
    ``commit()``, à la fin -- voir la docstring du module pour ce que casserait un
    commit intermédiaire). ``core.activity`` est écrite en premier : ``core.sample``
    porte ``ON DELETE CASCADE`` vers ``core.activity``, donc ses lignes ne peuvent pas
    être insérées avant que la ligne parente n'existe.

    ``core.device`` et ``core.sample_derived`` ne sont pas écrites : ``bundle`` n'en
    porte de toute façon aucune ligne (``bundle.sample_derived`` est toujours vide --
    voir la docstring de ``garminator.transform.rows``), rien à peupler ici.
    """
    with conn.cursor() as cur:
        _upsert_activity(cur, bundle.activity)
        n_samples = _replace_samples(cur, bundle.activity.activity_id, bundle.samples)
        n_laps = _replace_laps(cur, bundle.activity.activity_id, bundle.laps)
        n_swim_lengths = _replace_swim_lengths(
            cur, bundle.activity.activity_id, bundle.swim_lengths
        )
        n_hr_histogram = _replace_hr_histogram(
            cur, bundle.activity.activity_id, bundle.hr_histogram
        )
        n_power_curve = _replace_power_curve(cur, bundle.activity.activity_id, bundle.power_curve)
        n_metrics = _replace_metrics(cur, bundle.activity.activity_id, bundle.metrics)
    conn.commit()

    return StoreResult(
        activity_id=bundle.activity.activity_id,
        samples=n_samples,
        laps=n_laps,
        swim_lengths=n_swim_lengths,
        hr_histogram=n_hr_histogram,
        power_curve=n_power_curve,
        metrics=n_metrics,
    )
