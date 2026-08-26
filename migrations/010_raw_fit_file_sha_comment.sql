-- Migration 010: clarifies WHAT raw.fit_file.sha256 refers to, and documents the
-- reconciliation scan that the table comment from 002 promised from the start.
-- No modification of 002: applied migrations are not rewritten
-- (the runner verifies their checksum).
--
-- Neither structure nor data change here: only COMMENT ON. This is deliberately
-- a complete migration rather than a note in a Markdown file, because these
-- two comments are exactly what an operator reads with \d+ raw.fit_file when
-- they need to decide if a row is trustworthy.
--
-- WHY -- the 002 comment said "sha256 of the .FIT binary", which led to believing
-- hashing the download was enough. But Garmin''s "original" download is a ZIP ARCHIVE
-- containing the .fit or .tcx. The code actually compared the ENVELOPE''s sha to the
-- MEMBER''s stored here: the two could never match, so the idempotence shortcut never
-- triggered, and each pass on the current day''s window rewrote the PVC. Real defect,
-- fixed in t0.fit.archive / t0.scheduler.activity_files. The comment below
-- makes the rule explicit so it won''t need rediscovery.

COMMENT ON COLUMN raw.fit_file.sha256 IS
  'sha256 of the ARCHIVED CONTENT -- that is, of the .fit/.tcx file itself, NEVER the ZIP archive Garmin serves it in. Calculated by t0.fit.archive.archive_bytes AFTER re-reading the file written to the PVC (not in-memory bytes), which detects corruption from writing itself. UNIQUE: the same content is archived only once. It is the pivot for detecting actual re-download (section 6.2), and every caller wanting to know "is this content already in the database?" MUST hash the extracted member (t0.fit.archive.extract_payload), not the raw download -- confusing the two has already produced a real defect. Distinct from the JSON summary hash (section 6.3).';

COMMENT ON COLUMN raw.fit_file.storage_path IS
  'ABSOLUTE path of the file on the PVC, as it was mounted at write time. The filename carries the first 12 hex of sha256: a new version of the same activity (file truncated on first download, completed later) thus gets a distinct name and NEVER OVERWRITES the previous one. Corollary: a container mounting the PVC elsewhere will see all rows as "file missing" -- that is the expected symptom, not data loss.';

COMMENT ON TABLE raw.fit_file IS
  'Metadata for .FIT files archived on the PVC. The binary is NOT in the database (no bytea): ~1,090 files of several MB would bloat TOAST and every PostgreSQL dump without contributing anything queryable. MANDATORY ATOMICITY RULE: the file is written AND fsync''d on the PVC BEFORE the INSERT of this row, never the reverse. An orphaned file (no row) is harmless and recoverable by scanning; an orphaned row (no file) makes core reconstructibility false. This scan now EXISTS: `python -m t0.ops reconcile --archive-root <mount>` (module t0.ops.reconcile, CronJob ops-reconcile, PVC mounted read-only, no automatic deletion) -- see docs/RUNBOOK.md section 9. Backup corollary: the PostgreSQL dump and the PVC snapshot must be taken at the SAME point in time (see section 6.5).';
