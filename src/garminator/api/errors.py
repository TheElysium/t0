"""HTTP API layer L7 errors (upload endpoint).

Separate from :mod:`garminator.fit.errors`: these cover rejections that are neither
decoding nor archiving issues (request size, hostile archive), but HTTP boundary
concerns. The :mod:`garminator.api.upload` module maps them to explicit status codes —
see its docstring for the mapping table.
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
