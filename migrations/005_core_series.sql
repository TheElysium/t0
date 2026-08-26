-- Migration 005: core: sample, sample_derived, activity_hr_histogram, activity_power_curve, activity_metric
-- Source: research/05-postgres-schema.md section 3, transcribed as-is (frozen DDL).
-- Batch L1 (t0) -- do not modify the DDL manually outside migration.

CREATE TABLE core.sample (
    activity_id           bigint      NOT NULL REFERENCES core.activity(activity_id) ON DELETE CASCADE,
    t                     timestamptz NOT NULL,
    elapsed_s             integer     NOT NULL,
    latitude_deg          double precision NULL,
    longitude_deg         double precision NULL,
    altitude_m            real        NULL,
    distance_m            real        NULL,
    speed_mps             real        NULL,
    heart_rate            smallint    NULL,
    cadence               smallint    NULL,
    power                 smallint    NULL,
    temperature           smallint    NULL,
    vertical_oscillation  real        NULL,
    stance_time           real        NULL,
    step_length           real        NULL,
    vertical_ratio        real        NULL,
    extra                 jsonb       NULL,
    CONSTRAINT sample_pk PRIMARY KEY (activity_id, t),
    CONSTRAINT sample_hr_range   CHECK (heart_rate IS NULL OR heart_rate BETWEEN 20 AND 260),
    CONSTRAINT sample_lat_range  CHECK (latitude_deg  IS NULL OR latitude_deg  BETWEEN -90  AND 90),
    CONSTRAINT sample_lon_range  CHECK (longitude_deg IS NULL OR longitude_deg BETWEEN -180 AND 180),
    CONSTRAINT sample_power_nonneg CHECK (power IS NULL OR power >= 0)
);

COMMENT ON TABLE core.sample IS
  'Per-second series, ALL SPORTS COMBINED, faithful image of FIT record message (mesg num 20, 84 fields -- VERIFIED 02 section 2). Single table, not one per discipline: NULL columns cost only a bit in the tuple header nulls bitmap, whereas three tables would require a UNION in every multisport query and triple index maintenance. NO derived values here: this table is reconstructible identically from .FIT and nothing else. SIZING (calculation, see section 5): ~3.2 to 4 M rows in 5 years, ~0.5 to 0.6 GB including indexes. EXPLICIT CONCLUSION: NO PARTITIONING, neither now nor "just in case". The PK includes t, so TimescaleDB hypertable conversion remains possible without migration if volume someday contradicted this estimate.';
COMMENT ON COLUMN core.sample.t IS
  'Absolute sample timestamp (timestamptz). Component of the PK AND potential partitioning column. The order (activity_id, t) is deliberate: Grafana''s dominant query is "all the series of ONE activity", served by a contiguous index scan.';
COMMENT ON COLUMN core.sample.elapsed_s IS
  'Seconds elapsed since start. Only tolerated redundancy in this table (derivable from t minus activity.start_time): it avoids a join and interval calculation in every Grafana graph of a session, for 4 bytes per row.';
COMMENT ON COLUMN core.sample.latitude_deg IS
  'Decimal degrees, double precision -- not the FIT integer semicircles. The conversion is done once at ingestion; Grafana geomap and PostGIS expect degrees. Zero cost on indoor and pool activities: a NULL column occupies zero data bytes.';
COMMENT ON COLUMN core.sample.power IS
  'Instantaneous power in watts, raw. Populated for both bike (sensor) and running (Garmin Running Power, present on 155/155 runs -- VERIFIED 04 section 0.1). The two scales are NOT comparable (04 section 3.2.1): it is core.activity.sport_code that distinguishes them, and core.threshold that forbids sharing a threshold.';
COMMENT ON COLUMN core.sample.extra IS
  'Remaining FIT fields (including developer fields, messages 206/207) as jsonb, to lose nothing without columning 84 fields. Deliberately NOT indexed as GIN: no query identified filters on it, and a GIN index over millions of rows would cost more than the table. To add the day a specific need appears, not before.';

CREATE INDEX sample_t_brin ON core.sample USING brin (t) WITH (pages_per_range = 32);
COMMENT ON INDEX core.sample_t_brin IS
  'BRIN not B-tree: rows are inserted per activity, thus physically nearly sorted by t, which is exactly the BRIN use case. Cost: tens of kilobytes, versus ~110 MB for a B-tree on the same column. Serves inter-activity temporal range queries ("all data from March").';

