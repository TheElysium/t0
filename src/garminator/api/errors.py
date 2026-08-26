"""HTTP API layer L7 errors (upload endpoint, manual RPE entry endpoint).

Separate from :mod:`garminator.fit.errors`: these cover rejections that are neither
decoding nor archiving issues (request size, hostile archive, unknown activity,
canonical-value conflict), but HTTP boundary concerns. The :mod:`garminator.api.upload`
and :mod:`garminator.api.rpe` modules map them to explicit status codes — see their
docstrings for the mapping tables.
"""

from __future__ import annotations


class UploadError(RuntimeError):
    """Root error of this group. Never raised directly."""


class PayloadTooLargeError(UploadError):
    """Request body (or decompressed archive member) exceeds maximum accepted size.
    Raised before any write to disk: no byte of overflow is retained in memory beyond
    the threshold."""


class HostileArchiveError(UploadError):
    """ZIP archive bears a dangerous pattern: path escape (``../``, absolute path) or
    compression ratio characteristic of a decompression bomb.

    Raised in pre-inspection, before any actual member decompression: only ``ZipInfo``
    metadata (name, declared size, compressed size) is read.
    """


class ActivityNotFoundError(UploadError):
    """No ``core.activity`` row for the requested identifier: nothing to attach a manual
    RPE to. Raised before any write, so an unknown id can never leave partial state."""


class CanonicalTssConflictError(UploadError):
    """The activity already carries a canonical TSS from a measured method (power, GAP,
    HR): entering RPE without ``force`` would silently override measured data, so the
    request is refused. With ``force``, the caller takes responsibility — the measured
    row is demoted (``is_canonical = false``), never deleted."""
