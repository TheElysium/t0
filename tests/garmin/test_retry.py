"""Tests for :mod:`garminator.garmin.retry`.

Covers two acceptance requirements for this batch:
- sequence 429 -> backoff -> success;
- authentication failure triggers no retry.
"""

from __future__ import annotations

import random

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectInvalidFileFormatError,
    GarminConnectTooManyRequestsError,
)

from garminator.garmin.retry import (
    RetryExhaustedError,
    RetryPolicy,
    call_with_retry,
    compute_delay,
    is_retryable,
)


def test_429_then_429_then_success_retries_and_returns_value() -> None:
    calls = 0

    def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise GarminConnectTooManyRequestsError("429")
        return "ok"

    sleeps: list[float] = []
    result = call_with_retry(
        flaky,
        policy=RetryPolicy(max_attempts=5, base_delay_s=0.01, max_delay_s=1, jitter_s=0.0),
        sleep=sleeps.append,
        rng=random.Random(0),  # noqa: S311 -- test determinism, not crypto
    )

    assert result == "ok"
    assert calls == 3
    assert len(sleeps) == 2  # one sleep after each of the two 429s


def test_connection_error_is_also_retried() -> None:
    calls = 0

    def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise GarminConnectConnectionError("500")
        return "ok"

    result = call_with_retry(
        flaky,
        policy=RetryPolicy(max_attempts=3, base_delay_s=0.01, jitter_s=0.0),
        sleep=lambda _s: None,
    )

    assert result == "ok"
    assert calls == 2


def test_authentication_error_is_never_retried() -> None:
    calls = 0

    def always_fails() -> str:
        nonlocal calls
        calls += 1
        raise GarminConnectAuthenticationError("bad credentials")

    sleeps: list[float] = []
    with pytest.raises(GarminConnectAuthenticationError):
        call_with_retry(
            always_fails,
            policy=RetryPolicy(max_attempts=5, base_delay_s=0.01, jitter_s=0.0),
            sleep=sleeps.append,
        )

    assert calls == 1  # no additional attempts
    assert sleeps == []  # no backoff triggered


def test_invalid_file_format_error_is_never_retried() -> None:
    calls = 0

    def always_fails() -> str:
        nonlocal calls
        calls += 1
        raise GarminConnectInvalidFileFormatError("not a fit file")

    with pytest.raises(GarminConnectInvalidFileFormatError):
        call_with_retry(always_fails, policy=RetryPolicy(max_attempts=5), sleep=lambda _s: None)

    assert calls == 1


def test_exhausted_retries_raise_retry_exhausted_error_chained() -> None:
    def always_429() -> str:
        raise GarminConnectTooManyRequestsError("429")

    with pytest.raises(RetryExhaustedError) as excinfo:
        call_with_retry(
            always_429,
            policy=RetryPolicy(max_attempts=3, base_delay_s=0.01, jitter_s=0.0),
            sleep=lambda _s: None,
        )

    assert isinstance(excinfo.value.__cause__, GarminConnectTooManyRequestsError)


def test_compute_delay_is_exponential_and_capped() -> None:
    policy = RetryPolicy(max_attempts=10, base_delay_s=1.0, max_delay_s=5.0, jitter_s=0.0)
    rng = random.Random(0)  # noqa: S311 -- test determinism, not crypto

    assert compute_delay(policy, attempt=1, rng=rng) == 1.0
    assert compute_delay(policy, attempt=2, rng=rng) == 2.0
    assert compute_delay(policy, attempt=3, rng=rng) == 4.0
    assert compute_delay(policy, attempt=4, rng=rng) == 5.0  # capped
    assert compute_delay(policy, attempt=10, rng=rng) == 5.0  # still capped


def test_compute_delay_jitter_is_never_negative() -> None:
    policy = RetryPolicy(max_attempts=2, base_delay_s=1.0, max_delay_s=5.0, jitter_s=0.5)
    rng = random.Random(42)  # noqa: S311 -- test determinism, not crypto

    for attempt in range(1, 5):
        delay = compute_delay(policy, attempt=attempt, rng=rng)
        floor = min(policy.base_delay_s * (2 ** (attempt - 1)), policy.max_delay_s)
        assert delay >= floor
        assert delay <= floor + policy.jitter_s


def test_retry_policy_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="delays"):
        RetryPolicy(base_delay_s=-1)


# --- Discriminating 401/403 disguised as GarminConnectConnectionError ---------
#
# ``garminconnect/client.py::_run_request`` raises ``GarminConnectConnectionError("API
# Error 401 ...")`` for any status >= 400: without message inspection, auth denial
# would be retried, and each retry re-triggers ``_refresh_session()`` to SSO.
# These tests lock in the discrimination.


@pytest.mark.parametrize(
    "message",
    [
        "API Error 401: Unauthorized",
        "API Error 403: Forbidden",
        "Login failed: authentication error",
    ],
)
def test_auth_shaped_connection_error_is_not_retryable(message: str) -> None:
    assert is_retryable(GarminConnectConnectionError(message)) is False


@pytest.mark.parametrize(
    "message",
    ["API Error 503: Service Unavailable", "Connection reset by peer", "API Error 500"],
)
def test_transient_connection_error_stays_retryable(message: str) -> None:
    assert is_retryable(GarminConnectConnectionError(message)) is True


def test_429_is_always_retryable_whatever_the_message() -> None:
    # Login 429 message contains "attempts"; dedicated type decides alone.
    assert is_retryable(GarminConnectTooManyRequestsError("Too many login attempts")) is True


def test_401_shaped_error_is_propagated_without_retry_and_without_sleeping() -> None:
    calls: list[int] = []
    slept: list[float] = []

    def _always_401() -> None:
        calls.append(1)
        raise GarminConnectConnectionError("API Error 401: Unauthorized")

    with pytest.raises(GarminConnectConnectionError):
        call_with_retry(
            _always_401,
            policy=RetryPolicy(max_attempts=5, base_delay_s=0.0, jitter_s=0.0),
            sleep=slept.append,
        )

    assert len(calls) == 1  # only one call: no retry
    assert slept == []  # and no backoff consumed
