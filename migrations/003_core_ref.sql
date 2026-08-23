-- Migration 003: référentiels core (sport, load_method, threshold, hr_tss_coefficient, metric_method_priority, pmc_seed) -- seeds rendus idempotents (ON CONFLICT DO NOTHING, ajout explicite hors DDL figé)
-- Source : research/05-schema-postgres.md §3, transcrit tel quel (DDL figé).
-- Lot L1 (garminator) -- ne pas modifier la DDL manuellement hors migration.

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
  'L''athlète. Mono-utilisateur en pratique, mais la clé étrangère est portée partout pour ne pas fermer la porte. sex n''est pas cosmétique : il sélectionne le coefficient du TRIMP de Banister (VÉRIFIÉ 04 §5.1, k=0.64/1.92 homme vs 0.86/1.67 femme) — 04 exige explicitement qu''il soit configurable et non codé en dur.';
COMMENT ON COLUMN core.athlete.home_timezone IS
  'Identifiant IANA du fuseau de résidence. Sert UNIQUEMENT de valeur de repli quand le fuseau d''une activité ou d''une journée est indéterminable. La date locale de rattachement est toujours matérialisée à l''ingestion, jamais recalculée depuis ce champ dans une vue (cf. §7).';


CREATE TABLE core.sport (
    sport_code      text     PRIMARY KEY,
    fit_sport       smallint NULL,
    fit_sub_sport   smallint NULL,
    label_fr        text     NOT NULL,
    discipline      text     NOT NULL,
    is_indoor       boolean  NOT NULL DEFAULT false,
    produces_records boolean NOT NULL DEFAULT true,
    CONSTRAINT sport_discipline_ck CHECK (discipline IN ('bike','run','swim','other'))
);

COMMENT ON TABLE core.sport IS
  'Table de correspondance entre le couple (sport, sub_sport) du profil FIT officiel et un code de sport canonique garminator. Ce N''EST PAS une simple table de libellés — elle porte deux informations décisionnelles : discipline (qui pilote la matrice de repli de charge, 04 §0.3) et is_indoor (qui force f(i)=1 dans le calcul de GAP, VÉRIFIÉ 04 §3.3.4 : « Tapis de course : pente non fiable »). Une simple colonne text libre sur core.activity ne permettrait pas d''exprimer ces règles de façon déclarative.';
COMMENT ON COLUMN core.sport.produces_records IS
  'false pour les sports qui n''émettent pas de message FIT record par seconde (nage en piscine). Sert au dimensionnement et aux tests de non-régression sur le volume de core.sample.';

INSERT INTO core.sport (sport_code, label_fr, discipline, is_indoor, produces_records) VALUES
  ('running',        'Course à pied',        'run',   false, true),
  ('trail_running',  'Trail',                'run',   false, true),
  ('treadmill',      'Course sur tapis',     'run',   true,  true),
  ('cycling',        'Cyclisme',             'bike',  false, true),
  ('virtual_cycling','Cyclisme virtuel',     'bike',  true,  true),
  ('lap_swimming',   'Natation en piscine',  'swim',  true,  false),
  ('open_water',     'Natation en eau libre','swim',  false, true),
  ('hiking',         'Randonnée',            'other', false, true),
  ('resort_skiing',  'Ski en station',       'other', false, true),
  ('multisport',     'Multisport',           'other', false, false),
  ('transition',     'Transition',           'other', false, true),
  ('strength',       'Renforcement',         'other', true,  true)
ON CONFLICT DO NOTHING;


CREATE TABLE core.load_method (
    method_code   text    PRIMARY KEY,
    label_fr      text    NOT NULL,
    scale         text    NOT NULL,
    is_measured   boolean NOT NULL,
    CONSTRAINT load_method_scale_ck CHECK (scale IN ('tss','trimp','rpe','none'))
);

COMMENT ON TABLE core.load_method IS
  'Méthodes de calcul de charge. La colonne scale est STRUCTURANTE et non décorative : elle interdit d''additionner dans le CTL des grandeurs d''échelles différentes. VÉRIFIÉ (04 §5.1) : « 1 h au seuil donne ~170 TRIMP, pas 100. Le TRIMP n''est pas sur l''échelle du TSS et ne doit JAMAIS être additionné à des TSS dans le CTL. » Une vue qui somme la charge journalière DOIT filtrer sur scale = ''tss''. is_measured distingue une charge mesurée d''une charge estimée, pour la coloration Grafana exigée en 04 §5.3.';

INSERT INTO core.load_method (method_code, label_fr, scale, is_measured) VALUES
  ('bike_power', 'TSS puissance vélo (NP/FTP)',          'tss',   true),
  ('run_power',  'rTSS puissance course (NP/rFTP)',      'tss',   true),
  ('run_gap',    'rTSS allure corrigée pente (NGP/FTPa)','tss',   true),
  ('swim_css',   'sTSS natation (vitesse/CSS)',          'tss',   true),
  ('hr_hrtss',   'hrTSS par histogramme de FC',          'tss',   false),
  ('hr_trimp',   'TRIMP de Banister par échantillon',    'trimp', false),
  ('rpe',        'Charge saisie manuellement (RPE)',     'rpe',   false)
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
    -- Interdit structurellement de mélanger les deux échelles de puissance.
    CONSTRAINT threshold_bike_only CHECK (discipline <> 'bike' OR (rftp_watts IS NULL AND css_m_s IS NULL)),
    CONSTRAINT threshold_run_only  CHECK (discipline <> 'run'  OR (ftp_watts  IS NULL AND css_m_s IS NULL)),
    CONSTRAINT threshold_swim_only CHECK (discipline <> 'swim' OR (ftp_watts  IS NULL AND rftp_watts IS NULL)),
    CONSTRAINT threshold_hr_order  CHECK (hr_max_bpm IS NULL OR hr_rest_bpm IS NULL OR hr_max_bpm > hr_rest_bpm),
    CONSTRAINT threshold_css_range CHECK (css_m_s IS NULL OR (100.0/css_m_s) BETWEEN 45 AND 180)
);

