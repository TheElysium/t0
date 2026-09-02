"""Archive reconciliation: PVC <-> ``raw.fit_file`` scan promised by the DDL, read-only.

``migrations/002_raw.sql`` sets the atomicity rule (file is written and fsync to PVC
BEFORE the row INSERT) and qualifies the only tolerated anomaly: "An orphan file
(without row) is harmless and catchable by a scan; an orphan row (without file) breaks
core reconstructibility." This scan did not exist. This module IS that scan.

The two anomalies are not symmetric, and this module treats them differently:

- **Orphan file** (on PVC, no row) -- benign. Legitimately occurs when archiving succeeds
  but the following INSERT fails: the atomicity rule is meant to make THIS direction
  possible rather than the other. Also occurred, until ZIP hash fix in
  :mod:`t0.scheduler.activity_files`, on every pass over the current day window
  -- a deployment before that fix very likely accumulated some. Nothing to fix in DB:
  the file is simply unused.
- **Orphan row** (in DB, no file) -- critical. The row asserts a source file exists; any
  ``core`` reconstruction from ``raw`` depends on it and will fail, or worse, silently
  skip the activity.
- **Size mismatch** -- file exists but no longer matches the size in DB. Treated as
  critical: either truncation or file overwrite, either way the row no longer describes
  its file.

This module does NOT recalculate sha256 by default: over ~1,090 files of several MB,
rereading the entire PVC for routine checks costs a lot for an anomaly that size
comparison catches in the vast majority of cases. ``--deep`` forces content verification
when silent corruption is suspected.

**Read-only without exception.** No function here deletes files or modifies rows.
Deleting an "orphan" based on a scan is irreversible and assumes the scan is right; the
report names the files, the operator decides.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable


class _Cursor(Protocol):
    def execute(self, query: str, params: Any = ...) -> object: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Read-only structural view of the connection used by reconciliation."""

    def cursor(self) -> _Cursor: ...


#: Extensions of archived source files (see ``t0.fit.archive.sniff_format``).
#: Any other file on the PVC is ignored by the scan rather than reported as orphan: the
#: volume can legitimately carry temp files or empty directories.
ARCHIVE_SUFFIXES: frozenset[str] = frozenset({".fit", ".tcx"})

#: Block read for sha256 in ``--deep`` mode: an ultra .FIT can weigh dozens of MB,
#: and the scan walks through them all.
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class MissingFile:
    """Row ``raw.fit_file`` whose file is absent or no longer matches."""

    fit_file_id: int
    garmin_activity_id: int | None
    storage_path: str
    reason: str