ALTER TABLE core.sample SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.01);


CREATE TABLE core.sample_derived (
    activity_id        bigint      NOT NULL,
    t                  timestamptz NOT NULL,
    power_30s          real        NULL,
    grade_pct          real        NULL,
    grade_factor       real        NULL,
    gap_speed_mps      real        NULL,
    imputed            boolean     NOT NULL DEFAULT false,
    excised            boolean     NOT NULL DEFAULT false,
    computed_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sample_derived_pk PRIMARY KEY (activity_id, t),
    CONSTRAINT sample_derived_grade_clamp CHECK (grade_pct IS NULL OR grade_pct BETWEEN -30 AND 30)
);

COMMENT ON TABLE core.sample_derived IS
  'Per-second CALCULATED series (30s power smoothing, smoothed gradient, gradient correction factor, flat-equivalent speed). Separated from core.sample for a precise reason: core.sample is an immutable fact, this is the product of an algorithm version. Correcting the gradient smoothing window is handled by TRUNCATE + recalculation, without ever touching raw data or re-downloading a FIT. Written in the SAME transaction as parsing. NO foreign key to core.activity: this table is convertible to hypertable and Timescale forbids a hypertable being a foreign key target; integrity is ensured by bulk reconstruction.';
COMMENT ON COLUMN core.sample_derived.grade_pct IS
  'Gradient smoothed over a window of +/- 10 m of DISTANCE (not time, which would give a variable window by pace), clipped to +/- 30%. VERIFIED 04 section 3.3.2: without smoothing, barometric altimeter noise produces aberrant gradients second by second.';
COMMENT ON COLUMN core.sample_derived.grade_factor IS
  'Multiplicative factor f(i) applied to speed to get flat-equivalent speed. Forced to 1 on treadmill (core.sport.is_indoor), VERIFIED 04 section 3.3.4.';
COMMENT ON COLUMN core.sample_derived.imputed IS
  'true if the power value for this second was reconstructed (interpolation or 0 W filling). VERIFIED 04 section 1.3. The sum of these flags feeds core.activity.imputed_seconds.';

CREATE INDEX sample_derived_t_brin ON core.sample_derived USING brin (t) WITH (pages_per_range = 32);
ALTER TABLE core.sample_derived SET (autovacuum_vacuum_scale_factor = 0.02);


CREATE TABLE core.activity_hr_histogram (
    activity_id  bigint   NOT NULL REFERENCES core.activity(activity_id) ON DELETE CASCADE,
    hr_bpm       smallint NOT NULL,
    seconds      integer  NOT NULL,
    CONSTRAINT activity_hr_histogram_pk PRIMARY KEY (activity_id, hr_bpm),
    CONSTRAINT hr_hist_bpm_range CHECK (hr_bpm BETWEEN 20 AND 260),
    CONSTRAINT hr_hist_sec_pos   CHECK (seconds > 0)
);

COMMENT ON TABLE core.activity_hr_histogram IS
  'Heart rate frequency histogram at 1 BEAT-PER-MINUTE resolution (time spent at each whole bpm). Deliberately replaces storing seconds_in_zone_1..7. VERIFIED (04 section 5.4): "Storing time by zone freezes zone boundaries in the data. [...] Storing the histogram at 1 bpm makes time by zone recalculable for any set of bounds." DECISIVE CONSEQUENCE: correcting a wrong LTHR becomes an UPDATE in core.threshold followed by a REFRESH of mart views -- not re-ingesting 300 activities. COST: about 200 rows per activity, i.e., ~60,000 rows for existing history and ~220,000 in 5 years -- negligible versus core.sample. Does not dispense with keeping core.sample: the histogram loses chronological order, so aerobic decoupling and sample-by-sample TRIMP remain dependent on it.';
COMMENT ON COLUMN core.activity_hr_histogram.seconds IS
  'Number of seconds spent exactly at this whole HR value. The sum over an activity equals valid HR time, which may be less than duration_s if the belt drops off.';


