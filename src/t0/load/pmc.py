"""CTL/ATL/TSB by recurrence (04-load-formulas.md §6).

Exclusively recursive: closed form diverges numerically in double precision beyond
years (04 §6.7: (1-alpha)^-j reaches 6.61e3 at 1yr, 2.88e11 at 3yr, 1.26e19 at 5yr).
Do NOT implement closed form variant.

Exactly mirrors semantics of recursive CTE ``rec`` in ``mart.pmc`` (migrations/007,
frozen): dense calendar (no skipped days, missing daily TSS = 0, per 04 §6.4),
convention ``tsb = CTL_prev_day - ATL_prev_day`` (TrainingPeaks), and masking
reliability for first 180 days after seed date (``ctl_reliable``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

CTL_ALPHA = 1.0 / 42.0
ATL_ALPHA = 1.0 / 7.0
CTL_RELIABLE_AFTER_DAYS = 180
STATIONARY_SEED_DAYS = 42


@dataclass(frozen=True, slots=True)
class PmcDay:
    day_index: int
    tss: float
    ctl: float
    atl: float
    tsb: float
    tsb_same_day: float
    ctl_reliable: bool


def run_pmc(
    daily_tss: Sequence[float],
    seed_ctl: float,
    seed_atl: float,
    ctl_alpha: float = CTL_ALPHA,
    atl_alpha: float = ATL_ALPHA,
    reliable_after_days: int = CTL_RELIABLE_AFTER_DAYS,
) -> list[PmcDay]:
    """``daily_tss[0]`` corresponds to seed day itself (mirrors ``mart.pmc`` calendar
    starting at ``seed_date`` inclusive): first returned element is already recurrence
    applied to seed, not raw seed. ``ctl_reliable`` follows ``local_date >= seed_date + 180``,
    i.e. ``day_index - 1 >= reliable_after_days`` with this indexing convention.
    """
    if seed_ctl < 0 or seed_atl < 0:
        msg = "seed CTL/ATL cannot be negative (CHECK core.pmc_seed)"
        raise ValueError(msg)

    ctl, atl = seed_ctl, seed_atl
    days: list[PmcDay] = []
    for day_index, tss in enumerate(daily_tss, start=1):
        prev_ctl, prev_atl = ctl, atl
        ctl = ctl + (tss - ctl) * ctl_alpha
        atl = atl + (tss - atl) * atl_alpha
        days.append(
            PmcDay(
                day_index=day_index,
                tss=tss,
                ctl=ctl,
                atl=atl,
                tsb=prev_ctl - prev_atl,
                tsb_same_day=ctl - atl,
                ctl_reliable=(day_index - 1) >= reliable_after_days,
            )
        )
    return days


def dense_daily_tss(
    tss_by_date: Mapping[date, float], seed_date: date, end_date: date
) -> list[float]:
    """Dense calendar of daily TSS, ready for :func:`run_pmc`.

    Python mirror of CTE ``d`` in ``mart.pmc`` (migration 007): ``generate_series`` with
    ``LEFT JOIN`` on ``mart.daily_load``, ``COALESCE(dl.tss, 0)``. Day without activity
    is 0, not absent: only way CTL/ATL decline during breaks (04 §6.4). Do not iterate
    only on activity days—that bug keeps CTL from declining.

    Intentional asymmetry: daily TSS missing = 0.0, but at activity level incalculable
    TSS = None. Both rules coexist without contradiction (04 §6.4 corollary).

    Dates in ``tss_by_date`` outside [seed_date, end_date] ignored silently, like
    ``LEFT JOIN`` on bounded calendar.
    """
    if end_date < seed_date:
        msg = f"end_date {end_date} before seed_date {seed_date}: empty calendar"
        raise ValueError(msg)
    n_days = (end_date - seed_date).days + 1
    return [tss_by_date.get(seed_date + timedelta(days=offset), 0.0) for offset in range(n_days)]


def stationary_seed(
    first_days_tss: Sequence[float], window_days: int = STATIONARY_SEED_DAYS
) -> tuple[float, float]:
    """Stationary seed (04 §6.5.3, ``core.pmc_seed.strategy = 'stationary_42d'``):
    mean of daily TSS for first ``window_days`` (42) days of history.

    Window is hardcoded here, not caller's choice: strategy name stored in DB
    (``stationary_42d``) specifies precise day count. Accidentally passing entire history
    would yield plausible but false annual average—exact failure mode this project refuses.
    Shorter series accepted as-is (nascent history).

    Calculate ONCE on window anchored at history start date, store result. Never call on
    sliding window (04 §6.5 warns against induced circularity).
    """
    if not first_days_tss:
        msg = "empty series: cannot compute stationary seed"
        raise ValueError(msg)
    if window_days < 1:
        msg = f"invalid stationary seed window: {window_days} day(s)"
        raise ValueError(msg)
    window = first_days_tss[:window_days]
    mean = sum(window) / len(window)
    return mean, mean
