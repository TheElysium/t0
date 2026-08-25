-- Migration 003: core reference tables (sport, load_method, threshold, hr_tss_coefficient, metric_method_priority, pmc_seed) -- seeds made idempotent (ON CONFLICT DO NOTHING, explicit addition outside frozen DDL)
-- Source: research/05-postgres-schema.md section 3, transcribed as-is (frozen DDL).
-- Batch L1 (garminator) -- do not modify the DDL manually outside migration.

CREATE TABLE core.athlete (
    athlete_id     smallint    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    garmin_user_id text        NULL,
    display_name   text        NOT NULL,
    sex            text        NOT NULL DEFAULT 'male',
    birth_date     date        NULL,
    home_timezone  text        NOT NULL DEFAULT 'Europe/Paris',
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT athlete_sex_ck CHECK (sex IN ('male','female'))
);

COMMENT ON TABLE core.athlete IS
  'The athlete. Single-user in practice, but the foreign key is carried everywhere to leave the door open. sex is not cosmetic: it selects the Banister TRIMP coefficient (VERIFIED 04 section 5.1, k=0.64/1.92 male vs 0.86/1.67 female) -- 04 explicitly requires it to be configurable and not hardcoded.';
COMMENT ON COLUMN core.athlete.home_timezone IS
  'IANA identifier of the residence timezone. Serves ONLY as a fallback value when an activity''s or day''s timezone is indeterminable. The local attachment date is always materialized at ingestion, never recalculated from this field in a view (see section 7).';


CREATE TABLE core.sport (
    sport_code      text     PRIMARY KEY,
    fit_sport       smallint NULL,
    fit_sub_sport   smallint NULL,
    label        text     NOT NULL,
    discipline      text     NOT NULL,
    is_indoor       boolean  NOT NULL DEFAULT false,
    produces_records boolean NOT NULL DEFAULT true,
    CONSTRAINT sport_discipline_ck CHECK (discipline IN ('bike','run','swim','other'))
);

COMMENT ON TABLE core.sport IS
  'Correspondence table between the (sport, sub_sport) pair of the official FIT profile and a canonical garminator sport code. This is NOT a simple labels table -- it carries two decision-making pieces of information: discipline (which drives the load fallback matrix, 04 section 0.3) and is_indoor (which forces f(i)=1 in GAP calculation, VERIFIED 04 section 3.3.4: "Treadmill: gradient unreliable"). A simple free text column on core.activity would not allow these rules to be expressed declaratively.';
COMMENT ON COLUMN core.sport.produces_records IS
  'false for sports that do not emit a FIT record message per second (pool swimming). Used for sizing and non-regression tests on core.sample volume.';

INSERT INTO core.sport (sport_code, label, discipline, is_indoor, produces_records) VALUES
  ('running',        'Running',              'run',   false, true),
  ('trail_running',  'Trail',                'run',   false, true),
  ('treadmill',      'Treadmill',            'run',   true,  true),
  ('cycling',        'Cycling',              'bike',  false, true),
  ('virtual_cycling','Virtual cycling',      'bike',  true,  true),
  ('lap_swimming',   'Pool swimming',        'swim',  true,  false),
  ('open_water',     'Open water',           'swim',  false, true),
  ('hiking',         'Hiking',               'other', false, true),
  ('resort_skiing',  'Resort skiing',        'other', false, true),
  ('multisport',     'Multisport',           'other', false, false),
  ('transition',     'Transition',           'other', false, true),
  ('strength',       'Strength',             'other', true,  true)
ON CONFLICT DO NOTHING;


CREATE TABLE core.load_method (
    method_code   text    PRIMARY KEY,
    label      text    NOT NULL,
    scale         text    NOT NULL,
    is_measured   boolean NOT NULL,
    CONSTRAINT load_method_scale_ck CHECK (scale IN ('tss','trimp','rpe','none'))
);

COMMENT ON TABLE core.load_method IS
  'Load calculation methods. The scale column is STRUCTURAL and not decorative: it prevents adding quantities of different scales in CTL. VERIFIED (04 section 5.1): "1 hour at threshold gives ~170 TRIMP, not 100. TRIMP is not on the TSS scale and must NEVER be added to TSS in CTL." A view that sums daily load MUST filter on scale = ''tss''. is_measured distinguishes measured load from estimated load, for the Grafana coloring required in 04 section 5.3.';

INSERT INTO core.load_method (method_code, label, scale, is_measured) VALUES
  ('bike_power', 'TSS bike power (NP/FTP)',              'tss',   true),
  ('run_power',  'rTSS run power (NP/rFTP)',             'tss',   true),
  ('run_gap',    'rTSS gradient-adjusted pace (NGP/FTPa)','tss',  true),
  ('swim_css',   'sTSS swimming (speed/CSS)',            'tss',   true),
  ('hr_hrtss',   'hrTSS by heart rate histogram',        'tss',   false),
  ('hr_trimp',   'Banister TRIMP by sample',             'trimp', false),
  ('rpe',        'Manually entered load (RPE)',          'rpe',   false)
