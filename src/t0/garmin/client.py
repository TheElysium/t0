"""T0 Garmin client: persistent session, rate-controlled, raw first.

This module orchestrates other modules in :mod:`t0.garmin` around an injected
transport (:class:`~t0.garmin.transport.GarminTransport`): ``GarminClient`` never
constructs ``garminconnect.Garmin(email, password)`` itself. This construction (with real
credentials read from ``t0.settings``) is the responsibility of the service
entry point — out of scope for this layer — precisely so this entire module remains
testable without network: tests inject a fake transport.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from garminconnect import GarminConnectAuthenticationError

from t0.garmin.archive import extract_original_filename
from t0.garmin.rate_limit import RateLimiter
from t0.garmin.raw_store import (
    DBConnection,
    finish_ingest_run,
    insert_api_response,
    start_ingest_run,
)
from t0.garmin.retry import RetryPolicy, call_with_retry
from t0.garmin.tokenstore import ensure_tokenstore, tokenstore_fingerprint

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import date
    from pathlib import Path

    from t0.garmin.transport import GarminTransport

logger = logging.getLogger(__name__)


class GarminClientError(RuntimeError):
    """Generic error in this module, distinct from ``garminconnect`` exceptions."""


@dataclass(frozen=True, slots=True)
class OriginalActivityFile:
    """Raw bytes and actual filename from an 'original' activity download.

    Decoding (FIT or TCX) is deliberately not done here: that is L3's responsibility.
    """

    activity_id: str
    filename: str
    content: bytes


@dataclass(slots=True)
class IngestRunTracker:
    """Counters for an in-progress ingestion run, backed by a ``raw.ingest_run`` row."""

    conn: DBConnection
    ingest_run_id: int
    requests: int = 0
    errors: int = 0

    def record_success(self) -> None:
        self.requests += 1

    def record_error(self) -> None:
        self.requests += 1
        self.errors += 1


@contextmanager
def ingest_run(
    conn: DBConnection,
    *,
    mode: str,
    code_version: str | None = None,
) -> Iterator[IngestRunTracker]:
    """Open a ``raw.ingest_run`` row, close it with a coherent status on exit.

    - Normal exit, no errors recorded -> ``ok``.
    - Normal exit, at least one error recorded via :meth:`IngestRunTracker.record_error` ->
      ``partial``.
    - Unhandled exception crossing the block -> ``failed``, with the exception message in
      ``notes``, then the exception is re-raised (it is never swallowed).
    """
    run_id = start_ingest_run(conn, mode=mode, code_version=code_version)
    tracker = IngestRunTracker(conn=conn, ingest_run_id=run_id)
    try:
        yield tracker
    except Exception as exc:
        finish_ingest_run(
            conn,
            ingest_run_id=run_id,
            status="failed",
            notes={"requests": tracker.requests, "errors": tracker.errors, "error": str(exc)},
        )
        raise
    else:
        final_status = "ok" if tracker.errors == 0 else "partial"
        finish_ingest_run(
            conn,
            ingest_run_id=run_id,
            status=final_status,
            notes={"requests": tracker.requests, "errors": tracker.errors},
        )


@dataclass(slots=True)
class GarminClient:
    """Wrapper around a :class:`~t0.garmin.transport.GarminTransport`.

    All external dependencies (network transport, DB connection, clock, sleep) are
    injected fields with sensible production defaults, replaceable in tests.
    """

    transport: GarminTransport
    tokenstore_path: Path
    conn: DBConnection
    rate_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(min_interval_s=1.5))
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], None] = field(default=time.sleep)

    def login(self) -> None:
        """Authenticate, reusing the tokenstore if it already exists.

        Explicitly logs (``WARNING`` level) every occasion when the tokenstore was empty
        at call time: this signals that a full SSO likely just occurred, which should
        remain exceptional (a pod restart should never cause it if the tokenstore volume is
        properly mounted).

        Performs **no** retry on authentication failure (``GarminConnectAuthenticationError``):
        this is exactly what exposes the Garmin account to lockout.
        """
        had_tokens = ensure_tokenstore(self.tokenstore_path)
        before = tokenstore_fingerprint(self.tokenstore_path)
        if had_tokens:
            logger.info(
                "Garmin session restored from existing tokenstore (%s).",
                self.tokenstore_path,
            )
        else:
            logger.warning(
                "Full Garmin SSO: tokenstore (%s) was empty. "
                "This must remain exceptional — verify volume mount if this "
                "message appears at every startup.",
                self.tokenstore_path,
            )

        # ONE ATTEMPT ONLY, deliberately: ``login`` is **never** passed through
        # ``call_with_retry``. In ``garminconnect`` 0.3.2, ``Garmin.login`` converts
        # SSO 429 to ``GarminConnectTooManyRequestsError`` ("Too many login
        # attempts. Please wait a few minutes") and *any* other failure to
        # ``GarminConnectConnectionError(f"Login failed: {e}")`` (``__init__.py``
        # lines ~544 and ~579) — two types the backoff policy treats as transient.
        # Retrying them chains full SSOs seconds apart: the exact loop that locks out
        # a Garmin account. A login failure is handled by a new scheduled run, not tight
        # retry.
        try:
            self.transport.login(tokenstore=str(self.tokenstore_path))
        except GarminConnectAuthenticationError:
            logger.error(
                "Garmin authentication denied: immediate abort, no retry "
                "(retrying a login failure in a loop is what locks out an account)."
            )
            raise
        except Exception:
            logger.exception(
                "Garmin login failed: immediate abort, no retry. Even a seemingly "
                "transient error (SSO 429, connection error) is treated as final here — "
                "re-execution is the scheduler's responsibility, at minute scale, not "
                "second scale."
            )
            raise
        finally:
            after = tokenstore_fingerprint(self.tokenstore_path)
            if after != before:
                logger.warning(
                    "Garmin tokens (%s) were REWRITTEN during this login: "
                    "fingerprint %s -> %s. This signals a full SSO or token refresh, "
                    "not mere reuse.",
                    self.tokenstore_path,
                    before or "<absent>",
                    after or "<absent>",
                )

    def fetch(
        self,
        endpoint: str,
        *args: Any,
        scope_date: date | None = None,
        scope_key: str | None = None,
        tracker: IngestRunTracker | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call ``endpoint`` on transport, rate-limited and backoff-bounded, write raw.

        ``endpoint`` is the name of the ``garminconnect`` method to invoke (e.g.,
        ``"get_sleep_data"``), resolved dynamically on the injected transport — this
        module knows the signature of none of the ~130 endpoint methods, only their
        common contract (callable, returns JSON-compatible).

        The response is written to ``raw.api_response`` **before** being returned to the
        caller: this is the ELT principle "raw first". A downstream transformation error
        is replayed from ``raw`` without re-requesting Garmin.

        Raises:
            garminconnect.GarminConnectAuthenticationError: propagated immediately, no
                retry.
            t0.garmin.retry.RetryExhaustedError: if all retries allowed for a
                transient error (429, connection) are exhausted.
        """
        method = cast("Callable[..., Any]", getattr(self.transport, endpoint))

        def _call() -> Any:
            self.rate_limiter.wait()
            return method(*args, **kwargs)

        try:
            payload = call_with_retry(
                _call,
                policy=self.retry_policy,
                sleep=self.sleep,
                logger=logger,
                op_name=endpoint,
            )
        except Exception:
            if tracker is not None:
                tracker.record_error()
            raise

        response_id = insert_api_response(
            self.conn,
            endpoint=endpoint,
            payload=payload,
            scope_date=scope_date,
            scope_key=scope_key,
            ingest_run_id=tracker.ingest_run_id if tracker is not None else None,
        )
        logger.debug("%s response persisted to raw.api_response (id=%s).", endpoint, response_id)
        if tracker is not None:
            tracker.record_success()
        return payload

    def download_original(
        self,
        activity_id: str,
        *,
        dl_fmt: Any,
        tracker: IngestRunTracker | None = None,
    ) -> OriginalActivityFile:
        """Download an activity's 'original' archive (ZIP containing .fit or .tcx).

        ``dl_fmt`` must be ``garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL`` on
        the actual caller side. This module does not import or construct it itself — the
        caller (who already possesses the real ``garminconnect`` transport) provides it,
        avoiding this module depending on a network-implementation-specific symbol.

        Does **not** write to ``raw.api_response`` (``payload`` column typed ``jsonb``,
        unsuitable for binary) nor to ``raw.fit_file`` (L3's decision, which must first
        write the file to PVC before any row, atomicity rule §6.5). This client stops at
        delivering bytes and the real filename.
        """

        def _call() -> bytes:
            self.rate_limiter.wait()
            return self.transport.download_activity(activity_id, dl_fmt=dl_fmt)

        try:
            content = call_with_retry(
                _call,
                policy=self.retry_policy,
                sleep=self.sleep,
                logger=logger,
                op_name=f"download_activity[{activity_id}]",
            )
        except Exception:
            if tracker is not None:
                tracker.record_error()
            raise

        filename = extract_original_filename(content)
        if tracker is not None:
            tracker.record_success()
        return OriginalActivityFile(activity_id=activity_id, filename=filename, content=content)
