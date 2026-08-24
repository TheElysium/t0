"""Tests de :mod:`garminator.garmin.rate_limit`."""

from __future__ import annotations

from garminator.garmin.rate_limit import RateLimiter


def test_first_call_never_sleeps() -> None:
    sleeps: list[float] = []
    limiter = RateLimiter(min_interval_s=2.0, clock=lambda: 100.0, sleep=sleeps.append)

    limiter.wait()

    assert sleeps == []


def test_second_call_within_interval_sleeps_remaining_time() -> None:
    sleeps: list[float] = []
    times = iter([100.0, 100.0, 100.5, 100.5])
    limiter = RateLimiter(min_interval_s=2.0, clock=lambda: next(times), sleep=sleeps.append)

    limiter.wait()  # now=100.0, no sleep, _last_call=100.0
    limiter.wait()  # now=100.5, ecart 0.5s < 2s -> dort 1.5s

    assert sleeps == [1.5]


def test_call_after_interval_elapsed_does_not_sleep() -> None:
    sleeps: list[float] = []
    times = iter([100.0, 100.0, 103.0, 103.0])
    limiter = RateLimiter(min_interval_s=2.0, clock=lambda: next(times), sleep=sleeps.append)

    limiter.wait()  # now=100.0
    limiter.wait()  # now=103.0, gap 3s >= 2s -> no sleep

    assert sleeps == []
