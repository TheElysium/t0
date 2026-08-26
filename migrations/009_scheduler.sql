-- Migration 009: polling scheduling (batch L6)
-- Adds raw.poll_window, the progress log for periodic polling.
-- Batch L6 (t0) -- do not modify the DDL manually outside migration.

-- Context: raw.payload_seen (migration 002) answers "has this EXACT CONTENT already
-- been seen?" -- a deduplication question at payload level, independent of
-- time. It does NOT answer "have I already properly polled Garmin for this day?",
-- which is a SCHEDULING question: what windows remain to query, and
-- which ones were interrupted by a failure in the middle of a run. This
-- second question is what raw.poll_window solves.
CREATE TABLE raw.poll_window (
    endpoint            text        NOT NULL,
    window_date         date        NOT NULL,
    status              text        NOT NULL DEFAULT 'pending',
    attempts            integer     NOT NULL DEFAULT 0,
    last_attempted_at   timestamptz NULL,
    completed_at        timestamptz NULL,
    last_ingest_run_id  bigint      NULL REFERENCES raw.ingest_run (ingest_run_id),
    last_error          text        NULL,
    CONSTRAINT poll_window_pk PRIMARY KEY (endpoint, window_date),
    CONSTRAINT poll_window_status_ck
        CHECK (status IN ('pending', 'in_progress', 'done', 'failed'))
);

COMMENT ON TABLE raw.poll_window IS
  'Progress log of periodic polling, one row per (endpoint, athlete''s local calendar day -- see raw.api_response.scope_date). Identification key deliberately DIFFERENT from raw.payload_seen: here we identify a planned WORK WINDOW, not content. (endpoint, window_date) is stable and known BEFORE even querying Garmin (unlike a payload hash, which assumes already receiving the response) -- this is what lets us know, before any network call, what windows remain due. IDEMPOTENCE RULE: a window moves to ''in_progress'' BEFORE the network call and to ''done'' only AFTER complete success; a crash in the middle leaves it ''in_progress'', never ''done'' -- the scheduler (t0.scheduler) treats any ''in_progress'' row older than a staleness threshold as an interrupted run to replay. Thus a failure can never lose an activity: at worst a window is re-polled (which raw.payload_seen then makes cheap to detect downstream). A ''done'' window can legitimately become due again: the scheduler always re-polls a recent sliding window to catch renamed, corrected, or late-arriving activities on Garmin''s side -- idempotence must never prevent legitimate re-ingestion of a newer version.';
COMMENT ON COLUMN raw.poll_window.endpoint IS
  'Name of the garminconnect method polled (same vocabulary as raw.api_response.endpoint).';
COMMENT ON COLUMN raw.poll_window.window_date IS
  'Athlete''s LOCAL date covered by this polling window (one window = one day). No finer granularity: the ~130 garminconnect endpoints used here are daily (cdate parameter).';
COMMENT ON COLUMN raw.poll_window.attempts IS
  'Number of polling attempts for this window, including the most recent failure. Incremented at each transition to ''in_progress'', never reset to zero: a counter resetting to zero would hide that a window already needed many tries.';
COMMENT ON COLUMN raw.poll_window.last_error IS
  'Message of the last error if status = ''failed''. Reset to NULL as soon as the window goes ''done'' again -- never leave an obsolete error next to a more recent success.';

CREATE INDEX poll_window_due_idx
    ON raw.poll_window (endpoint, status, window_date);

COMMENT ON INDEX raw.poll_window_due_idx IS
  'Serves the scheduling query: for a given endpoint, quickly find windows by status and date (oldest due first, see t0.scheduler.planning.due_windows).';
