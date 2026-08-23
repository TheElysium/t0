"""Tests purs de :mod:`garminator.ops.refresh` -- aucun PostgreSQL requis.

Seule :func:`~garminator.ops.refresh.refresh_plan` est pure (elle ne prend en entrée
qu'un ``Mapping[str, bool]`` fourni par l'appelant) ; ``mart_views_populated`` et
``refresh_all_mart_views`` exigent une vraie connexion psycopg et ne sont donc pas
testées ici -- voir ``tests/ops/test_refresh_integration.py`` (gated par
``GARMINATOR_TEST_POSTGRES_DSN``)."""

from __future__ import annotations

import pytest

from garminator.ops.refresh import MART_VIEWS, refresh_plan


def test_toutes_les_vues_peuplees_donnent_un_plan_entierement_concurrently() -> None:
    populated = dict.fromkeys(MART_VIEWS, True)

    plan = refresh_plan(populated)

    assert [step.view for step in plan] == list(MART_VIEWS)
    assert all(step.mode == "concurrently" for step in plan)


def test_une_vue_jamais_peuplee_bascule_en_with_data_pour_elle_seule() -> None:
    populated = dict.fromkeys(MART_VIEWS, True)
    populated["mart.daily_load"] = False

    plan = refresh_plan(populated)

    by_view = {step.view: step for step in plan}
    assert by_view["mart.daily_load"].mode == "with_data"
    assert by_view["mart.activity_load"].mode == "concurrently"
    assert by_view["mart.pmc"].mode == "concurrently"


def test_le_plan_respecte_lordre_de_dependance_daily_load_avant_pmc() -> None:
    populated = dict.fromkeys(MART_VIEWS, True)

    plan = refresh_plan(populated)

    views_in_order = [step.view for step in plan]
    assert views_in_order.index("mart.daily_load") < views_in_order.index("mart.pmc")


def test_une_vue_absente_du_mapping_de_peuplement_leve_key_error() -> None:
    populated = {view: True for view in MART_VIEWS if view != "mart.pmc"}

    with pytest.raises(KeyError):
        refresh_plan(populated)


def test_le_sql_dune_etape_concurrently_contient_le_mot_cle_concurrently() -> None:
    plan = refresh_plan({"mart.pmc": True}, views=["mart.pmc"])

    assert plan[0].sql == "REFRESH MATERIALIZED VIEW CONCURRENTLY mart.pmc;"


def test_le_sql_dune_etape_with_data_ne_contient_pas_concurrently() -> None:
    plan = refresh_plan({"mart.pmc": False}, views=["mart.pmc"])

    assert plan[0].sql == "REFRESH MATERIALIZED VIEW mart.pmc WITH DATA;"
    assert "CONCURRENTLY" not in plan[0].sql
