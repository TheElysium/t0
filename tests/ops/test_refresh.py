"""Pure tests of :mod:`garminator.ops.refresh` -- no PostgreSQL required.

Only :func:`~garminator.ops.refresh.refresh_plan` is pure (takes only a
``Mapping[str, bool]`` supplied by caller); ``mart_views_populated`` and
``refresh_all_mart_views`` require a real psycopg connection and are not tested here --
see ``tests/ops/test_refresh_integration.py`` (gated by ``GARMINATOR_TEST_POSTGRES_DSN``)."""

from __future__ import annotations

import pytest

from garminator.ops.refresh import MART_VIEWS, refresh_plan


def test_all_populated_views_give_entirely_concurrent_plan() -> None:
    populated = dict.fromkeys(MART_VIEWS, True)

    plan = refresh_plan(populated)

    assert [step.view for step in plan] == list(MART_VIEWS)
    assert all(step.mode == "concurrently" for step in plan)


def test_unpopulated_view_switches_to_with_data_only_for_itself() -> None:
    populated = dict.fromkeys(MART_VIEWS, True)
    populated["mart.daily_load"] = False

    plan = refresh_plan(populated)

    by_view = {step.view: step for step in plan}
    assert by_view["mart.daily_load"].mode == "with_data"
    assert by_view["mart.activity_load"].mode == "concurrently"
    assert by_view["mart.pmc"].mode == "concurrently"


def test_plan_respects_dependency_order_daily_load_before_pmc() -> None:
    populated = dict.fromkeys(MART_VIEWS, True)

    plan = refresh_plan(populated)

    views_in_order = [step.view for step in plan]
    assert views_in_order.index("mart.daily_load") < views_in_order.index("mart.pmc")


def test_view_missing_from_population_mapping_raises_key_error() -> None:
    populated = {view: True for view in MART_VIEWS if view != "mart.pmc"}

    with pytest.raises(KeyError):
        refresh_plan(populated)


def test_sql_of_concurrent_step_contains_concurrently_keyword() -> None:
    plan = refresh_plan({"mart.pmc": True}, views=["mart.pmc"])

    assert plan[0].sql == "REFRESH MATERIALIZED VIEW CONCURRENTLY mart.pmc;"


def test_sql_of_with_data_step_does_not_contain_concurrently() -> None:
    plan = refresh_plan({"mart.pmc": False}, views=["mart.pmc"])

    assert plan[0].sql == "REFRESH MATERIALIZED VIEW mart.pmc WITH DATA;"
    assert "CONCURRENTLY" not in plan[0].sql