ON CONFLICT DO NOTHING;


CREATE TABLE core.threshold (
    threshold_id       bigint    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    athlete_id         smallint  NOT NULL REFERENCES core.athlete(athlete_id),
    discipline         text      NOT NULL,
    validity           daterange NOT NULL,
    ftp_watts             real   NULL,
    rftp_watts            real   NULL,
    ftp_pace_s_per_km     real   NULL,
    css_m_s               real   NULL,
    lthr_bpm              smallint NULL,
    hr_max_bpm            smallint NULL,
    hr_rest_bpm           smallint NULL,
    weight_kg             real   NULL,
    power_source          text   NULL,
    source                text   NOT NULL DEFAULT 'manual',
    css_estimated         boolean NOT NULL DEFAULT false,
    note                  text   NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT threshold_discipline_ck CHECK (discipline IN ('bike','run','swim','all')),
    CONSTRAINT threshold_source_ck     CHECK (source IN ('manual','cp_model','auto_20min','import')),
    CONSTRAINT threshold_no_overlap
        EXCLUDE USING gist (athlete_id WITH =, discipline WITH =, validity WITH &&),
    -- Structurally forbidden to mix the two power scales.
    CONSTRAINT threshold_bike_only CHECK (discipline <> 'bike' OR (rftp_watts IS NULL AND css_m_s IS NULL)),
    CONSTRAINT threshold_run_only  CHECK (discipline <> 'run'  OR (ftp_watts  IS NULL AND css_m_s IS NULL)),
    CONSTRAINT threshold_swim_only CHECK (discipline <> 'swim' OR (ftp_watts  IS NULL AND rftp_watts IS NULL)),
    CONSTRAINT threshold_hr_order  CHECK (hr_max_bpm IS NULL OR hr_rest_bpm IS NULL OR hr_max_bpm > hr_rest_bpm),
    CONSTRAINT threshold_css_range CHECK (css_m_s IS NULL OR (100.0/css_m_s) BETWEEN 45 AND 180)
);

COMMENT ON TABLE core.threshold IS
  'Physiological thresholds HISTORIZED BY VALIDITY PERIOD. Non-negotiable product constraint, reaffirmed by 04 section 2.3: "An activity''s TSS uses the threshold in effect on its date. Otherwise an FTP update would retroactively rewrite the entire CTL history." The GiST exclusion constraint guarantees that no date is covered by two rows of the same discipline: the join "the threshold on day J" can never return two rows. CHECK constraints per discipline physically prevent entering ftp_watts (bike, measured mechanical power) and rftp_watts (run, Garmin modeled power) on the same row -- VERIFIED 04 section 3.2.1: "Absolute rule: rFTP and bike FTP are not comparable and must never be mixed."';
COMMENT ON COLUMN core.threshold.validity IS
  'Date range [start, end) in half-open convention. NULL/infinity upper bound designates the current threshold. Use validity @> a.local_date to join an activity to its threshold.';
COMMENT ON COLUMN core.threshold.power_source IS
  'Origin of power measurement (garmin_native, stryd, powermeter_<model>). VERIFIED (04 section 3.2): Garmin Running Power is a proprietary undocumented model; "a 250 W Garmin and a 250 W Stryd are not the same measurement". A change in value here MUST close the validity period and invalidate the rFTP.';
COMMENT ON COLUMN core.threshold.css_estimated IS
  'true when CSS comes from the coarse fallback "1000 m TT x 0.97" due to lack of a double 200/400 m test (VERIFIED 04 section 4.1). Should be exposed in Grafana: an sTSS built on an estimated CSS does not have the same status.';


CREATE TABLE core.hr_tss_coefficient (
    zone_code     text     PRIMARY KEY,
    zone_order    smallint NOT NULL UNIQUE,
    lo_pct_lthr   real     NOT NULL,
    hi_pct_lthr   real     NOT NULL,
    tss_per_hour  real     NOT NULL,
    label      text     NOT NULL,
    CONSTRAINT hr_tss_bounds_ck CHECK (hi_pct_lthr > lo_pct_lthr),
    CONSTRAINT hr_tss_rate_ck   CHECK (tss_per_hour >= 0)
);

COMMENT ON TABLE core.hr_tss_coefficient IS
  'Table of TSS/hour coefficients by heart rate zone, bounds expressed as PERCENTAGE OF LTHR (not max HR). VERIFIED (04 section 5.2): "This table is an arbitrary convention and sources openly contradict each other [...] it must be stored in a modifiable SQL table, never hardcoded." It is interpolated on the numeric anchors published by Joe Friel. Modifying it and running a REFRESH of the mart views is enough to recalculate the entire hrTSS history: no re-ingestion.';

