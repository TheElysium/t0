-- Migration 002: raw schema (append-only, source of truth)
-- Source: research/05-postgres-schema.md section 3, transcribed as-is (frozen DDL).
-- Batch L1 (garminator) -- do not modify the DDL manually outside migration.

CREATE TABLE raw.api_response (
    id              bigint       GENERATED ALWAYS AS IDENTITY,
    fetched_at      timestamptz  NOT NULL DEFAULT now(),
    endpoint        text         NOT NULL,
    scope_date      date         NULL,
    scope_key       text         NULL,
    http_status     smallint     NOT NULL,
    payload         jsonb        NULL,
    payload_sha256  bytea        NULL,
    ingest_run_id   bigint       NULL,
    CONSTRAINT api_response_pk PRIMARY KEY (fetched_at, id),
    CONSTRAINT api_response_sha_len CHECK (payload_sha256 IS NULL OR length(payload_sha256) = 32)
);

COMMENT ON TABLE  raw.api_response IS
  'Raw response from a Garmin API call (garminconnect 0.3.2 client). APPEND-ONLY: no row is ever modified or deleted. Each re-fetch of the same day creates a new row, making Garmin''s republication history (sleep recalculation, wellness/epoch/request) observable and auditable. Deliberately WITHOUT UNIQUE constraint: a uniqueness constraint including payload_sha256 but not fetched_at would prevent conversion to hypertable. Deduplication is deferred to raw.payload_seen.';
COMMENT ON COLUMN raw.api_response.endpoint IS
  'Name of the garminconnect method called, e.g. get_sleep_data, get_activity, get_stress_data. Serves as parsing discriminant in core.';
COMMENT ON COLUMN raw.api_response.scope_date IS
  'Athlete''s LOCAL date that the response relates to (cdate parameter of the 36 daily methods). NULL for non-daily endpoints. This is not a UTC date: Garmin wellness endpoints are natively keyed by local calendarDate.';
COMMENT ON COLUMN raw.api_response.scope_key IS
  'Secondary discriminant for non-daily endpoints, e.g. activityId for get_activity. NULL otherwise.';
COMMENT ON COLUMN raw.api_response.payload_sha256 IS
  'sha256 of the jsonb payload serialized canonically (sorted keys, fixed separators). Basis for retroactive change detection (see section 6).';

CREATE INDEX api_response_endpoint_scope_idx
    ON raw.api_response (endpoint, scope_date DESC NULLS LAST, fetched_at DESC);

CREATE INDEX api_response_scope_key_idx
    ON raw.api_response (endpoint, scope_key, fetched_at DESC)
    WHERE scope_key IS NOT NULL;


CREATE TABLE raw.payload_seen (
    endpoint        text        NOT NULL,
    scope_date      date        NOT NULL DEFAULT '-infinity',
    scope_key       text        NOT NULL DEFAULT '',
    payload_sha256  bytea       NOT NULL,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    seen_count      integer     NOT NULL DEFAULT 1,
    CONSTRAINT payload_seen_pk PRIMARY KEY (endpoint, scope_date, scope_key, payload_sha256)
);

COMMENT ON TABLE raw.payload_seen IS
  'Deduplication sentinel, deliberately separate from raw.api_response. Answers the question "has this exact content already been seen for this (endpoint, day)?" without imposing a UNIQUE constraint on raw.api_response -- a constraint that, not including the temporal column, would block hypertable conversion. Tiny table (one row per distinct content), entirely cached. An INSERT ... ON CONFLICT DO UPDATE incrementing seen_count serves both as an "already seen" check and as a republication counter.';


CREATE TABLE raw.fit_file (
    fit_file_id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    garmin_activity_id  bigint      NOT NULL,
    storage_path        text        NOT NULL,
    sha256              bytea       NOT NULL,
    size_bytes          bigint      NOT NULL,
    downloaded_at       timestamptz NOT NULL DEFAULT now(),
    parsed_at           timestamptz NULL,
    parser_version      text        NULL,
    parse_error         text        NULL,
    CONSTRAINT fit_file_sha_uq   UNIQUE (sha256),
    CONSTRAINT fit_file_sha_len  CHECK (length(sha256) = 32),
    CONSTRAINT fit_file_size_pos CHECK (size_bytes > 0)
);

COMMENT ON TABLE raw.fit_file IS
  'Metadata for .FIT files archived on the PVC. The binary is NOT in the database (no bytea): ~1,090 files of several MB would bloat TOAST and every PostgreSQL dump without contributing anything queryable. MANDATORY ATOMICITY RULE: the file is written AND fsync''d on the PVC BEFORE the INSERT of this row, never the reverse. An orphaned file (no row) is harmless and recoverable by scanning; an orphaned row (no file) makes core reconstructibility false. Backup corollary: the PostgreSQL dump and the PVC snapshot must be taken at the SAME point in time (see section 6.5).';
COMMENT ON COLUMN raw.fit_file.sha256 IS
  'sha256 of the .FIT binary. UNIQUE: the same content is archived only once. It is the pivot for detecting actual re-download (section 6.2). Distinct from the JSON summary hash (section 6.3).';
COMMENT ON COLUMN raw.fit_file.parser_version IS
  'Version of the parser (garmin-fit-sdk 21.213.0 + garminator code version) that produced the core rows. Allows selectively replaying files parsed by a buggy version.';

CREATE INDEX fit_file_activity_idx ON raw.fit_file (garmin_activity_id, downloaded_at DESC);
CREATE INDEX fit_file_unparsed_idx ON raw.fit_file (downloaded_at) WHERE parsed_at IS NULL;


CREATE TABLE raw.ingest_run (
    ingest_run_id  bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz NULL,
    mode           text        NOT NULL,
    status         text        NOT NULL DEFAULT 'running',
    code_version   text        NULL,
    notes          jsonb       NULL,
    CONSTRAINT ingest_run_mode_ck   CHECK (mode   IN ('backfill','daily','reprocess','manual')),
    CONSTRAINT ingest_run_status_ck CHECK (status IN ('running','ok','partial','failed'))
);

COMMENT ON TABLE raw.ingest_run IS
  'Ingestion execution log. Allows answering "which execution produced this row?" and replaying/canceling a batch. The reprocess mode corresponds to FORCE_REPROCESS_ACTIVITIES of the reference project (VERIFIED 01 section 3).';
