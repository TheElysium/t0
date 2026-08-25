"""Computation and storage of the PMC seed (``core.pmc_seed``).

``mart.pmc`` is a recursive view whose non-recursive term reads ``core.pmc_seed``: with
no row there, the recursion has no starting calendar and the view is EMPTY. Every PMC
panel then renders blank, without any error anywhere -- the failure is silent. The
arithmetic already lives in :mod:`garminator.load.pmc`; what was missing was the step
that actually writes the result into the table.

WHY THE SEED IS ANCHORED ON ``mart.daily_load`` AND NOT ON ``core.activity``.
CTL is an average of daily TSS, so a day whose load could not be computed is not a
zero-load day -- it is an unknown one. Anchoring the window on the first activity would
put the seed at the start of a stretch that carries no TSS at all (no threshold in
effect, no power meter, ...), average in a long run of structural zeros, and produce a
seed far below the athlete's real fitness. ``mart.daily_load`` only holds days with a
canonical TSS, so its first date is the first day the load is actually KNOWN. That is
the honest history start, and the one the 42-day window is anchored on.

WHY THE VIEW MUST BE REFRESHED FIRST. ``mart.daily_load`` is materialized. Reading it
right after an import but before ``python -m garminator.ops refresh`` returns the
PREVIOUS state, so the seed would be computed on stale data without any visible sign.
:func:`compute_pmc_seed` therefore refuses to run against an unpopulated view rather
than seeding from nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from garminator.load.pmc import STATIONARY_SEED_DAYS, dense_daily_tss, stationary_seed

if TYPE_CHECKING:
    from datetime import date

#: ``core.pmc_seed.strategy`` value written by this module. The name encodes the window
#: length, so the two must never drift apart -- see :func:`compute_pmc_seed`.
STATIONARY_STRATEGY = "stationary_42d"


class PmcSeedError(Exception):
    """The seed cannot be computed from the data currently in the database."""


@dataclass(frozen=True, slots=True)
class PmcSeed:
    athlete_id: int
    seed_date: date
    ctl: float
    atl: float
    strategy: str
    #: Days of the window that carried a known load. Not stored: it is a quality signal
    #: for the operator, not part of the seed. A window with 4 known days out of 42 is
    #: arithmetically valid and practically meaningless.
    days_with_load: int
    window_days: int


def compute_pmc_seed(
    conn: Any,
    athlete_id: int,
    seed_date: date | None = None,
    window_days: int = STATIONARY_SEED_DAYS,
) -> PmcSeed:
    """Stationary seed over the ``window_days`` days starting at ``seed_date``.

    ``seed_date`` defaults to the first date carrying a known load for this athlete.
    Passing a value overrides that, which is what a deliberate re-seed after a backfill
    does.

    ``window_days`` is exposed only so the caller can state the default explicitly; any
    other value stops the strategy from being ``stationary_42d``, and the row is stored
    as ``manual`` instead, because that is what it is.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ispopulated FROM pg_matviews "
            "WHERE schemaname = 'mart' AND matviewname = 'daily_load'"
        )
        populated = cur.fetchone()
        if populated is None:
            msg = "mart.daily_load does not exist: run `python -m garminator.db migrate` first"
            raise PmcSeedError(msg)
        if not populated[0]:
            msg = (
                "mart.daily_load is not populated: run `python -m garminator.ops refresh` "
                "before seeding, otherwise the seed is computed on stale data"
            )
            raise PmcSeedError(msg)

        if seed_date is None:
            cur.execute(
                "SELECT min(local_date) FROM mart.daily_load WHERE athlete_id = %s",
                (athlete_id,),
            )
            row = cur.fetchone()
            if row is None or row[0] is None:
                msg = (
                    f"no day with a known load for athlete_id={athlete_id}: nothing to seed "
                    "from. Import activities and refresh the mart views first"
                )
                raise PmcSeedError(msg)
            seed_date = row[0]

        end_date = seed_date + timedelta(days=window_days - 1)
        cur.execute(
            "SELECT local_date, tss FROM mart.daily_load "
            "WHERE athlete_id = %s AND local_date BETWEEN %s AND %s",
            (athlete_id, seed_date, end_date),
        )
        tss_by_date = {row[0]: float(row[1]) for row in cur.fetchall() if row[1] is not None}

    # Days absent from mart.daily_load count as 0, exactly as in the CTE `d` of
    # mart.pmc: only that makes CTL fall during a break. Averaging over activity days
    # alone would return the mean load of a SESSION, not of a DAY -- roughly double.
    daily = dense_daily_tss(tss_by_date, seed_date, end_date)
    ctl, atl = stationary_seed(daily, window_days=window_days)
    strategy = STATIONARY_STRATEGY if window_days == STATIONARY_SEED_DAYS else "manual"
    return PmcSeed(
        athlete_id=athlete_id,
        seed_date=seed_date,
        ctl=ctl,
        atl=atl,
        strategy=strategy,
        days_with_load=len(tss_by_date),
        window_days=window_days,
    )


def write_pmc_seed(conn: Any, seed: PmcSeed) -> None:
    """Insert the seed, or update it in place if one already exists on that same date.

    ``core.pmc_seed`` is keyed on ``(athlete_id, seed_date)`` and deliberately keeps the
    history of seeds: seeding a DIFFERENT date adds a row and supersedes the older one
    (``mart.pmc`` takes the most recent). Recomputing the SAME date is not a new seed
    though -- it is a correction of that one, hence the upsert rather than a second row
    that could never be told apart from the first.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.pmc_seed (athlete_id, seed_date, ctl, atl, strategy) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (athlete_id, seed_date) DO UPDATE "
            "SET ctl = EXCLUDED.ctl, atl = EXCLUDED.atl, "
            "    strategy = EXCLUDED.strategy, computed_at = now()",
            (seed.athlete_id, seed.seed_date, seed.ctl, seed.atl, seed.strategy),
        )


def format_pmc_seed(seed: PmcSeed) -> str:
    coverage = f"{seed.days_with_load}/{seed.window_days} day(s) with a known load"
    return (
        f"athlete {seed.athlete_id}: seed_date={seed.seed_date} "
        f"ctl={seed.ctl:.1f} atl={seed.atl:.1f} strategy={seed.strategy} ({coverage})"
    )
