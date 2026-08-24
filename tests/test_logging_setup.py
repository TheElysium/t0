"""Tests for :mod:`garminator.logging_setup`.

These tests reconfigure the root logger, which is process-global. Each restores the
original state in a finally block: otherwise a test leaving level at ERROR would silence
the caplog of a later test, and the culprit would be unfindable as failure appears
elsewhere.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from garminator.logging_setup import LOG_LEVEL_ENV_VAR, configure_logging, resolve_log_level

if TYPE_CHECKING:
    import pytest


def _snapshot() -> tuple[int, list[logging.Handler]]:
    root = logging.getLogger()
    return root.level, list(root.handlers)


def _restore(state: tuple[int, list[logging.Handler]]) -> None:
    root = logging.getLogger()
    root.setLevel(state[0])
    root.handlers[:] = state[1]


def test_default_level_lets_info_through() -> None:
    """Fix point: logger.info must now produce output.

    Unconfigured, root logger at WARNING and 23 code calls disappeared without trace.
    """
    state = _snapshot()
    try:
        configure_logging(None)
        assert logging.getLogger("garminator.garmin.retry").isEnabledFor(logging.INFO)
    finally:
        _restore(state)


def test_format_includes_timestamp_level_and_module(capsys: pytest.CaptureFixture[str]) -> None:
    """Without these three fields, a log line correlates to nothing later."""
    state = _snapshot()
    try:
        configure_logging("INFO")
        logging.getLogger("garminator.garmin.retry").info("wait %ss", 30)
        output = capsys.readouterr()
        # stdout not stderr: CronJobs already write reports there, Kubernetes doesn't
        # guarantee relative order of two streams.
        assert output.err == ""
        assert "INFO" in output.out
        assert "garminator.garmin.retry" in output.out
        assert "wait 30s" in output.out
        assert output.out.startswith("20")  # ISO timestamp at line start
    finally:
        _restore(state)


def test_unknown_value_does_not_fail_process() -> None:
    """Typo in convenience setting must not cost the daily poll.

    A ValueError here would fail the CronJob before first query, athlete loses daily
    data for unrelated setting.
    """
    state = _snapshot()
    try:
        configure_logging("VERBOSE")
        assert logging.getLogger().level == logging.INFO
    finally:
        _restore(state)


def test_repeated_calls_do_not_duplicate_lines(capsys: pytest.CaptureFixture[str]) -> None:
    """force=True replaces handler instead of stacking another."""
    state = _snapshot()
    try:
        configure_logging("INFO")
        configure_logging("INFO")
        logging.getLogger("garminator.test").info("once only")
        assert capsys.readouterr().out.count("once only") == 1
    finally:
        _restore(state)


def test_env_var_is_the_chart_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name read here is what ConfigMap writes—the contract the chart assumes."""
    state = _snapshot()
    try:
        monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "WARNING")
        configure_logging()
        assert logging.getLogger().level == logging.WARNING
    finally:
        _restore(state)


def test_resolve_accepts_case_and_whitespace() -> None:
    """YAML value copied by hand often arrives with case or space variance."""
    assert resolve_log_level(" debug ") == logging.DEBUG
    assert resolve_log_level("Error") == logging.ERROR
    assert resolve_log_level("") == logging.INFO
    assert resolve_log_level(None) == logging.INFO
