"""Production ops for the t0 pipeline (batch L10).

This package groups three distinct operational needs, each in its own module to remain
testable independently:

- :mod:`t0.ops.refresh` -- refresh of materialized views ``mart.*``,
  in the dependency order imposed by migrations 007/008.
- :mod:`t0.ops.backfill` -- tracking and steering of historical backfill,
  built ON top of the L6 scheduler (:mod:`t0.scheduler`) without duplicating its
  planning logic.
- :mod:`t0.ops.health` -- real operational questions (last successful ingestion,
  failed or stuck windows, view freshness, calendar gaps), answered from the actual state
  in the database.

Like :mod:`t0.db.runner`, these modules are intentionally autonomous with
respect to :mod:`t0.settings`: they read their DSN independently (dedicated
environment variable), without requiring Garmin credentials.
"""

from __future__ import annotations