CREATE TABLE core.activity_power_curve (
    activity_id   bigint   NOT NULL REFERENCES core.activity(activity_id) ON DELETE CASCADE,
    kind          text     NOT NULL,
    duration_s    integer  NOT NULL,
    value         real     NOT NULL,
    spans_pause   boolean  NOT NULL DEFAULT false,
    CONSTRAINT activity_power_curve_pk PRIMARY KEY (activity_id, kind, duration_s),
    CONSTRAINT apc_kind_ck CHECK (kind IN ('bike_power','run_power','run_speed','swim_speed','heart_rate')),
    CONSTRAINT apc_duration_pos CHECK (duration_s >= 1)
);

COMMENT ON TABLE core.activity_power_curve IS
  'Curve of an activity''s best moving averages: for each duration d, the best average value sustained for d seconds. Pre-calculated at ingestion on a grid of approximately 60 to 100 LOG-SPACED DURATIONS (d_j = round(10^(j/20)) deduplicated, VERIFIED 04 section 8.2) not all integer durations: the curve is smooth in log scale, fine resolution beyond a minute adds nothing and would multiply volume by a hundred. SQL calculation on the fly over millions of rows is beyond the reach of an interactive dashboard -- hence pre-calculation.';
COMMENT ON COLUMN core.activity_power_curve.kind IS
  'Nature of the curve, explicit and unambiguous for units. bike_power and run_power are in watts and MUST NEVER BE MERGED (VERIFIED 04 section 8.3, incomparable scales). run_speed and swim_speed are in m/s: speed not pace, so the best effort is always a MAX (pace in s/km would require a MIN and break any generic aggregation). Display pace is derived by 1000/value. Without run_speed, the record curve would ignore 155 of 300 running activities for which pace is the historical reference.';
COMMENT ON COLUMN core.activity_power_curve.spans_pause IS
  'true if the optimal window spans an excised pause: the average is then physiologically false (two efforts stuck together). VERIFIED 04 section 8.3: these points must be excluded from the period record curve, while remaining visible on the activity.';

CREATE INDEX activity_power_curve_best_idx
    ON core.activity_power_curve (kind, duration_s, value DESC)
    WHERE NOT spans_pause;
COMMENT ON INDEX core.activity_power_curve_best_idx IS
  'Index serving the period record curve: "best value over d seconds among activities in a range". The DESC order on value allows the planner to stop at the first row per group. The partial filter immediately excludes windows spanning a pause.';


CREATE TABLE core.activity_metric (
    activity_id   bigint   NOT NULL REFERENCES core.activity(activity_id) ON DELETE CASCADE,
    metric        text     NOT NULL,
    method_code   text     NOT NULL REFERENCES core.load_method(method_code),
    value         double precision NULL,
    is_canonical  boolean  NOT NULL DEFAULT false,
    threshold_id  bigint   NULL REFERENCES core.threshold(threshold_id),
    computed_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT activity_metric_pk PRIMARY KEY (activity_id, metric, method_code)
);

CREATE UNIQUE INDEX activity_metric_canonical_uq
    ON core.activity_metric (activity_id, metric)
    WHERE is_canonical;

COMMENT ON TABLE core.activity_metric IS
  'Metrics calculated per activity, in LONG FORMAT (one row per calculation method) not wide columns. Justification: the same activity can legitimately carry multiple TSS values obtained by different methods (power AND heart rate), and we want to compare them, not overwrite one. A wide format would impose tss_power, tss_hr, tss_gap, tss_rpe... and one more column per new method. Wide display remains available via the mart.activity_wide view: narrow table for writing, wide view for reading.';
COMMENT ON COLUMN core.activity_metric.is_canonical IS
  'Designates THE value that is authoritative for this activity and metric, resolved via core.metric_method_priority. The unique partial index guarantees there are never two: ambiguity is forbidden by schema, not code convention. Every load view reads only is_canonical.';
COMMENT ON COLUMN core.activity_metric.threshold_id IS
  'Threshold actually used for this calculation. Essential traceability: without it, you cannot answer "was this 2025 TSS calculated with the old FTP?" after a threshold update.';
COMMENT ON COLUMN core.activity_metric.value IS
  'NULL is a legitimate and MEANINGFUL value: it means "not calculable" and is distinct from 0. VERIFIED 04 section 0.3: when power is present but HR is absent (observed on 4 of 19 home-trainer sessions), trimp, hrtss, decoupling and efficiency_factor must be NULL never 0, lest artificially crash all averages.';

CREATE INDEX activity_metric_metric_idx ON core.activity_metric (metric, method_code);
