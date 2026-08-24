"""Garminator Garmin client (batch L2).

Wrapper around ``garminconnect`` 0.3.2: persistent session (tokenstore mandatory, never
implicit), rate limiting and bounded exponential backoff, systematic write of the raw
payload to ``raw.api_response`` before any transformation, journalling in
``raw.ingest_run``. The real network transport (``garminconnect.Garmin``) is **injected**
— this package never builds it itself, so that it stays fully testable without network
(see :class:`~garminator.garmin.transport.GarminTransport`).
"""

from __future__ import annotations

from garminator.garmin.archive import ArchiveError, extract_original_filename
from garminator.garmin.client import (
    GarminClient,
    GarminClientError,
    IngestRunTracker,
    OriginalActivityFile,
    ingest_run,
)
from garminator.garmin.rate_limit import RateLimiter
from garminator.garmin.raw_store import (
    DBConnection,
    canonical_json_dumps,
    finish_ingest_run,
    insert_api_response,
    payload_sha256,
    start_ingest_run,
)
from garminator.garmin.retry import (
    NON_RETRYABLE_MESSAGE_MARKERS,
    RETRYABLE_EXCEPTIONS,
    RetryExhaustedError,
    RetryPolicy,
    call_with_retry,
    is_retryable,
)
from garminator.garmin.tokenstore import ensure_tokenstore
from garminator.garmin.transport import GarminTransport

__all__ = [
    "NON_RETRYABLE_MESSAGE_MARKERS",
    "RETRYABLE_EXCEPTIONS",
    "ArchiveError",
    "DBConnection",
    "GarminClient",
    "GarminClientError",
    "GarminTransport",
    "IngestRunTracker",
    "OriginalActivityFile",
    "RateLimiter",
    "RetryExhaustedError",
    "RetryPolicy",
    "call_with_retry",
    "canonical_json_dumps",
    "ensure_tokenstore",
    "extract_original_filename",
    "finish_ingest_run",
    "ingest_run",
    "insert_api_response",
    "is_retryable",
    "payload_sha256",
    "start_ingest_run",
]
