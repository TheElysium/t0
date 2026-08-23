"""Batch L5: training load and materialized views.

Package computes load metrics (TSS/rTSS/sTSS, TRIMP, hrTSS) and canonical method
resolution (``core.metric_method_priority``), then provides pure Python implementation—
testable without PostgreSQL—of CTL/ATL/TSB recurrence already in recursive CTE form in
``mart.pmc`` (migration 007, frozen).

No module here accesses DB or network: all functions pure, receive inputs (thresholds,
HR histograms, NP/GAP values from L4 batch) as arguments, return values or structures
ready for ``core.activity_metric`` by ingestion layer (out of this batch scope).
"""

from __future__ import annotations
