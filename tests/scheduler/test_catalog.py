"""Tests for :mod:`t0.scheduler.catalog` (batch L11).

The most valuable test in this batch: ``test_endpoint_exists_and_matches_declared_call_shape``
reverifies, by introspection of ``garminconnect.Garmin`` really installed (without
instantiating it, without network), that each declared endpoint exists and the declared
call shape matches its real signature. Without it, a ``garminconnect`` update
that renames a parameter or changes a default value would only be detected in
production, at the very first poll failure.

``skipif`` (via ``importorskip``) : this test module must never fail due to
missing dependency, only due to detected drift when it is present.
"""

from __future__ import annotations

import inspect
from datetime import date

import pytest

from t0.scheduler.catalog import (
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
    assert method is not None, f"{spec.name} no longer exists on installed garminconnect.Garmin"

    sig = inspect.signature(method)
    params = [p for name, p in sig.parameters.items() if name != "self"]

    if spec.call_shape is CallShape.DAY:
        # f(cdate: str) -- single mandatory argument, no default value.
        assert len(params) >= 1, f"{spec.name} : unexpected signature {sig}"
        first = params[0]
        assert first.name == "cdate", f"{spec.name} : first parameter {first.name!r} != cdate"
        assert (
            first.default is inspect.Parameter.empty
        ), f"{spec.name} : 'cdate' should not have a default value"
    else:
        # f(startdate: str, enddate: str | None = None, ...) -- RANGE and
        # ACTIVITIES_BY_DATE share this same call shape.
        assert len(params) >= 2, f"{spec.name} : unexpected signature {sig}"
        first, second = params[0], params[1]
        assert (
            first.name == "startdate"
        ), f"{spec.name} : first parameter {first.name!r} != startdate"
        assert (
            first.default is inspect.Parameter.empty
        ), f"{spec.name} : 'startdate' should not have a default value"
        assert (
            second.name == "enddate"
        ), f"{spec.name} : second parameter {second.name!r} != enddate"
        assert second.default is None, f"{spec.name} : 'enddate' should default to None"


def test_catalog_has_no_duplicate_endpoint_names() -> None:
    names = [spec.name for spec in CATALOG]
    assert len(names) == len(set(names))


def test_default_endpoints_are_all_known() -> None:
    assert all(is_known_endpoint(name) for name in DEFAULT_ENDPOINTS)


def test_default_endpoints_are_relevant_for_triathlete() -> None:
    """Without locking the exact list (it can evolve), verify that the major families
    mentioned in the brief are well represented: sleep, HRV, activities."""
    assert "get_sleep_data" in DEFAULT_ENDPOINTS
    assert "get_hrv_data" in DEFAULT_ENDPOINTS
    assert "get_activities_by_date" in DEFAULT_ENDPOINTS


def test_is_known_endpoint_rejects_unknown_name() -> None:
    assert not is_known_endpoint("get_totally_made_up_endpoint")


def test_endpoint_spec_raises_value_error_for_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown endpoint"):
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
