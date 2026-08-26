"""Archive original (FIT/TCX) files of individual activities (batch L12).

The JSON poll (:mod:`t0.scheduler.service`) yields only the *summary* of an
activity: native Running Power and swim length detail—the two signals that truly matter
for this athlete (see ``profil-athlete``)—live only in the original ``.FIT``/``.TCX``
file. This module chains for any given activity: download
(:meth:`t0.garmin.client.GarminClient.download_original`), archive to PVC
(:func:`t0.fit.archive.archive_download`), and record the ``raw.fit_file`` row
(:func:`t0.garmin.raw_store.insert_fit_file`)—then a higher-level function
repeats this sequence over the list of activities for one day.

Core invariant, non-negotiable (drawn from :mod:`t0.fit.archive`): **the file
lands on PVC BEFORE any row in the database, never the reverse**. A ``raw.fit_file``
row pointing to a missing ``storage_path`` is unrecoverable (L3 parsing will fail
forever on that activity); a PVC file with no associated row is at worst wasted disk
space, discoverable and re-recordable on the next pass. The code below respects this
order by construction: ``archive_download`` is called, then ONLY if it succeeds,
``insert_fit_file`` is called in turn—never the other way, and no database write is
attempted if archiving raised.

This module imports ``garminconnect`` only for ``GarminConnectAuthenticationError``—
same choice as :mod:`t0.garmin.client`: ``dl_fmt`` stays an opaque parameter
(``Any``) supplied by the actual caller, who already has
``garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL``. Keeping that symbol out of
this module leaves it testable with a minimal fake client, with no network-library
dependency. ``GarminConnectAuthenticationError`` is different: it is an exception TYPE
that this module must explicitly recognize to never swallow (see the note on auth
propagation below)—not a symbol specific to transport.

Idempotence, revised and corrected (blocking bug found in review—the old version of
this module treated "a ``raw.fit_file`` row exists for this activity" as equivalent to
"the file is definitely complete and correct", which locked forever a long activity
re-polled mid-Garmin-upload or simply truncated on first pass):

- The DDL (``migrations/002_raw.sql``) does NOT require a ``garmin_activity_id`` to have
  only one ``raw.fit_file`` row—instead, the index ``fit_file_activity_idx ON
  raw.fit_file (garmin_activity_id, downloaded_at DESC)`` documents a "multiple versions
  over time" model, which this module now honors.
- For a CLOSED window (``window_date < today``, explicitly injected by the caller—never
  ``date.today()`` inline, same convention as ``run_scheduled_poll``/
  ``t0.ops.backfill``), the latest known version is treated as final: no network
  call is made, ``"already_present"`` is returned immediately. Garmin documents no notion
  of original file modified after a past window closes.
- For the CURRENT window (``window_date >= today``), the file is re-downloaded on each
  pass and its sha256 compared to the latest known version: identical content →
  ``"already_present"`` (nothing new); different content → new ``raw.fit_file`` row
  inserted, status ``"refreshed"`` (e.g., the activity was still uploading on the
  previous pass and has grown since).

On WHICH bytes the sha256 is computed (real bug found in review, fixed here): Garmin's
"original" download is a ZIP archive containing the ``.fit``/``.tcx`` (see
``GarminClient.download_original``), while ``raw.fit_file.sha256`` holds the sha of the
EXTRACTED member—``archive_bytes`` computes it after re-reading the written file.
Comparing the ZIP envelope's sha to a ``raw.fit_file`` row's can NEVER match: the
idempotence comparison failed systematically, every activity on the current day was
re-archived and reported ``"refreshed"`` on each pass, and the filename (qualified by
that same sha) could vary pass to pass if Garmin does not re-emit a bit-identical ZIP—
leaving a PVC orphan per pass with no ``raw.fit_file`` row referencing it. This module
thus extracts the member itself (:func:`t0.fit.archive.extract_payload`) BEFORE
hashing, and manipulates only those bytes: a single sha, the one that ends up in the
database.

This redesign has a consequence for the filename on PVC: two versions of the same
activity (one truncated, one complete) must never share the same path—else the second
silently overwrites the first during the atomic window of ``archive_download`` (write
temp → fsync → reread → verify → ``os.replace``), which would be harmless for final
content but would leave the ``storage_path`` of the OLD ``raw.fit_file`` row incoherent
with the bytes it now points to. The filename thus includes an excerpt of the sha256 of
the content (``activity-{id}-{sha256[:12]}``), not just the activity id.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from garminconnect import GarminConnectAuthenticationError

from t0.fit.archive import archive_download, extract_payload
from t0.fit.errors import ArchiveError
from t0.garmin.raw_store import DBConnection, FitFileShaConflictError, insert_fit_file

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from t0.garmin.client import IngestRunTracker, OriginalActivityFile

logger = logging.getLogger(__name__)


class OriginalFileDownloader(Protocol):
    """Structural view of :class:`t0.garmin.client.GarminClient`.

    A Protocol rather than a direct dependency on ``GarminClient``: this module needs
    only this one method, and tests inject a fake client that implements it alone—
    without rebuilding a complete ``GarminClient`` (transport, tokenstore, rate
    limiter...) out of scope here.
    """

    def download_original(
        self,
        activity_id: str,
        *,
        dl_fmt: Any,
        tracker: IngestRunTracker | None = None,
    ) -> OriginalActivityFile: ...


@dataclass(frozen=True, slots=True)
class _LatestFitFile:
    """Latest known ``raw.fit_file`` row for an activity (see :func:`_latest_fit_file`)."""

    fit_file_id: int
    sha256: bytes
    storage_path: str


def _latest_fit_file(conn: DBConnection, *, garmin_activity_id: int) -> _LatestFitFile | None:
    """Latest archived version of an activity, or ``None`` if none exists yet.

    ``ORDER BY downloaded_at DESC LIMIT 1``—not a bare ``LIMIT 1`` without sorting (bug
    found in review: without ``ORDER BY``, PostgreSQL guarantees NO order for returned
    rows, so "the" chosen row could vary call to call, even across multiple passes on
    the SAME multi-version activity). Sorting by ``downloaded_at`` descending makes the
    choice deterministic: always the most recently archived version, consistent with the
    ``fit_file_activity_idx`` index already placed on this column pair by
    ``migrations/002_raw.sql`` (so no extra sorting cost in practice).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fit_file_id, sha256, storage_path
              FROM raw.fit_file
             WHERE garmin_activity_id = %s
             ORDER BY downloaded_at DESC
             LIMIT 1;
            """,
            (garmin_activity_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _LatestFitFile(fit_file_id=int(row[0]), sha256=bytes(row[1]), storage_path=str(row[2]))


def _dest_dir_for(archive_root: Path, window_date: date) -> Path:
    """Subdirectory ``archive_root/YYYY/MM`` for the polled window date.

    A single flat directory for all activities across all history would end up with tens
    of thousands of entries (years of history, multiple sessions per day): some
    filesystems degrade significantly beyond a few thousand entries per directory, and
    any tool listing the directory (backup, manual inspection) becomes painful. Year/month
    split yields directories with dozens to at most hundreds of files for an athlete
    training multiple times a day.

    The date used is from the POLLED window (the calendar day known to the caller,
    consistent with ``raw.poll_window``/``raw.api_response.scope_date``), not a date
    extracted from the file content: parsing the FIT for a timestamp is L3's job, out of
    scope for this module which only archives bytes.
    """
    return archive_root / f"{window_date:%Y}" / f"{window_date:%m}"


@dataclass(frozen=True, slots=True)
class ActivityFileOutcome:
    """Result of processing one activity through :func:`archive_activity_file`.

    ``status`` distinguishes five cases so the caller (and the operator reading logs)
    never has to infer the outcome from field presence/absence alone:

    - ``"archived"``: first version of this activity—download + archive + insert all
      succeeded on this pass.
    - ``"refreshed"``: a version existed already, but the re-downloaded content differs
      (e.g., activity was still uploading on the previous pass)—new ``raw.fit_file`` row
      inserted for this activity.
    - ``"already_present"``: nothing new—either the window is closed and a version
      existed (no network call made), or the re-downloaded content is identical to the
      latest known version.
    - ``"duplicate_content"``: the downloaded and archived content has the same sha256
      as a ``raw.fit_file`` row already associated with a DIFFERENT activity (see
      :class:`t0.garmin.raw_store.FitFileShaConflictError`—multisport/brick
      scenario). The file remains on PVC (assumed orphan, see PVC-before-database
      invariant at the top of this module), but no row is created for THIS activity:
      ``fit_file_id`` stays ``None``, ``error`` carries the detail.
    - ``"failed"``: download, archive, or insert failed for some other reason; ``error``
      carries the message, ``fit_file_id``/``storage_path`` stay ``None``.
    """

    activity_id: str
    status: Literal["archived", "refreshed", "already_present", "duplicate_content", "failed"]
    fit_file_id: int | None = None
    storage_path: Path | None = None
    error: str | None = None

    def as_log_line(self) -> str:
        """Human-readable output for an operator, one line per processed activity."""
        if self.status in ("archived", "refreshed"):
            verb = "archived" if self.status == "archived" else "updated"
            return (
                f"activity {self.activity_id}: {verb} → {self.storage_path} "
                f"(fit_file_id={self.fit_file_id})"
            )
        if self.status == "already_present":
            return f"activity {self.activity_id}: already archived (fit_file_id={self.fit_file_id})"
        if self.status == "duplicate_content":
            return f"activity {self.activity_id}: DUPLICATE CONTENT ({self.error})"
        return f"activity {self.activity_id}: FAILED ({self.error})"


def archive_activity_file(
    client: OriginalFileDownloader,
    conn: DBConnection,
    *,
    activity_id: str,
    window_date: date,
    today: date,
    archive_root: Path,
    dl_fmt: Any,
    tracker: IngestRunTracker | None = None,
) -> ActivityFileOutcome:
    """Download, archive, and record the original file of ONE activity.

    ``today`` is explicitly injected by the caller (never ``date.today()`` inline, same
    convention as ``run_scheduled_poll``/``t0.ops.backfill``): this is what
    distinguishes a closed window (strict idempotence, no network call) from an open
    window (re-download + content comparison—see the idempotence note at the top of this
    module for the full reasoning).

    Strict sequence—see the invariant note at the top of this module:

    1. Convert ``activity_id`` to an integer, once, guarded: the rest reuses
       ``garmin_activity_id``. A malformed identifier never raises outside this function
       (unchanged contract: isolation per activity)—it yields a ``"failed"``.
    2. Look up the latest known version (:func:`_latest_fit_file`). If the window is
       closed and a version exists → immediate ``"already_present"``, no network.
    3. ``client.download_original`` (network). A ``GarminConnectAuthenticationError`` is
       re-raised WITHOUT being converted to ``"failed"`` (see below); any other exception
       → ``"failed"``, nothing is written anywhere. Counting via
       ``tracker.record_error()``/``record_success()`` for this step is already done by
       ``download_original`` itself—not repeating it here avoids double-counting.
    4. If a version existed and the re-downloaded content has the SAME sha256 →
       ``"already_present"``: nothing new to archive.
    5. ``archive_download`` (PVC), with a filename qualified by the content's sha256 (see
       the note at the top of this module on inter-version overwrite risk). Any exception
       (:class:`ArchiveError`, ``OSError``) → ``"failed"`` AND ``tracker.record_error()``
       (this step is NOT already counted by ``download_original``); ``archive_download``
       already guarantees no partial file is left on the volume in this case (see its
       docstring).
    6. ``insert_fit_file`` (database), ONLY if step 5 succeeded. :class:`FitFileShaConflictError`
       (multisport/brick scenario, see ``raw_store.py``) → ``"duplicate_content"`` AND
       ``tracker.record_error()``; any OTHER exception here → ``"failed"`` AND
       ``tracker.record_error()``, but the file archived in step 5 STAYS on PVC
       (orphan, by design—see the invariant at the top of this module).

    On a denied authentication (``GarminConnectAuthenticationError``), this function never
    swallows it behind a ``"failed"`` status: it propagates to the caller. An
    authentication failure retried activity by activity, looping over dozens of daily
    activities, is exactly the pattern that locks a Garmin account (same
    reasoning as ``client.py``/``retry.py``)—it must interrupt all day processing, not be
    counted as one failure among others.

    Apart from that case, each activity stays isolated: this function never raises on a
    download/archive/insert failure (it captures them and returns them in
    ``ActivityFileOutcome.error``)—it is :func:`archive_activities_for_day` that calls
    this in a loop without one failure interrupting the others.
    """
    try:
        garmin_activity_id = int(activity_id)
    except (TypeError, ValueError) as exc:
        logger.warning("Invalid activity id %r: %s", activity_id, exc)
        return ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))

    latest = _latest_fit_file(conn, garmin_activity_id=garmin_activity_id)
    if latest is not None and window_date < today:
        # Closed window: Garmin documents no notion of original file modified after
        # the fact—skipping network is safe (see the idempotence note at the top of
        # this module).
        return _already_present(activity_id, latest)

    # Split into two sub-functions (download+compare, then archive+insert) only to
    # stay under the PLR0911 limit (return statements per function) without
    # sacrificing linear readability of the sequence—see their docstrings for details
    # of each step, already documented above.
    download_result = _download_and_check_content(
        client, activity_id=activity_id, dl_fmt=dl_fmt, tracker=tracker, latest=latest
    )
    if isinstance(download_result, ActivityFileOutcome):
        return download_result
    payload, content_sha256 = download_result

    return _archive_and_insert(
        conn,
        activity_id=activity_id,
        garmin_activity_id=garmin_activity_id,
        content=payload,
        content_sha256=content_sha256,
        window_date=window_date,
        archive_root=archive_root,
        latest=latest,
        tracker=tracker,
    )


def _already_present(activity_id: str, latest: _LatestFitFile) -> ActivityFileOutcome:
    """Shortcut common to both "nothing new" cases (closed window, or identical content)."""
    return ActivityFileOutcome(
        activity_id=activity_id,
        status="already_present",
        fit_file_id=latest.fit_file_id,
        storage_path=Path(latest.storage_path),
    )


def _download_and_check_content(
    client: OriginalFileDownloader,
    *,
    activity_id: str,
    dl_fmt: Any,
    tracker: IngestRunTracker | None,
    latest: _LatestFitFile | None,
) -> tuple[bytes, bytes] | ActivityFileOutcome:
    """Download, unwrap the ZIP, then compare to the sha256 of the latest known version.

    Returns either ``(bytes to archive, their sha256)`` for the caller to proceed to
    archiving, or directly a terminal ``ActivityFileOutcome`` (failure, or identical
    content to the latest version—nothing to archive).

    The bytes returned are from the ARCHIVE MEMBER, not the ZIP envelope served by
    Garmin: this is the only way to get a sha256 comparable to ``raw.fit_file.sha256``
    (see the "On WHICH bytes the sha256 is computed" note at the top of this module—
    comparing the envelope never matched).

    Counting via ``tracker.record_error()``/``record_success()`` for the download itself
    is already done by ``client.download_original``—not repeating it here avoids
    double-counting. Extraction is not counted by anyone else: its failure thus does
    call ``record_error()`` (same handling as archiving).
    """
    try:
        downloaded = client.download_original(activity_id, dl_fmt=dl_fmt, tracker=tracker)
    except GarminConnectAuthenticationError:
        # Deliberate propagation, never swallowed—see archive_activity_file docstring.
        raise
    except Exception as exc:  # isolation per activity, see docstring
        logger.warning("Activity %s download failed: %s", activity_id, exc)
        return ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))

    try:
        payload = extract_payload(downloaded.content)
    except (ArchiveError, OSError) as exc:
        if tracker is not None:
            tracker.record_error()
        logger.warning("Activity %s download unreadable (archive): %s", activity_id, exc)
        return ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))

    content_sha256 = hashlib.sha256(payload).digest()
    if latest is not None and content_sha256 == latest.sha256:
        # Identical to the latest known version: nothing new to archive, even if the
        # window was still open at pass time.
        return _already_present(activity_id, latest)
    return payload, content_sha256


def _archive_and_insert(
    conn: DBConnection,
    *,
    activity_id: str,
    garmin_activity_id: int,
    content: bytes,
    content_sha256: bytes,
    window_date: date,
    archive_root: Path,
    latest: _LatestFitFile | None,
    tracker: IngestRunTracker | None,
) -> ActivityFileOutcome:
    """Archive to PVC then record the ``raw.fit_file`` row—strict order.

    See the PVC-before-database invariant at the top of this module: ``insert_fit_file``
    is called ONLY if ``archive_download`` succeeded, never the other way.
    """
    dest_dir = _dest_dir_for(archive_root, window_date)
    # The content's sha256 qualifies the filename: two versions of the same activity
    # (truncated then complete) must never share the same path on PVC, else the second
    # silently overwrites the version the OLD raw.fit_file row still points to—see the
    # note at the top of this module.
    # ``content`` is already the extracted member (see _download_and_check_content), so
    # ``content_sha256`` is indeed the sha that ``archive_bytes`` will recalculate after
    # re-reading: passing it as ``expected_sha256`` turns writing into end-to-end
    # verification, and guarantees that the sha in the filename, the one verified on
    # disk, and the one inserted in the database are a single number.
    filename_stem = f"activity-{activity_id}-{content_sha256.hex()[:12]}"
    try:
        archived = archive_download(
            content, dest_dir, filename_stem=filename_stem, expected_sha256=content_sha256
        )
    except (ArchiveError, OSError) as exc:
        if tracker is not None:
            tracker.record_error()
        logger.warning("Activity %s archiving failed: %s", activity_id, exc)
        return ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))

    try:
        fit_file_id = insert_fit_file(
            conn,
            garmin_activity_id=garmin_activity_id,
            storage_path=str(archived.storage_path),
            sha256=archived.sha256,
            size_bytes=archived.size_bytes,
        )
    except FitFileShaConflictError as exc:
        if tracker is not None:
            tracker.record_error()
        # File deliberately stays on PVC (orphan): see the invariant at the top of this
        # module. Do NOT delete it here—a database failure must never cause a file
        # already written and verified to disappear.
        logger.warning(
            "Inserting raw.fit_file for activity %s: content duplicate with another "
            "activity (file kept at %s): %s",
            activity_id,
            archived.storage_path,
            exc,
        )
        return ActivityFileOutcome(
            activity_id=activity_id, status="duplicate_content", error=str(exc)
        )
    except Exception as exc:  # isolation per activity, see docstring
        if tracker is not None:
            tracker.record_error()
        logger.warning(
            "Inserting raw.fit_file for activity %s failed (file kept at %s): %s",
            activity_id,
            archived.storage_path,
            exc,
        )
        return ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))

    return ActivityFileOutcome(
        activity_id=activity_id,
        status="refreshed" if latest is not None else "archived",
        fit_file_id=fit_file_id,
        storage_path=archived.storage_path,
    )


@dataclass(slots=True)
class DayArchiveSummary:
    """Summary of a call to :func:`archive_activities_for_day`, one entry per activity."""

    outcomes: list[ActivityFileOutcome] = field(default_factory=list)

    @property
    def archived(self) -> list[ActivityFileOutcome]:
        return [o for o in self.outcomes if o.status == "archived"]

    @property
    def refreshed(self) -> list[ActivityFileOutcome]:
        return [o for o in self.outcomes if o.status == "refreshed"]

    @property
    def already_present(self) -> list[ActivityFileOutcome]:
        return [o for o in self.outcomes if o.status == "already_present"]

    @property
    def duplicate_content(self) -> list[ActivityFileOutcome]:
        return [o for o in self.outcomes if o.status == "duplicate_content"]

    @property
    def failed(self) -> list[ActivityFileOutcome]:
        return [o for o in self.outcomes if o.status == "failed"]

    def as_log_lines(self) -> list[str]:
        """One human-readable line per activity, in processing order."""
        return [outcome.as_log_line() for outcome in self.outcomes]


def archive_activities_for_day(
    client: OriginalFileDownloader,
    conn: DBConnection,
    *,
    activity_ids: Sequence[str],
    window_date: date,
    today: date,
    archive_root: Path,
    dl_fmt: Any,
    tracker: IngestRunTracker | None = None,
) -> DayArchiveSummary:
    """Apply :func:`archive_activity_file` to each activity in ``activity_ids``.

    Failure isolation at the day level: an activity that fails (network, unrecognized
    format, I/O, database) never blocks processing of the following ones—exactly the
    same principle as ``run_scheduled_poll`` (batch L6) at the window level. The
    ``except Exception`` catch below is deliberately redundant with the one already in
    :func:`archive_activity_file`: it guards against any unexpected exception that
    might still bubble up here, ensuring the invariant "a failed activity never
    interrupts others" holds even in this edge case.

    ``GarminConnectAuthenticationError`` deliberately makes an EXCEPTION to this
    isolation: it traverses this loop without being swallowed by the defensive catch
    below (same reasoning as in :func:`archive_activity_file`—an authentication failure
    retried activity by activity is exactly the pattern that locks a Garmin account).
    """
    summary = DayArchiveSummary()
    for activity_id in activity_ids:
        try:
            outcome = archive_activity_file(
                client,
                conn,
                activity_id=activity_id,
                window_date=window_date,
                today=today,
                archive_root=archive_root,
                dl_fmt=dl_fmt,
                tracker=tracker,
            )
        except GarminConnectAuthenticationError:
            # Deliberate propagation, never swallowed—see docstring above.
            raise
        except Exception as exc:  # isolation per failure, see docstring
            logger.warning(
                "Processing activity %s: unexpected exception outside the normal "
                "archive_activity_file path: %s",
                activity_id,
                exc,
            )
            outcome = ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))
        summary.outcomes.append(outcome)
        logger.info(outcome.as_log_line())
    return summary
