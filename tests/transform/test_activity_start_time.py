"""D'ou vient ``core.activity.start_time``, et ce qui se passe quand le fichier se tait.

La colonne est un ``timestamptz``. PostgreSQL accepte sans broncher un datetime naif :
il l'interprete comme de l'UTC. Une seance decalee de deux heures ne viole aucune
contrainte, ne leve aucune erreur, et fausse silencieusement tout regroupement
journalier du PMC. C'est le seul defaut que ce fichier surveille.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from garminator.fit.model import ParsedActivitySource
from garminator.transform.activity import build_core_rows
from garminator.transform.summary import ActivityContext

from .conftest import ALL_THRESHOLD, ATHLETE_ID, RUN_THRESHOLD, make_sample

#: Heure locale naive telle que ``build_activity_context`` la produit depuis
#: ``startTimeLocal``, et decalage deduit de l'ecart avec ``startTimeGMT`` (09h00
#: locales - 07h00 GMT = +2 h).
LOCAL_NAIF = datetime(2026, 6, 1, 9, 0)
OFFSET_S = 7200

CONTEXT = ActivityContext(
    activity_id=1,
    sport_code="running",
    discipline="run",
    duration_s=3600,
    start_time_local=LOCAL_NAIF,
    tz_offset_s=OFFSET_S,
)


def _decoded(*, start_time: datetime | None) -> ParsedActivitySource:
    samples = [make_sample(i, heart_rate=150, speed_mps=4.2) for i in range(0, 3600, 60)]
    if start_time is not None:
        samples = [replace(s, t=start_time + timedelta(seconds=s.elapsed_s)) for s in samples]
    return ParsedActivitySource(
        source_format="fit",
        samples=samples,
        laps=[],
        swim_lengths=[],
        session=None,
        start_time=start_time,
    )


def _build(decoded: ParsedActivitySource) -> datetime:
    bundle = build_core_rows(
        decoded,
        CONTEXT,
        athlete_id=ATHLETE_ID,
        fit_file_id=None,
        threshold=RUN_THRESHOLD,
        all_threshold=ALL_THRESHOLD,
        sex="male",
    )
    return bundle.activity.start_time


def test_l_instant_du_fichier_prime_sur_celui_du_resume() -> None:
    """Le fichier porte l'horodatage de la montre ; le resume n'a qu'une precision minute.

    Les deux sources divergent volontairement de 37 secondes : sans cet ecart, le test
    passerait quelle que soit celle qui gagne, et ne prouverait rien.
    """
    instant_fichier = datetime(2026, 6, 1, 7, 0, 37, tzinfo=UTC)
    assert _build(_decoded(start_time=instant_fichier)) == instant_fichier


def test_sans_horodatage_le_fichier_muet_est_recale_par_le_decalage_du_resume() -> None:
    """Repli atteignable : un TCX sans horodatage de trackpoint ne datte rien.

    Ecrire ``start_time_local`` tel quel decalerait la seance de 2 h vers le passe sans
    qu'aucune contrainte ne proteste. On verifie donc l'instant absolu ET le fait qu'il
    porte un fuseau -- un naif serait relu comme 09h00 UTC.
    """
    obtenu = _build(_decoded(start_time=None))

    assert obtenu.tzinfo is not None
    assert obtenu == datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
    assert obtenu != datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    assert obtenu.utcoffset() == timezone(timedelta(seconds=OFFSET_S)).utcoffset(None)
