-- Migration 004: core: device, activity, activity_lap, swim_length
-- Source: research/05-postgres-schema.md section 3, transcribed as-is (frozen DDL).
-- Batch L1 (t0) -- do not modify the DDL manually outside migration.

CREATE TABLE core.device (
    device_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    manufacturer    text   NULL,
    product         text   NULL,
    serial_number   text   NULL,
    device_type     text   NULL,
    software_version text  NULL,
    CONSTRAINT device_identity_uq UNIQUE (manufacturer, product, serial_number, device_type)
);

COMMENT ON TABLE core.device IS
  'Watch, power meter, heart rate belt, cadence sensor -- fed by FIT device_info messages (mesg num 23, VERIFIED 02 section 2). Concrete utility, not theoretical: the bike power meter being present on only 1 outdoor ride in 10 (VERIFIED, see section 0.1), this is where we can prove "power is missing because the sensor was absent" rather than "the field was empty". Also used to invalidate rFTP when changing power source (core.threshold.power_source).';


CREATE TABLE core.activity (
    activity_id          bigint      PRIMARY KEY,
    athlete_id           smallint    NOT NULL REFERENCES core.athlete(athlete_id),
    sport_code           text        NOT NULL REFERENCES core.sport(sport_code),
    garmin_type_key      text        NULL,

    start_time           timestamptz NOT NULL,
    start_time_local     timestamp   NOT NULL,
    local_date           date        NOT NULL,
    tz_offset_s          integer     NOT NULL,

    duration_s           integer     NOT NULL,
    elapsed_s            integer     NULL,
    moving_s             integer     NULL,
    distance_m           real        NULL,
    ascent_m             real        NULL,
    descent_m            real        NULL,

    avg_hr               smallint    NULL,
    max_hr               smallint    NULL,
    avg_power            real        NULL,
    max_power            real        NULL,
    avg_power_nonzero    real        NULL,
    np_watts             real        NULL,
    np_reliable          boolean     NULL,
    avg_speed_mps        real        NULL,
    avg_cadence          real        NULL,
    calories             integer     NULL,

    ngp_speed_mps        real        NULL,
    gap_applied          boolean     NOT NULL DEFAULT false,
    swim_time_s          integer     NULL,
    swim_time_source     text        NULL,

    total_samples        integer     NULL,
    imputed_seconds      integer     NOT NULL DEFAULT 0,
    excised_seconds      integer     NOT NULL DEFAULT 0,
    data_quality         text        NOT NULL DEFAULT 'unknown',

    parent_activity_id   bigint      NULL REFERENCES core.activity(activity_id),
    is_parent            boolean     NOT NULL DEFAULT false,
    leg_order            smallint    NULL,

    primary_device_id    bigint      NULL REFERENCES core.device(device_id),
    fit_file_id          bigint      NULL REFERENCES raw.fit_file(fit_file_id),
    summary_sha256       bytea       NULL,
    name                 text        NULL,
    needs_manual_rpe     boolean     NOT NULL DEFAULT false,
    ingested_at          timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT activity_duration_pos   CHECK (duration_s > 0),
    CONSTRAINT activity_quality_ck     CHECK (data_quality IN ('good','fair','poor','unknown')),
    CONSTRAINT activity_swim_src_ck    CHECK (swim_time_source IS NULL OR swim_time_source IN ('fit_lengths','timer','elapsed')),
    CONSTRAINT activity_tz_range       CHECK (tz_offset_s BETWEEN -50400 AND 50400),
    CONSTRAINT activity_hierarchy_ck   CHECK (NOT (is_parent AND parent_activity_id IS NOT NULL)),
    CONSTRAINT activity_leg_ck         CHECK ((parent_activity_id IS NULL) = (leg_order IS NULL)),
    CONSTRAINT activity_np_ck          CHECK (np_watts IS NULL OR np_reliable IS NOT NULL),
    CONSTRAINT activity_summary_sha_len CHECK (summary_sha256 IS NULL OR length(summary_sha256) = 32)
);

COMMENT ON TABLE core.activity IS
  'One workout session. The primary key is Garmin''s activityId itself, not a surrogate key: it is stable and the only key guaranteeing idempotence against retroactive changes. REMINDER OF THE REFERENCE BUG (VERIFIED 01, issue #187): garmin-grafana duplicates an activity when its type or device changes afterwards, because these attributes are part of the InfluxDB tagset hence the row''s identity. Here, type and device are COLUMNS, never key components: a type change is an UPDATE, not a new row. The bug is structurally impossible.';