@dataclass(frozen=True, slots=True)
class OrphanFile:
    """File on PVC not referenced by any ``raw.fit_file`` row."""

    path: Path
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Scan result. ``is_clean`` distinguishes benign from critical."""

    rows_scanned: int
    files_scanned: int
    missing_files: tuple[MissingFile, ...]
    orphan_files: tuple[OrphanFile, ...]
    deep: bool

    @property
    def is_clean(self) -> bool:
        """True if no CRITICAL anomalies. Orphan files are not.

        This property controls the CLI exit code: failing a CronJob because the PVC
        holds an unused file would teach ignoring its alerts, and we cannot afford to
        ignore the "orphan row" alert.
        """
        return not self.missing_files


def _file_sha256(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.digest()


def _iter_archive_files(archive_root: Path) -> Iterable[Path]:
    if not archive_root.exists():
        return ()
    return (
        path
        for path in archive_root.rglob("*")
        if path.is_file() and path.suffix.lower() in ARCHIVE_SUFFIXES
    )


def reconcile_archive(
    conn: DBConnection,
    archive_root: Path,
    *,
    deep: bool = False,
) -> ReconcileReport:
    """Compare ``raw.fit_file`` inventory against actual ``archive_root`` contents.

    Args:
        conn: PostgreSQL connection. Used read-only.
        archive_root: archive volume root (the ``fit-archive`` PVC).
        deep: recalculate sha256 for each file instead of comparing size. Costly:
            rereads entire volume.

    Returns:
        The report. A path in DB is resolved as-is: rows carry absolute paths, and an
        ``archive_root`` mounted elsewhere than at write time would report ALL rows as
        missing -- deliberately loud symptom rather than silent, a wrongly-mounted PVC
        being exactly what must be seen.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fit_file_id, garmin_activity_id, storage_path, sha256, size_bytes "
            "FROM raw.fit_file ORDER BY fit_file_id"
        )
        rows = cur.fetchall()

    missing: list[MissingFile] = []
    referenced: set[Path] = set()

    for fit_file_id, activity_id, storage_path, sha256, size_bytes in rows:
        path = Path(storage_path)
        referenced.add(path)
        if not path.is_file():
            missing.append(
                MissingFile(fit_file_id, activity_id, storage_path, "file missing from volume")
            )
            continue
        if deep:
            try:
                actual = _file_sha256(path)
            except OSError as exc:
                missing.append(
                    MissingFile(fit_file_id, activity_id, storage_path, f"unreadable: {exc}")
                )
                continue
            if actual != bytes(sha256):
                missing.append(
                    MissingFile(
                        fit_file_id,
                        activity_id,
                        storage_path,
                        f"sha256 mismatch (db {bytes(sha256).hex()[:12]}, "
                        f"disk {actual.hex()[:12]})",
                    )
                )
            continue
        actual_size = path.stat().st_size
        if actual_size != size_bytes:
            missing.append(
                MissingFile(
                    fit_file_id,
                    activity_id,
                    storage_path,
                    f"size mismatch (db {size_bytes}, disk {actual_size})",
                )
            )

    orphans = tuple(
        OrphanFile(path, path.stat().st_size)
        for path in sorted(_iter_archive_files(archive_root))
        if path not in referenced
    )

    return ReconcileReport(
        rows_scanned=len(rows),
        files_scanned=len(orphans) + (len(rows) - len(missing)),
        missing_files=tuple(missing),
        orphan_files=orphans,
        deep=deep,
    )


#: Same display bound as health report, for same reason: report scrolling hundreds of
#: lines in CronJob logs stops being read.
_MAX_LISTED_LINES = 20


def format_reconcile_report(report: ReconcileReport) -> str:
    """Text rendering, similar spirit to
    :func:`t0.ops.health.format_health_report`."""
    mode = "deep (sha256 recalculated)" if report.deep else "fast (size compared)"
    lines = [
        f"PVC reconciliation <-> raw.fit_file -- {mode} mode",
        f"  rows examined: {report.rows_scanned}",
        "",
        f"CRITICAL -- rows without valid file ({len(report.missing_files)}):",
    ]
    if not report.missing_files:
        lines.append("  (none -- all raw.fit_file rows have their file)")
    for mf in report.missing_files[:_MAX_LISTED_LINES]:
        activity = mf.garmin_activity_id if mf.garmin_activity_id is not None else "manual upload"
        lines.append(f"  #{mf.fit_file_id} (activity {activity}) {mf.storage_path}: {mf.reason}")
    if len(report.missing_files) > _MAX_LISTED_LINES:
        lines.append(f"  ... and {len(report.missing_files) - _MAX_LISTED_LINES} more.")

    lines.append("")
    lines.append(f"BENIGN -- orphan files on volume ({len(report.orphan_files)}):")
    if not report.orphan_files:
        lines.append("  (none)")
    lines.extend(
        f"  {of.path} ({of.size_bytes} bytes)" for of in report.orphan_files[:_MAX_LISTED_LINES]
    )
    if len(report.orphan_files) > _MAX_LISTED_LINES:
        lines.append(f"  ... and {len(report.orphan_files) - _MAX_LISTED_LINES} more.")
    if report.orphan_files:
        lines.append(
            "  These files are not referenced by any row: harmless, they only use "
            "space. NO deletion is done by this scan."
        )

    lines.append("")
    lines.append("Verdict: " + ("OK" if report.is_clean else "CRITICAL ANOMALY, see above"))
    return "\n".join(lines)
