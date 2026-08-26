-- Migration 006: core: wellness_metric (idempotent seed), wellness_sample, daily_wellness, sleep_session, sleep_stage
-- Source: research/05-postgres-schema.md section 3, transcribed as-is (frozen DDL).
-- Batch L1 (t0) -- do not modify the DDL manually outside migration.

CREATE TABLE core.wellness_metric (
    metric_id     smallint PRIMARY KEY,
    metric_code   text     NOT NULL UNIQUE,
    label      text     NOT NULL,
    unit          text     NOT NULL,
    api_method    text     NULL,
    is_intraday   boolean  NOT NULL DEFAULT true
);

COMMENT ON TABLE core.wellness_metric IS
  'Reference table for intraday wellness metrics. The metric_id is a smallint DELIBERATELY SHORT because it repeats over millions of core.wellness_sample rows: text would cost tens of megabytes for nothing. api_method retains the garminconnect 0.3.2 method name, allowing replay of collection for a single metric.';

INSERT INTO core.wellness_metric (metric_id, metric_code, label, unit, api_method) VALUES
  (1,'heart_rate',     'Heart rate',               'bpm',   'get_heart_rates'),
  (2,'stress',         'Stress',                   'score', 'get_stress_data'),
  (3,'body_battery',   'Body Battery',             'score', 'get_body_battery'),
  (4,'steps',          'Steps',                    'count', 'get_steps_data'),
  (5,'respiration',    'Respiration rate',         'rpm',   'get_respiration_data'),
  (6,'spo2',           'SpO2',                     'pct',   'get_spo2_data'),
  (7,'hrv',            'Heart rate variability',   'ms',    'get_hrv_data'),
  (8,'intensity_min',  'Intensity minutes',        'min',   'get_intensity_minutes_data'),
  (9,'floors',         'Floors',                   'count', 'get_floors')
ON CONFLICT DO NOTHING;


CREATE TABLE core.wellness_sample (
    metric_id   smallint    NOT NULL,
    t           timestamptz NOT NULL,
    athlete_id  smallint    NOT NULL,
    value       real        NOT NULL,
    local_date  date        NOT NULL,
    CONSTRAINT wellness_sample_pk PRIMARY KEY (metric_id, t)
);

COMMENT ON TABLE core.wellness_sample IS
  'Intraday wellness series, long format. PRIMARY KEY ORDER: (metric_id, t) not (t, metric_id). Reason: every Grafana query filters first on a single metric then on a time range, served by perfectly contiguous index scan; the reverse order would impose scanning the entire time range with metric_id filter. This point was opposed by the reviewer and he is right (see section 10, concession 6). NO foreign key: table convertible to hypertable, and Timescale forbids a hypertable being a foreign key target -- referential integrity to a 9-row reference table is an assumed zero risk. VOLUME: ~1.1 M points per year, ~5.5 M in 5 years (ASSUMED, see section 5.4: actual cadence per metric is not verified, only magnitude).';
COMMENT ON COLUMN core.wellness_sample.local_date IS
  'Local attachment date, materialized at ingestion. Garmin wellness endpoints are natively keyed by local calendarDate: recalculating this date in a view via AT TIME ZONE would replay exactly the timezone bugs documented in the reference project (VERIFIED 01, issues #85, #40, #137, #185, #25).';
COMMENT ON COLUMN core.wellness_sample.value IS
  'real (4 bytes) not double precision: none of these metrics has more than 3 significant figures, and half are integers. Direct savings of 4 bytes over millions of rows.';

CREATE INDEX wellness_sample_local_date_idx ON core.wellness_sample (local_date, metric_id);
CREATE INDEX wellness_sample_t_brin ON core.wellness_sample USING brin (t) WITH (pages_per_range = 32);
ALTER TABLE core.wellness_sample SET (autovacuum_vacuum_scale_factor = 0.05);


