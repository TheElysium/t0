"""Chains ``decoded (L3) + thresholds (L5) -> core.* rows``, then writes to the database.

The package splits in two halves, and that split is the one thing to know before
touching it:

- **The computation core is PURE.** ``activity`` and ``rows`` touch neither the
  database nor the network: they consume what ``garminator.fit.decode_fit`` /
  ``garminator.tcx.decode_tcx`` return (``ParsedActivitySource``, see
  ``garminator.fit.model``) and what ``garminator.load`` computes (TSS, TRIMP, hrTSS,
  thresholds), producing immutable dataclasses. No SQL, no connection, hence testable
  without PostgreSQL — which matters because no instance is available in the
  development environment, and this purity is what allows the whole load computation
  to be covered regardless.
- **The SQL boundary is confined to three modules.** ``summary`` (reads thresholds and
  builds the ``ActivityContext`` from the Garmin summary), ``store`` (idempotent write
  of an activity and its series), ``runner`` (consumes the ``raw.fit_file`` queue). All
  three go through a structural ``Protocol`` ``DBConnection`` — never a concrete psycopg
  type — so an in-memory double is enough to test them while mypy checks the real usage.
"""

from __future__ import annotations
