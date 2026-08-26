"""Errors from batch L3 (FIT and TCX parsing).

Single error set in ``t0.fit``, reused by ``t0.tcx``: both readers
produce same intermediate structure (:mod:`t0.fit.model`) and must fail uniformly
so calling code (archive, L6, L7) doesn't maintain two exception branches.
"""

from __future__ import annotations


class SourceParseError(RuntimeError):
    """Source file (.FIT or .TCX) could not be decoded cleanly.

    Raised on: truncated file, invalid FIT CRC, malformed XML, unexpected message.
    Must never leave side effects—no archived file, no DB row. Caller responsibility
    (see :mod:`t0.fit.archive`) to enforce "write-verify-before-insert" order;
    this exception triggers refusal.
    """


class FitParseError(SourceParseError):
    """FIT file decode error."""


class TcxParseError(SourceParseError):
    """TCX file decode error (malformed XML, DOCTYPE refused, etc.)."""


class ArchiveError(RuntimeError):
    """File archiving (write, verify, or format detect) failed.

    Raised before any DB row written: see order guarantee on ``raw.fit_file``
    (migrations/002_raw.sql).
    """