COMMENT ON COLUMN core.activity.start_time IS
  'Absolute departure instant, timestamptz (thus stored in UTC). Used for temporal joins with wellness and for sequencing.';
COMMENT ON COLUMN core.activity.start_time_local IS
  'Athlete''s wall time at departure, MATERIALIZED at ingestion (timestamp without timezone, never to be reconverted). It answers "did I run in the morning or the evening?". No Grafana view should contain AT TIME ZONE (see section 7).';
COMMENT ON COLUMN core.activity.local_date IS
  'Local attachment date, materialized. Daily aggregation key for the ENTIRE project: PMC, daily load, join with wellness (whose Garmin endpoints are natively keyed by local calendarDate). Aggregation by UTC date would shift summer evening sessions by one day.';
COMMENT ON COLUMN core.activity.tz_offset_s IS
  'UTC offset in seconds at departure time, kept to be able to prove afterwards how local_date was calculated (and detect travel / time change).';
COMMENT ON COLUMN core.activity.duration_s IS
  'Elapsed time (timer time), in WHOLE SECONDS. Deliberate typing choice: never the time type (antipattern noted VERIFIED 02 section 3 in GarminDB, where time overflows at 24 h and is not summable). It is this column -- not elapsed_s -- that gives the real volume: 1,076,042 s over 300 activities (VERIFIED, Activities.csv), i.e., 298.9 h, versus 1,159,651 s of elapsed time.';
COMMENT ON COLUMN core.activity.np_watts IS
  'Normalized power recalculated by t0 at FIT parsing (30s moving average, then mean of fourth-power, VERIFIED 04 section 1.1). NEVER read from FIT field session.normalized_power: that is populated only if Garmin calculated it upstream and its smoothing convention is undocumented.';
COMMENT ON COLUMN core.activity.np_reliable IS
  'false when the usable series spans 30 to 300 s (NP mathematically defined but barely significant); NULL/absent when N < 30 s, in which case np_watts is NULL. VERIFIED 04 section 1.5.';
COMMENT ON COLUMN core.activity.avg_power_nonzero IS
  'Average power excluding zeros. Stored IN ADDITION to avg_power because the difference between the two is the direct diagnosis of coasting / descent, and because Garmin''s convention on this is undocumented (VERIFIED 04 section 1.7).';
COMMENT ON COLUMN core.activity.imputed_seconds IS
  'Number of power seconds reconstructed (interpolation <= 3 s, or filling with 0 W on gap without pause). VERIFIED 04 section 1.3: beyond 10% of duration, data_quality switches to poor. Without this column, an NP calculated on a gapped series is indistinguishable from clean NP.';
COMMENT ON COLUMN core.activity.swim_time_s IS
  'Effective swim time, reconstructed by sum of active-type FIT length messages. VERIFIED 04 section 4.3: "Using elapsed time gives sTSS overestimated by 30-50%" on a repetition session. In open water, where lengths don''t exist, swim_time_source is elapsed and data_quality capped at fair.';
COMMENT ON COLUMN core.activity.parent_activity_id IS
  'Filled in for segments (legs) of a multisport sequence; NULL on a simple activity or on the parent. VERIFIED (Activities.csv, 2 Multisport activities): the parent row has HR and power as "--" and an "Average pace" of 20.3 which is actually km/h speed. The parent row is thus UNUSABLE as-is: its totals must be recalculated by summing children. Any load view MUST filter is_parent = false to avoid double-counting.';
COMMENT ON COLUMN core.activity.summary_sha256 IS
  'sha256 of the JSON summary get_activity(activity_id), canonicalized and stripped of volatile fields. SECOND hashing mechanism, independent of raw.fit_file.sha256: the actually documented case of bug #187 is a type or title change WITHOUT a new FIT file. The FIT hash doesn''t move then; only this one detects the change (see section 6.3).';
COMMENT ON COLUMN core.activity.needs_manual_rpe IS
  'true when neither usable power nor HR allows load estimation: TSS remains NULL (never 0) and the activity is queued for manual entry. VERIFIED 04 section 0.3.';

