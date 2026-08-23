"""Non-regression : decodage des vrais fichiers FIT de l'athlete, si presents.

Ces fichiers (``running.fit``, ``swim.fit``) contiennent des traces GPS qui revelent
le domicile de l'athlete : ils sont exclus du depot (voir .gitignore) et ne doivent
JAMAIS y etre copies, y compris sous ``tests/fixtures/``. Ce test les lit directement
a la racine du depot quand ils sont presents localement, et se contente de passer
proprement (``pytest.skip``, jamais d'echec) quand ils sont absents -- ce qui sera le
cas sur toute autre machine que celle de l'athlete, y compris en CI.

Activation : placer ``running.fit`` et/ou ``swim.fit`` a la racine du depot (a cote de
``pyproject.toml``). Rien d'autre a configurer -- aucune variable d'environnement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from garminator.fit.reader import decode_fit_file

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNING_FIT = _REPO_ROOT / "running.fit"
_SWIM_FIT = _REPO_ROOT / "swim.fit"


def test_decode_running_fit_if_present() -> None:
    if not _RUNNING_FIT.exists():
        pytest.skip(
            "running.fit absent (attendu : fichier reel de l'athlete, non versionne). "
            "Placer le fichier a la racine du depot pour activer ce test."
        )

    result = decode_fit_file(_RUNNING_FIT, activity_id=1)

    assert result.source_format == "fit"
    assert len(result.samples) > 0
    assert result.start_time is not None
    # Running Power est native (addendum SS1) : au moins une puissance non nulle.
    assert any(s.power is not None for s in result.samples)


def test_decode_swim_fit_if_present() -> None:
    if not _SWIM_FIT.exists():
        pytest.skip(
            "swim.fit absent (attendu : fichier reel de l'athlete, non versionne). "
            "Placer le fichier a la racine du depot pour activer ce test."
        )

    result = decode_fit_file(_SWIM_FIT, activity_id=2)

    assert result.source_format == "fit"
    # Les longueurs portent les donnees de natation, pas les record (addendum SS3).
    assert len(result.swim_lengths) > 0
    assert any(length.total_strokes is not None for length in result.swim_lengths)
    # Au moins une longueur doit etre rattachee a un tour via first_length_index/num_lengths.
    assert any(length.lap_index is not None for length in result.swim_lengths)

    # Actives et idle sont distinguees (point 4) : un fichier de natation en bassin
    # contient les deux. Aucun comptage n'est fige ici -- les volumes reels de
    # l'athlete n'ont pas a apparaitre dans le depot.
    types = {length.length_type for length in result.swim_lengths}
    assert types == {"active", "idle"}
    # Les longueurs idle sont des temps de repos : pas de style ni de coups de bras,
    # et ces champs restent None -- jamais mis a 0 (point 2).
    idle = [length for length in result.swim_lengths if length.length_type == "idle"]
    assert all(length.swim_stroke is None for length in idle)
    assert all(length.total_strokes is None for length in idle)
    # La contrainte swim_length_dur_pos (migrations/004) exige duration_s > 0.
    assert all(length.duration_s > 0 for length in result.swim_lengths)
    # En natation, aucun record ne porte puissance/cadence/vitesse : ces colonnes
    # doivent rester NULL et non pas 0.
    assert all(s.power is None for s in result.samples)
    assert all(s.speed_mps is None for s in result.samples)