CREATE TABLE core.daily_wellness (
    athlete_id            smallint NOT NULL REFERENCES core.athlete(athlete_id),
    local_date            date     NOT NULL,
    resting_hr            smallint NULL,
    hrv_last_night_avg    real     NULL,
    hrv_status            text     NULL,
    body_battery_high     smallint NULL,
    body_battery_low      smallint NULL,
    avg_stress            smallint NULL,
    steps                 integer  NULL,
    floors                smallint NULL,
    intensity_minutes     smallint NULL,
    weight_kg             real     NULL,
    training_readiness    smallint NULL,
    vo2max_running        real     NULL,
    vo2max_cycling        real     NULL,
    source_sha256         bytea    NULL,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT daily_wellness_pk PRIMARY KEY (athlete_id, local_date)
);

COMMENT ON TABLE core.daily_wellness IS
  'Daily aggregates PROVIDED BY GARMIN (not recalculated from core.wellness_sample): resting HR, HRV status, Body Battery, estimated VO2max. WIDE FORMAT assumed here, unlike core.wellness_sample: these fields are few, stable, all present the same day, and systematically read together on a daily dashboard. One row per day, i.e., ~1,800 rows in 5 years: normalization would add nothing and cost a join. KNOWN RISK (VERIFIED 03): on Forerunner 255, training_readiness, endurance_score and hill_score can remain empty -- nullable columns, never NOT NULL.';
COMMENT ON COLUMN core.daily_wellness.source_sha256 IS
  'Hash of the daily payload that produced this row. Allows detecting when a day was republished by Garmin with different content (sleep recalculation, wellness/epoch/request) without field-by-field comparison.';


CREATE TABLE core.sleep_session (
    athlete_id        smallint    NOT NULL REFERENCES core.athlete(athlete_id),
    wake_date         date        NOT NULL,
    sleep_start       timestamptz NOT NULL,
    sleep_end         timestamptz NOT NULL,
    sleep_start_local timestamp   NOT NULL,
    duration_s        integer     NOT NULL,
    deep_s            integer     NULL,
    light_s           integer     NULL,
    rem_s             integer     NULL,
    awake_s           integer     NULL,
    sleep_score       smallint    NULL,
    avg_spo2          real        NULL,
    avg_respiration   real        NULL,
    source_sha256     bytea       NULL,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sleep_session_pk PRIMARY KEY (athlete_id, wake_date),
    CONSTRAINT sleep_order_ck CHECK (sleep_end > sleep_start),
    CONSTRAINT sleep_score_ck CHECK (sleep_score IS NULL OR sleep_score BETWEEN 0 AND 100)
);

COMMENT ON TABLE core.sleep_session IS
  'Night of sleep, KEYED BY WAKE DATE not sleep date. Timezone arbitration decided once and for all (see section 7): a night started on the 3rd at 23:40 and ended on the 4th at 7 am attaches to the 4th, which is the Garmin convention and the only one allowing correlation of "night sleep" and "next session" without a day shift. The PK by (athlete_id, wake_date) makes upsert of a republished night trivial and attachment order unambiguous.';

CREATE TABLE core.sleep_stage (
    athlete_id  smallint    NOT NULL,
    wake_date   date        NOT NULL,
    stage_start timestamptz NOT NULL,
    stage_end   timestamptz NOT NULL,
    stage       text        NOT NULL,
    CONSTRAINT sleep_stage_pk PRIMARY KEY (athlete_id, wake_date, stage_start),
    CONSTRAINT sleep_stage_fk FOREIGN KEY (athlete_id, wake_date)
        REFERENCES core.sleep_session(athlete_id, wake_date) ON DELETE CASCADE,
    CONSTRAINT sleep_stage_ck CHECK (stage IN ('deep','light','rem','awake','unmeasurable'))
);

COMMENT ON TABLE core.sleep_stage IS
  'Sleep phases as INTERVALS (start, end) not minute samples: sleep is natively described by Garmin as a sequence of segments, and storing them as-is divides volume by ~30 while remaining exact. ON DELETE CASCADE allows replacing a republished night with a simple DELETE of the session followed by a re-insert.';
