"""Application logging setup, called by each CLI entry point.

REAL BUG FIXED HERE. 23 logger.info/warning calls across 7 modules produced
nothing exploitable without basicConfig:
- logger.info silently discarded (root logger defaults to WARNING).
- logger.warning fell to logging.lastResort: no timestamp, level, or module name.

Impact: t0.garmin.retry and client log Garmin auth/backoff—critical
for detecting rate-limit sequences on accounts without 2FA (silent auth
failures lead to account lock). Timestamps/levels are essential to reconstruct
these sequences.

Why read environment directly instead of Settings: Settings requires PostgreSQL
DSN and Garmin credentials (no defaults). Read-only tools like
`python -m t0.ops reconcile` have no reason to provide credentials
just to set log level. Log level is read here alone, standalone.

Why stdout not stderr: CronJobs write reports to stdout. Stderr would
interleave two streams with no Kubernetes ordering guarantee; job completion
messages would appear mid-journal. Single stream, single order.
"""

from __future__ import annotations

import logging
import os
import sys

#: Environment variable for log verbosity. Same prefix as Settings (T0_)
#: for chart consistency, though this module deliberately bypasses Settings (see module doc).
LOG_LEVEL_ENV_VAR = "T0_LOG_LEVEL"

#: INFO not WARNING: these are scheduled tasks reviewed after completion.
#: Silent log on success leaves no audit trail; replaying to verify costs extra Garmin requests.
DEFAULT_LOG_LEVEL = "INFO"

#: Timestamp first, then level, then module: order that sorts and filters cleanly at grep.
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def resolve_log_level(raw: str | None) -> int:
    """Convert environment value to logging level, never raise.

    Unknown values fall back to DEFAULT_LOG_LEVEL instead of failing—refusing
    to start a scheduled poll over a typo would deny the athlete today's data
    for an unrelated reason. Fallback is reported once logging is configured.
    """
    if raw is None or not raw.strip():
        return logging.INFO
    level = logging.getLevelNamesMapping().get(raw.strip().upper())
    return level if level is not None else logging.INFO


def _is_unrecognized_value(raw: str | None) -> bool:
    """Distinguish "INFO explicitly requested" from "unrecognized value, fell back".

    resolve_log_level returns logging.INFO in both cases; only raw text separates them.
    Operator needs to see this distinction in logs.
    """
    if raw is None or not raw.strip():
        return False
    return raw.strip().upper() not in {"INFO", "20"} and resolve_log_level(raw) == logging.INFO


def configure_logging(level: str | None = None) -> None:
    """Install the process's unique handler, writing to stdout.

    Idempotent: a second call does not duplicate (force=True replaces prior
    handlers), which matters because entry points sometimes call each other in tests.

    Args:
        level: explicit level. None reads LOG_LEVEL_ENV_VAR.
    """
    raw = level if level is not None else os.environ.get(LOG_LEVEL_ENV_VAR)
    resolved = resolve_log_level(raw)
    logging.basicConfig(
        level=resolved,
        format=_LOG_FORMAT,
        datefmt=_DATE_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    if _is_unrecognized_value(raw):
        # Fallback reported after basicConfig to be visible: without this,
        # "DEBUG produces nothing" looks like a logging bug rather than a typo.
        logging.getLogger(__name__).warning(
            "Unrecognized log level %r: falling back to %s.", raw, DEFAULT_LOG_LEVEL
        )
