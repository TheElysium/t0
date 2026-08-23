"""Fixtures 100% synthétiques pour les tests du lot ``garminator.transform``.

AUCUN octet réel d'activité (``running.fit``/``swim.fit``/``bike.tcx``/
``Activities.csv``/``metrics&wellness_1year/``) n'apparaît ici : ces fichiers
contiennent des traces GPS qui identifient le domicile de l'athlète (cf. mandat du
lot). Toutes les valeurs ci-dessous sont construites à la main, choisies pour être
rondes et faciles à vérifier de tête (une puissance de 200 W pendant 60 s a une
moyenne de 200 W, pas un chiffre extrait d'un vrai fichier)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from garminator.fit.model import LapRow, ParsedActivitySource, SampleRow, SwimLengthRow
from garminator.load.thresholds import Threshold
from garminator.transform.activity import ActivityContext

ACTIVITY_ID = 1
ATHLETE_ID = 7
START = datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC)

#: Seuils synthétiques couvrant les trois disciplines, dans le style de
#: tests/load/test_activity_load.py.
BIKE_THRESHOLD = Threshold(
    discipline="bike", valid_from=date(2026, 1, 1), valid_to=None, threshold_id=1, ftp_watts=220.0
)
RUN_THRESHOLD = Threshold(
    discipline="run",
    valid_from=date(2026, 1, 1),
    valid_to=None,
    threshold_id=2,
    rftp_watts=None,  # jamais deviné pour cet athlète : cf. mandat du lot.
    ftp_pace_s_per_km=270.0,  # 4:30/km
)
SWIM_THRESHOLD = Threshold(
    discipline="swim", valid_from=date(2026, 1, 1), valid_to=None, threshold_id=3, css_m_s=0.909091
)
ALL_THRESHOLD = Threshold(
    discipline="all",
    valid_from=date(2026, 1, 1),
    valid_to=None,
    threshold_id=4,
    lthr_bpm=170.0,
    hr_max_bpm=190.0,
    hr_rest_bpm=55.0,
)


def make_sample(
    elapsed_s: int,
    *,
    power: int | None = None,
    heart_rate: int | None = None,
    speed_mps: float | None = None,
    distance_m: float | None = None,
    altitude_m: float | None = None,
    cadence: int | None = None,
) -> SampleRow:
    """Un ``SampleRow`` synthétique à ``elapsed_s`` secondes du départ."""
    return SampleRow(
        activity_id=ACTIVITY_ID,
        t=START + timedelta(seconds=elapsed_s),
        elapsed_s=elapsed_s,
        power=power,
        heart_rate=heart_rate,
        speed_mps=speed_mps,
        distance_m=distance_m,
        altitude_m=altitude_m,
        cadence=cadence,
    )


def bike_context(*, duration_s: int = 3600, activity_id: int = ACTIVITY_ID) -> ActivityContext:
    return ActivityContext(
        activity_id=activity_id,
        sport_code="virtual_cycling",  # home-trainer TCX, cf. profil athlète.
        discipline="bike",
        duration_s=duration_s,
        start_time_local=START,
        tz_offset_s=7200,
    )


def run_context(*, duration_s: int = 3600, activity_id: int = ACTIVITY_ID) -> ActivityContext:
    return ActivityContext(
        activity_id=activity_id,
        sport_code="running",
        discipline="run",
        duration_s=duration_s,
        start_time_local=START,
        tz_offset_s=7200,
    )


def swim_context(*, duration_s: int = 1800, activity_id: int = ACTIVITY_ID) -> ActivityContext:
    return ActivityContext(
        activity_id=activity_id,
        sport_code="lap_swimming",
        discipline="swim",
        duration_s=duration_s,
        start_time_local=START,
        tz_offset_s=7200,
    )


def parsed_source(
    samples: list[SampleRow],
    *,
    laps: list[LapRow] | None = None,
    swim_lengths: list[SwimLengthRow] | None = None,
) -> ParsedActivitySource:
    return ParsedActivitySource(
        source_format="fit",
        samples=samples,
        laps=laps or [],
        swim_lengths=swim_lengths or [],
        session=None,
        start_time=samples[0].t if samples else START,
    )
