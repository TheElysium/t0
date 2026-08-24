"""Refresh of materialized views ``mart.*`` (batch L10).

Three facts, verified in migrations 007 and 008 (not guessed), underpin this module:

1. **Dependency order.** Only one materialized view reads another: ``mart.pmc`` does
   ``LEFT JOIN mart.daily_load`` (migration 007, CTE ``d``). No other ``mart`` view
   appears in the SQL body of another (verified by textual search on both migrations:
   the only occurrence of ``FROM mart.`` or ``JOIN mart.`` is that one). ``mart.
   daily_load`` must be refreshed before ``mart.pmc``; the other four views
   (``activity_load``, ``power_curve_best``, ``hr_zone_time``, ``activity_wide``)
   read only ``core.*`` and have no ordering constraint with each other or the first two.
2. **``CONCURRENTLY`` requires a UNIQUE index.** All six views have one
   (``activity_load_pk``, ``daily_load_pk``, ``pmc_pk``, ``power_curve_best_pk``,
   ``hr_zone_time_pk``, ``activity_wide_pk``) -- condition explicitly documented by
   the comment on ``mart.activity_load`` (migration 007): "The UNIQUE index is not
   decorative: it is the sine qua non condition for REFRESH MATERIALIZED VIEW
   CONCURRENTLY".
3. **``CONCURRENTLY`` rejects an unpopulated view.** ``CREATE MATERIALIZED VIEW ...``
   (without ``WITH NO DATA``, not used by either migration) populates the view at
   creation: in normal operation, this case happens only once, just after
   ``python -m garminator.db migrate`` on a new DB, though PostgreSQL has already
   populated views -- ``ispopulated`` is True once migration is applied. The
   ``ispopulated = False`` case can only truly occur after manual ``REFRESH ... WITH
   NO DATA`` (outside this module) or partial restore from ``pg_dump`` without data.
   This module detects it via ``pg_matviews.ispopulated`` and switches to non-concurrent
   refresh for that view specifically rather than letting PostgreSQL return the bare
   error ("materialized view "..." has not been populated").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    import psycopg

logger = logging.getLogger(__name__)

#: Materialized views of ``mart``, in correct refresh order. See point 1 of module
#: docstring for justification from migrations 007/008.
MART_VIEWS: tuple[str, ...] = (
    # Refreshed even though no dashboard reads it: migration 011 makes it a
    # counter-expert view, its gap with mart.activity_wide reveals if a threshold was
    # adjusted after ingestion. This gap only makes sense if it stays current.
    "mart.activity_load",
    "mart.daily_load",
    "mart.pmc",
    "mart.power_curve_best",
    "mart.hr_zone_time",
    "mart.activity_wide",
)

RefreshMode = Literal["concurrently", "with_data"]


@dataclass(frozen=True, slots=True)
class RefreshStep:
    """A planned step: refresh ``view`` using ``mode``."""

    view: str
    mode: RefreshMode

    @property
    def sql(self) -> str:
        """Corresponding SQL statement -- no parameterized bindings: view name is not
        parameterized in ``REFRESH MATERIALIZED VIEW`` (not a column value), and
        ``MART_VIEWS`` is a frozen tuple from the repo, never user input."""
        if self.mode == "concurrently":
            return f"REFRESH MATERIALIZED VIEW CONCURRENTLY {self.view};"
        return f"REFRESH MATERIALIZED VIEW {self.view} WITH DATA;"


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """Outcome of executing a :class:`RefreshStep`."""

    step: RefreshStep
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


def refresh_plan(
    populated: Mapping[str, bool], *, views: Iterable[str] = MART_VIEWS
) -> list[RefreshStep]:
    """Determine, for each view in ``views`` in the given order, the refresh mode to apply:
    ``with_data`` (non-concurrent) if ever unpopulated per ``populated``, ``concurrently``
    otherwise.

    Pure function -- ``populated`` supplied by caller (typically read from
    ``pg_matviews.ispopulated``), making it testable without PostgreSQL.

    Raises:
        KeyError: if a view in ``views`` is missing from ``populated`` -- caller must
            query state of ALL views before planning, never assume a default (optimistic
            default risks PostgreSQL rejecting ``CONCURRENTLY``; pessimistic default
            wrongly loses concurrent mode, locking Grafana for no reason).
    """
    steps: list[RefreshStep] = []
    for view in views:
        if view not in populated:
            raise KeyError(
                f"Unknown population state for {view}: query pg_matviews before "
                "planning (see mart_views_populated)."
            )
        mode: RefreshMode = "concurrently" if populated[view] else "with_data"
        steps.append(RefreshStep(view=view, mode=mode))
    return steps


def mart_views_populated(
    conn: psycopg.Connection[tuple[Any, ...]], *, views: Iterable[str] = MART_VIEWS
) -> dict[str, bool]:
    """Read ``pg_matviews.ispopulated`` for each view in ``views``.

    Raises:
        RuntimeError: if expected view does not appear in ``pg_matviews`` -- schema not
            migrated, or wrong view name. Better to fail immediately than silently
            incomplete planning.
    """
    names = list(views)
    schema_names = [name.split(".", 1)[1] for name in names]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schemaname || '.' || matviewname, ispopulated "
            "FROM pg_matviews WHERE schemaname = 'mart' AND matviewname = ANY(%s);",
            (schema_names,),
        )
        rows = cur.fetchall()
    found = {row[0]: bool(row[1]) for row in rows}
    missing = [name for name in names if name not in found]
    if missing:
        raise RuntimeError(
            f"Materialized view(s) not found in pg_matviews: {', '.join(missing)}. "
            "Were mart migrations (007/008) applied?"
        )
    return found


def refresh_all_mart_views(
    conn: psycopg.Connection[tuple[Any, ...]], *, views: Iterable[str] = MART_VIEWS
) -> list[RefreshOutcome]:
    """Refresh all views in ``views``, in order, each in its own autocommit transaction.

    A failed view does not prevent subsequent ones from being tried -- same philosophy
    as :func:`garminator.scheduler.service.run_scheduled_poll`: partial failure must
    stay visible and localized, never block the entire run. This is especially needed
    here because ``mart.pmc`` depends on ``mart.daily_load``: if ``daily_load`` refresh
    fails, ``pmc`` is still attempted (it will rework on ``daily_load`` data before that
    failure, which is not wrong -- only less fresh -- and better than interrupting the
    entire run).

    ``conn`` must be in ``autocommit=True``: ``REFRESH MATERIALIZED VIEW CONCURRENTLY``
    must not stay open in a transaction shared with other views, or a lock taken for one
    would remain active during refresh of the following ones.
    """
    populated = mart_views_populated(conn, views=views)
    plan = refresh_plan(populated, views=views)
    outcomes: list[RefreshOutcome] = []
    for step in plan:
        try:
            with conn.cursor() as cur:
                cur.execute(step.sql)
            logger.info("View %s refreshed (mode %s).", step.view, step.mode)
            outcomes.append(RefreshOutcome(step=step))
        except Exception as exc:
            logger.warning("Refresh of %s failed: %s", step.view, exc)
            outcomes.append(RefreshOutcome(step=step, error=str(exc)))
    return outcomes
