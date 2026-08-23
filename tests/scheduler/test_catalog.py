"""Tests de :mod:`garminator.scheduler.catalog` (lot L11).

Le test le plus précieux de ce lot : ``test_endpoint_exists_and_matches_declared_call_shape``
revérifie, par introspection de ``garminconnect.Garmin`` réellement installée (sans
l'instancier, sans réseau), que chaque endpoint déclaré existe et que la forme d'appel
déclarée correspond à sa vraie signature. Sans lui, une mise à jour de ``garminconnect``
qui renomme un paramètre ou change une valeur par défaut ne serait détectée qu'en
production, au tout premier échec de sondage.

``skipif`` (via ``importorskip``) : ce module de test ne doit jamais échouer faute de
dépendance absente, seulement faute de dérive détectée quand elle est présente.
"""

from __future__ import annotations

import inspect
from datetime import date

import pytest

from garminator.scheduler.catalog import (
    CATALOG,
    DEFAULT_ENDPOINTS,
    CallShape,
    EndpointSpec,
    call_args,
    endpoint_spec,
    is_known_endpoint,
)

garminconnect = pytest.importorskip("garminconnect")


@pytest.mark.parametrize("spec", CATALOG, ids=[spec.name for spec in CATALOG])
def test_endpoint_exists_and_matches_declared_call_shape(spec: EndpointSpec) -> None:
    method = getattr(garminconnect.Garmin, spec.name, None)
    assert method is not None, f"{spec.name} n'existe plus sur garminconnect.Garmin installé"

    sig = inspect.signature(method)
    params = [p for name, p in sig.parameters.items() if name != "self"]

    if spec.call_shape is CallShape.DAY:
        # f(cdate: str) -- un seul argument obligatoire, sans valeur par défaut.
        assert len(params) >= 1, f"{spec.name} : signature inattendue {sig}"
        first = params[0]
        assert first.name == "cdate", f"{spec.name} : premier paramètre {first.name!r} != cdate"
        assert (
            first.default is inspect.Parameter.empty
        ), f"{spec.name} : 'cdate' ne devrait pas avoir de valeur par défaut"
    else:
        # f(startdate: str, enddate: str | None = None, ...) -- RANGE et
        # ACTIVITIES_BY_DATE partagent cette même forme d'appel.
        assert len(params) >= 2, f"{spec.name} : signature inattendue {sig}"
        first, second = params[0], params[1]
        assert (
            first.name == "startdate"
        ), f"{spec.name} : premier paramètre {first.name!r} != startdate"
        assert (
            first.default is inspect.Parameter.empty
        ), f"{spec.name} : 'startdate' ne devrait pas avoir de valeur par défaut"
        assert (
            second.name == "enddate"
        ), f"{spec.name} : second paramètre {second.name!r} != enddate"
        assert second.default is None, f"{spec.name} : 'enddate' devrait défaut à None"


def test_catalog_has_no_duplicate_endpoint_names() -> None:
    names = [spec.name for spec in CATALOG]
    assert len(names) == len(set(names))


def test_default_endpoints_are_all_known() -> None:
    assert all(is_known_endpoint(name) for name in DEFAULT_ENDPOINTS)


def test_default_endpoints_are_pertinent_pour_un_triathlete() -> None:
    """Sans figer la liste exacte (elle peut évoluer), vérifie que les grandes familles
    citées par le brief sont bien représentées : sommeil, HRV, activités."""
    assert "get_sleep_data" in DEFAULT_ENDPOINTS
    assert "get_hrv_data" in DEFAULT_ENDPOINTS
    assert "get_activities_by_date" in DEFAULT_ENDPOINTS


def test_is_known_endpoint_rejects_unknown_name() -> None:
    assert not is_known_endpoint("get_totally_made_up_endpoint")


def test_endpoint_spec_raises_value_error_for_unknown_name() -> None:
    with pytest.raises(ValueError, match="endpoint inconnu"):
        endpoint_spec("get_totally_made_up_endpoint")


def test_call_args_day_shape_returns_single_iso_date() -> None:
    spec = endpoint_spec("get_sleep_data")
    assert call_args(spec, date(2026, 8, 20)) == ("2026-08-20",)


def test_call_args_range_shape_returns_same_date_twice() -> None:
    spec = endpoint_spec("get_body_composition")
    assert call_args(spec, date(2026, 8, 20)) == ("2026-08-20", "2026-08-20")


def test_call_args_activities_shape_returns_same_date_twice() -> None:
    spec = endpoint_spec("get_activities_by_date")
    assert call_args(spec, date(2026, 8, 20)) == ("2026-08-20", "2026-08-20")