CREATE INDEX activity_local_date_idx  ON core.activity (local_date DESC);
CREATE INDEX activity_sport_date_idx  ON core.activity (sport_code, local_date DESC);
CREATE INDEX activity_start_idx       ON core.activity (start_time DESC);
CREATE INDEX activity_parent_idx      ON core.activity (parent_activity_id, leg_order) WHERE parent_activity_id IS NOT NULL;
CREATE INDEX activity_needs_rpe_idx   ON core.activity (local_date DESC) WHERE needs_manual_rpe;


CREATE TABLE core.activity_lap (
    activity_id      bigint      NOT NULL REFERENCES core.activity(activity_id) ON DELETE CASCADE,
    lap_index        smallint    NOT NULL,
    start_time       timestamptz NOT NULL,
    duration_s       integer     NOT NULL,
    elapsed_s        integer     NULL,
    distance_m       real        NULL,
    avg_speed_mps    real        NULL,
    avg_hr           smallint    NULL,
    max_hr           smallint    NULL,
    avg_power        real        NULL,
    max_power        real        NULL,
    np_watts         real        NULL,
    np_convention    text        NULL,
    avg_cadence      real        NULL,
    ascent_m         real        NULL,
    descent_m        real        NULL,
    intensity        text        NULL,
    lap_trigger      text        NULL,
    swim_stroke      text        NULL,
    num_lengths      smallint    NULL,
    extra            jsonb       NULL,
    CONSTRAINT activity_lap_pk PRIMARY KEY (activity_id, lap_index),
    CONSTRAINT activity_lap_np_conv_ck CHECK (np_convention IS NULL OR np_convention IN ('global_window','lap_local'))
);

COMMENT ON TABLE core.activity_lap IS
  'Laps (FIT lap message, mesg num 19, 124 fields in official profile -- VERIFIED 02 section 2). Intermediate level preserved as-is rather than recalculated: lap boundaries are a training intention (start of interval) that no reconstruction from samples can guess. The ~124 profile fields are not all columned: common ones are typed, the rest go into extra.';
COMMENT ON COLUMN core.activity_lap.np_convention IS
  'Convention for calculating lap NP. global_window (default): the 30s moving series is calculated ONCE over the entire activity then restricted to lap indices -- VERIFIED 04 section 1.2, convention (C) retained. CONSEQUENCE TO ASSUME: a lap''s NP is then NOT recalculable from lap data alone; this column exists so that this non-recalculability is documented in the database and not discovered by a perplexed user.';


CREATE TABLE core.swim_length (
    activity_id    bigint      NOT NULL REFERENCES core.activity(activity_id) ON DELETE CASCADE,
    length_index   smallint    NOT NULL,
    lap_index      smallint    NULL,
    start_time     timestamptz NOT NULL,
    duration_s     real        NOT NULL,
    length_type    text        NOT NULL,
    swim_stroke    text        NULL,
    total_strokes  smallint    NULL,
    avg_cadence    real        NULL,
    CONSTRAINT swim_length_pk PRIMARY KEY (activity_id, length_index),
    CONSTRAINT swim_length_type_ck CHECK (length_type IN ('active','idle')),
    CONSTRAINT swim_length_dur_pos CHECK (duration_s > 0)
);

COMMENT ON TABLE core.swim_length IS
  'Pool lengths (FIT length message, mesg num 101, 22 fields -- VERIFIED 02 section 2). ESSENTIAL table, not redundant with laps: in a pool, the watch does NOT emit a record message per second, so core.sample is empty and length is the only fine granularity existing. It is the most profitable table in the schema: 80 pool swimming activities (VERIFIED, Activities.csv) for a negligible volume (~40 to 80 rows per session).';
COMMENT ON COLUMN core.swim_length.length_type IS
  'active = swum length, idle = rest at wall. This distinction is what makes sTSS correct: summing the duration of active lengths only gives core.activity.swim_time_s. VERIFIED 04 section 4.3.';
COMMENT ON COLUMN core.swim_length.duration_s IS
  'real not integer: FIT length times are to the hundredth of a second and rounding to the second over 40 lengths would introduce visible drift in average pace.';

CREATE INDEX swim_length_active_idx ON core.swim_length (activity_id, lap_index) WHERE length_type = 'active';