COMMENT ON TABLE core.threshold IS
  'Seuils physiologiques HISTORISÉS PAR PÉRIODE DE VALIDITÉ. Contrainte produit non négociable, réaffirmée par 04 §2.3 : « Le TSS d''une activité utilise le seuil en vigueur à sa date. Sinon une mise à jour de FTP réécrirait rétroactivement tout l''historique de CTL. » La contrainte d''exclusion GiST garantit qu''aucune date n''est couverte par deux lignes de la même discipline : la jointure « le seuil du jour J » ne peut donc jamais renvoyer deux lignes. Les contraintes CHECK par discipline interdisent physiquement de renseigner ftp_watts (vélo, puissance mécanique mesurée) et rftp_watts (course, puissance modélisée Garmin) sur la même ligne — VÉRIFIÉ 04 §3.2.1 : « Règle absolue : rFTP et FTP vélo ne sont pas comparables et ne doivent jamais être mélangés. »';
COMMENT ON COLUMN core.threshold.validity IS
  'Intervalle de dates [debut, fin) en convention semi-ouverte. La borne haute NULL/infinity désigne le seuil courant. Utiliser validity @> a.local_date pour joindre une activité à son seuil.';
COMMENT ON COLUMN core.threshold.power_source IS
  'Origine de la mesure de puissance (garmin_native, stryd, powermeter_<modele>). VÉRIFIÉ (04 §3.2) : la Running Power Garmin est un modèle propriétaire non documenté ; « un 250 W Garmin et un 250 W Stryd ne sont pas la même mesure ». Un changement de valeur ici DOIT clore la période de validité et invalider le rFTP.';
COMMENT ON COLUMN core.threshold.css_estimated IS
  'true quand la CSS provient du repli grossier « 1000 m CLM x 0.97 » faute de double test 200/400 m (VÉRIFIÉ 04 §4.1). À exposer dans Grafana : un sTSS bâti sur une CSS estimée n''a pas le même statut.';


CREATE TABLE core.hr_tss_coefficient (
    zone_code     text     PRIMARY KEY,
    zone_order    smallint NOT NULL UNIQUE,
    lo_pct_lthr   real     NOT NULL,
    hi_pct_lthr   real     NOT NULL,
    tss_per_hour  real     NOT NULL,
    label_fr      text     NOT NULL,
    CONSTRAINT hr_tss_bounds_ck CHECK (hi_pct_lthr > lo_pct_lthr),
    CONSTRAINT hr_tss_rate_ck   CHECK (tss_per_hour >= 0)
);

COMMENT ON TABLE core.hr_tss_coefficient IS
  'Table des coefficients TSS/heure par zone de FC, bornes exprimées en POURCENTAGE DU LTHR (et non de la FC max). VÉRIFIÉ (04 §5.2) : « Cette table est une convention arbitraire et les sources se contredisent ouvertement [...] elle doit être stockée en table SQL modifiable, jamais codée en dur. » Elle est interpolée sur les ancrages chiffrés publiés par Joe Friel. La modifier et lancer un REFRESH des vues mart suffit à recalculer tout l''historique de hrTSS : aucune réingestion.';

INSERT INTO core.hr_tss_coefficient (zone_code, zone_order, lo_pct_lthr, hi_pct_lthr, tss_per_hour, label_fr) VALUES
  ('Z1',  1,   0.00, 0.81,  20, 'Récupération'),
  ('Z2',  2,   0.81, 0.90,  55, 'Endurance'),
  ('Z3',  3,   0.90, 0.94,  75, 'Tempo'),
  ('Z4',  4,   0.94, 1.00,  90, 'Seuil'),
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
  'Matrice de repli DÉCLARATIVE : pour un couple (métrique, sport), quelle méthode fait foi, et dans quel ordre en cas d''indisponibilité. Matérialise en base la table 04 §0.3. Raison d''être : sans elle, la cascade « puissance sinon FC sinon RPE » se recopie en CASE WHEN dans chaque vue Grafana — non auditable, non testable, divergente dès la deuxième copie. La contrainte UNIQUE (metric, sport_code, priority) interdit deux méthodes ex aequo, donc toute ambiguïté de résolution. C''est cette table qui alimente le calcul de core.activity_metric.is_canonical.';

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
  ('tss','strength',       'hr_hrtss',  1), ('tss','strength',       'rpe',2)
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
  'Valeurs d''amorçage de la récursion CTL/ATL à la date de début de l''historique. VÉRIFIÉ (04 §6.5.3) : initialiser à 0 sans avertissement est « à proscrire » car cela « fabrique une progression fictive » ; le défaut recommandé est le seed stationnaire (moyenne des TSS journaliers des 42 premiers jours). PIÈGE DOCUMENTÉ (04 §6.5.3) : cette moyenne introduit une circularité si on la recalcule sur une fenêtre glissante — elle doit être calculée UNE SEULE FOIS sur la date de début fixe, puis STOCKÉE. Cette table est précisément ce stockage ; le seed n''est jamais recalculé par la vue mart.pmc.';