INSERT INTO core.hr_tss_coefficient (zone_code, zone_order, lo_pct_lthr, hi_pct_lthr, tss_per_hour, label) VALUES
  ('Z1',  1,   0.00, 0.81,  20, 'Recovery'),
  ('Z2',  2,   0.81, 0.90,  55, 'Endurance'),
  ('Z3',  3,   0.90, 0.94,  75, 'Tempo'),
  ('Z4',  4,   0.94, 1.00,  90, 'Threshold'),
  ('Z5a', 5,   1.00, 1.03, 105, 'Z5a'),
  ('Z5b', 6,   1.03, 1.07, 120, 'Z5b'),
  ('Z5c', 7,   1.07, 9.99, 140, 'Z5c')
ON CONFLICT DO NOTHING;


CREATE TABLE core.metric_method_priority (
    metric      text     NOT NULL,
    sport_code  text     NOT NULL REFERENCES core.sport(sport_code),
    method_code text     NOT NULL REFERENCES core.load_method(method_code),
    priority    smallint NOT NULL,
    CONSTRAINT metric_method_priority_pk PRIMARY KEY (metric, sport_code, method_code),
    CONSTRAINT metric_method_priority_uq UNIQUE (metric, sport_code, priority),
    CONSTRAINT metric_method_priority_pos CHECK (priority >= 1)
);

COMMENT ON TABLE core.metric_method_priority IS
  'DECLARATIVE fallback matrix: for a (metric, sport) pair, which method is authoritative, and in what order if unavailable. Materializes table 04 section 0.3 in the database. Raison d''etre: without it, the cascade "power else HR else RPE" gets recopied as CASE WHEN in every Grafana view -- unauditable, untestable, divergent on the second copy. The UNIQUE constraint (metric, sport_code, priority) forbids two methods ex aequo, thus any resolution ambiguity. This table feeds the calculation of core.activity_metric.is_canonical.';

INSERT INTO core.metric_method_priority (metric, sport_code, method_code, priority) VALUES
  ('tss','cycling',        'bike_power',1), ('tss','cycling',        'hr_hrtss',2), ('tss','cycling',        'rpe',3),
  ('tss','virtual_cycling','bike_power',1), ('tss','virtual_cycling','hr_hrtss',2), ('tss','virtual_cycling','rpe',3),
  ('tss','running',        'run_power', 1), ('tss','running',        'run_gap', 2), ('tss','running',        'hr_hrtss',3),
  ('tss','trail_running',  'run_power', 1), ('tss','trail_running',  'run_gap', 2), ('tss','trail_running',  'hr_hrtss',3),
  ('tss','treadmill',      'run_power', 1), ('tss','treadmill',      'hr_hrtss',2),
  ('tss','lap_swimming',   'swim_css',  1), ('tss','lap_swimming',   'hr_hrtss',2), ('tss','lap_swimming',   'rpe',3),
  ('tss','open_water',     'swim_css',  1), ('tss','open_water',     'hr_hrtss',2), ('tss','open_water',     'rpe',3),
  ('tss','hiking',         'hr_hrtss',  1), ('tss','hiking',         'rpe',2),
  ('tss','resort_skiing',  'hr_hrtss',  1), ('tss','resort_skiing',  'rpe',2),
  ('tss','strength',       'hr_hrtss',  1), ('tss','strength',       'rpe',2),
  -- multisport and transition are seeded in core.sport but had NO chain here, and
  -- resolve_canonical() RAISES on a (metric, sport_code) pair it does not find -- it
  -- refuses to return None silently for what would be a configuration error. A triathlon
  -- therefore crashed the load computation outright. Neither sport has a discipline of
  -- its own (DISCIPLINE_BY_SPORT_CODE maps both to 'other'), so no power- or pace-based
  -- method applies: heart rate first, manual RPE as the last resort, exactly like hiking
  -- and resort_skiing. Counting the multisport PARENT does not double-count its legs:
  -- every mart view filters on is_parent = false (007/008).
  ('tss','multisport',     'hr_hrtss',  1), ('tss','multisport',     'rpe',2),
  ('tss','transition',     'hr_hrtss',  1), ('tss','transition',     'rpe',2)
ON CONFLICT DO NOTHING;


CREATE TABLE core.pmc_seed (
    athlete_id   smallint  NOT NULL REFERENCES core.athlete(athlete_id),
    seed_date    date      NOT NULL,
    ctl          real      NOT NULL,
    atl          real      NOT NULL,
    strategy     text      NOT NULL,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pmc_seed_pk PRIMARY KEY (athlete_id, seed_date),
    CONSTRAINT pmc_seed_strategy_ck CHECK (strategy IN ('manual','stationary_42d','zero')),
    CONSTRAINT pmc_seed_nonneg CHECK (ctl >= 0 AND atl >= 0)
);

COMMENT ON TABLE core.pmc_seed IS
  'Seed values for the CTL/ATL recursion at the history start date. VERIFIED (04 section 6.5.3): initializing to 0 without warning is "to be avoided" because it "creates fictional progression"; the recommended default is the stationary seed (average of daily TSS of the first 42 days). DOCUMENTED PITFALL (04 section 6.5.3): this average introduces circularity if recalculated on a sliding window -- it must be calculated EXACTLY ONCE on the fixed start date, then STORED. This table is precisely that storage; the seed is never recalculated by the mart.pmc view.';
