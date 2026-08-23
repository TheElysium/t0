-- Migration 006: core : wellness_metric (seed idempotent), wellness_sample, daily_wellness, sleep_session, sleep_stage
-- Source : research/05-schema-postgres.md §3, transcrit tel quel (DDL figé).
-- Lot L1 (garminator) -- ne pas modifier la DDL manuellement hors migration.

CREATE TABLE core.wellness_metric (
    metric_id     smallint PRIMARY KEY,
    metric_code   text     NOT NULL UNIQUE,
    label_fr      text     NOT NULL,
    unit          text     NOT NULL,
    api_method    text     NULL,
    is_intraday   boolean  NOT NULL DEFAULT true
);

COMMENT ON TABLE core.wellness_metric IS
  'Référentiel des métriques de bien-être intrajournalières. Le metric_id est un smallint DÉLIBÉRÉMENT COURT car il est répété sur plusieurs millions de lignes de core.wellness_sample : un text y coûterait plusieurs dizaines de méga-octets pour rien. api_method conserve le nom de la méthode garminconnect 0.3.2 correspondante, ce qui permet de rejouer la collecte d''une seule métrique.';

INSERT INTO core.wellness_metric (metric_id, metric_code, label_fr, unit, api_method) VALUES
  (1,'heart_rate',     'Fréquence cardiaque',      'bpm',   'get_heart_rates'),
  (2,'stress',         'Stress',                   'score', 'get_stress_data'),
  (3,'body_battery',   'Body Battery',             'score', 'get_body_battery'),
  (4,'steps',          'Pas',                      'count', 'get_steps_data'),
  (5,'respiration',    'Fréquence respiratoire',   'rpm',   'get_respiration_data'),
  (6,'spo2',           'SpO2',                     'pct',   'get_spo2_data'),
  (7,'hrv',            'Variabilité cardiaque',    'ms',    'get_hrv_data'),
  (8,'intensity_min',  'Minutes d''intensité',     'min',   'get_intensity_minutes_data'),
  (9,'floors',         'Étages',                   'count', 'get_floors')
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
  'Séries intrajournalières de bien-être, format long. ORDRE DE LA CLÉ PRIMAIRE : (metric_id, t) et non (t, metric_id). Raison : toute requête Grafana filtre d''abord sur une métrique unique puis sur une plage de temps, ce que sert un parcours d''index parfaitement contigu ; l''ordre inverse imposerait un parcours de toute la plage temporelle avec filtre sur metric_id. Ce point m''a été opposé par le relecteur et il a raison (cf. §10, concession 6). PAS de clé étrangère : table convertible en hypertable, et Timescale interdit qu''une hypertable soit cible de clé étrangère — l''intégrité référentielle vers un référentiel de 9 lignes est un risque nul assumé. VOLUME : ~1,1 M de points par an, ~5,5 M à 5 ans (SUPPOSÉ, cf. §5.4 : la cadence réelle par métrique n''est pas vérifiée, seul l''ordre de grandeur l''est).';
COMMENT ON COLUMN core.wellness_sample.local_date IS
  'Date locale de rattachement, matérialisée à l''ingestion. Les endpoints wellness Garmin sont nativement clés par calendarDate locale : recalculer cette date dans une vue par AT TIME ZONE rejouerait exactement les bugs de fuseau documentés du projet de référence (VÉRIFIÉ 01, issues #85, #40, #137, #185, #25).';
COMMENT ON COLUMN core.wellness_sample.value IS
  'real (4 octets) et non double precision : aucune de ces métriques n''a plus de 3 chiffres significatifs, et la moitié sont des entiers. Économie directe de 4 octets sur plusieurs millions de lignes.';

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
  'Agrégats journaliers fournis PAR GARMIN (et non recalculés depuis core.wellness_sample) : FC de repos, statut VFC, Body Battery, VO2max estimée. Format LARGE assumé ici, contrairement à core.wellness_sample : ces champs sont peu nombreux, stables, tous présents le même jour, et systématiquement lus ensemble sur un tableau de bord journalier. Une seule ligne par jour, soit ~1 800 lignes à 5 ans : la normalisation n''apporterait rien et coûterait une jointure. RISQUE CONNU (VÉRIFIÉ 03) : sur Forerunner 255, training_readiness, endurance_score et hill_score peuvent rester vides — colonnes nullable, jamais NOT NULL.';
COMMENT ON COLUMN core.daily_wellness.source_sha256 IS
  'Hash du payload journalier ayant produit cette ligne. Permet de détecter qu''une journée a été republiée par Garmin avec un contenu différent (recalcul de sommeil, wellness/epoch/request) sans comparer champ à champ.';


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
  'Nuit de sommeil, CLÉ PAR LA DATE DE RÉVEIL et non par la date de coucher. Arbitrage de fuseau tranché une fois pour toutes (cf. §7) : une nuit commencée le 3 à 23 h 40 et finie le 4 à 7 h se rattache au 4, ce qui est la convention Garmin et la seule qui permette de corréler « sommeil de la nuit » et « séance du lendemain » sans décalage d''un jour. La PK par (athlete_id, wake_date) rend l''upsert d''une nuit republiée trivial et l''ordre de rattachement non ambigu.';

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
  'Phases de sommeil sous forme d''INTERVALLES (début, fin) et non d''échantillons par minute : le sommeil est nativement décrit par Garmin comme une suite de segments, et les stocker tels quels divise le volume par ~30 tout en restant exact. Le ON DELETE CASCADE permet de remplacer une nuit republiée par un simple DELETE de la session suivi d''un réinsert.';
