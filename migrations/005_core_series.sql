-- Migration 005: core : sample, sample_derived, activity_hr_histogram, activity_power_curve, activity_metric
-- Source : research/05-schema-postgres.md §3, transcrit tel quel (DDL figé).
-- Lot L1 (garminator) -- ne pas modifier la DDL manuellement hors migration.

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
  'Série par seconde, TOUS SPORTS CONFONDUS, image fidèle du message FIT record (mesg num 20, 84 champs — VÉRIFIÉ 02 §2). Table unique et non une table par discipline : les colonnes NULL ne coûtent qu''un bit dans le bitmap de valeurs nulles de l''en-tête de tuple, alors que trois tables imposeraient un UNION dans chaque requête multisport et une triple maintenance d''index. AUCUNE valeur dérivée ici : cette table est reconstructible à l''identique depuis le .FIT et rien d''autre. DIMENSIONNEMENT (calcul, cf. §5) : ~3,2 à 4 M de lignes à 5 ans, ~0,5 à 0,6 Go index compris. CONCLUSION EXPLICITE : PAS DE PARTITIONNEMENT, ni maintenant ni « au cas où ». La PK inclut t, donc la conversion en hypertable TimescaleDB reste possible sans migration si le volume démentait un jour cette estimation.';
COMMENT ON COLUMN core.sample.t IS
  'Horodatage absolu de l''échantillon (timestamptz). Composant de la PK ET colonne de partitionnement potentielle. L''ordre (activity_id, t) est délibéré : la requête dominante de Grafana est « toute la série d''UNE activité », servie par un parcours d''index contigu.';
COMMENT ON COLUMN core.sample.elapsed_s IS
  'Secondes écoulées depuis le départ. Seule redondance tolérée dans cette table (dérivable de t moins activity.start_time) : elle évite une jointure et un calcul d''intervalle dans chaque graphe Grafana d''une séance, pour 4 octets par ligne.';
COMMENT ON COLUMN core.sample.latitude_deg IS
  'Degrés décimaux, double precision — et non l''entier semicircles du FIT. La conversion est faite une fois à l''ingestion ; le geomap Grafana et PostGIS attendent des degrés. Coût nul sur les activités indoor et en piscine : une colonne NULL n''occupe aucun octet de données.';
COMMENT ON COLUMN core.sample.power IS
  'Puissance instantanée en watts, brute. Renseignée à la fois pour le vélo (capteur) et pour la course (Running Power Garmin, présente sur 155/155 des courses — VÉRIFIÉ 04 §0.1). Les deux échelles ne sont PAS comparables (04 §3.2.1) : c''est core.activity.sport_code qui les distingue, et core.threshold qui interdit de partager un seuil.';
COMMENT ON COLUMN core.sample.extra IS
  'Champs FIT restants (dont les developer fields, messages 206/207) sous forme jsonb, pour ne rien perdre sans coloniser 84 champs. Volontairement NON indexé en GIN : aucune requête identifiée ne filtre dessus, et un index GIN sur plusieurs millions de lignes coûterait plus que la table. À ajouter le jour où un besoin précis apparaît, pas avant.';

CREATE INDEX sample_t_brin ON core.sample USING brin (t) WITH (pages_per_range = 32);
COMMENT ON INDEX core.sample_t_brin IS
  'BRIN et non B-tree : les lignes sont insérées par activité, donc physiquement quasi triées par t, ce qui est exactement le cas d''usage du BRIN. Coût : quelques dizaines de kilo-octets, contre ~110 Mo pour un B-tree sur la même colonne. Sert les requêtes de plage temporelle inter-activités (« toutes les données de mars »).';

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
  'Séries par seconde CALCULÉES (lissage 30 s de la puissance, pente lissée, facteur de correction de pente, vitesse équivalente plat). Séparée de core.sample pour une raison précise : core.sample est un fait immuable, ceci est le produit d''une version d''algorithme. Une correction de la fenêtre de lissage de pente se traite par TRUNCATE + recalcul, sans jamais toucher aux données brutes ni re-télécharger un FIT. Écrite dans la MÊME transaction que le parsing. PAS de clé étrangère vers core.activity : cette table est convertible en hypertable et Timescale interdit qu''une hypertable soit cible de clé étrangère ; l''intégrité est assurée par la reconstruction en bloc.';
COMMENT ON COLUMN core.sample_derived.grade_pct IS
  'Pente lissée sur une fenêtre de +/- 10 m de DISTANCE (et non de temps, qui donnerait une fenêtre variable selon l''allure), écrêtée à +/- 30 %. VÉRIFIÉ 04 §3.3.2 : sans lissage, le bruit de l''altimètre barométrique produit des pentes aberrantes seconde par seconde.';
COMMENT ON COLUMN core.sample_derived.grade_factor IS
  'Facteur multiplicatif f(i) appliqué à la vitesse pour obtenir la vitesse équivalente sur plat. Forcé à 1 sur tapis de course (core.sport.is_indoor), VÉRIFIÉ 04 §3.3.4.';
COMMENT ON COLUMN core.sample_derived.imputed IS
  'true si la valeur de puissance de cette seconde a été reconstituée (interpolation ou remplissage à 0 W). VÉRIFIÉ 04 §1.3. La somme de ces drapeaux alimente core.activity.imputed_seconds.';

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
  'Histogramme de fréquence cardiaque à la résolution de 1 BATTEMENT PAR MINUTE (temps passé à chaque bpm entier). Remplace délibérément le stockage de seconds_in_zone_1..7. VÉRIFIÉ (04 §5.4) : « Stocker le temps par zone fige les bornes de zones dans la donnée. [...] Stocker l''histogramme à 1 bpm rend le temps par zone recalculable pour n''importe quel jeu de bornes. » CONSÉQUENCE DÉCISIVE : corriger un LTHR erroné devient un UPDATE dans core.threshold suivi d''un REFRESH des vues mart — pas une réingestion de 300 activités. COÛT : environ 200 lignes par activité, soit ~60 000 lignes pour l''historique existant et ~220 000 à 5 ans — négligeable devant core.sample. Ne dispense pas de conserver core.sample : l''histogramme perd l''ordre chronologique, donc le découplage aérobie et le TRIMP par échantillon en restent tributaires.';
COMMENT ON COLUMN core.activity_hr_histogram.seconds IS
  'Nombre de secondes passées exactement à cette valeur entière de FC. La somme sur une activité vaut le temps de FC valide, qui peut être inférieur à duration_s en cas de décrochage de ceinture.';


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
  'Courbe des meilleures moyennes glissantes d''une activité : pour chaque durée d, la meilleure valeur moyenne soutenue pendant d secondes. Précalculée à l''ingestion sur une grille d''environ 60 à 100 DURÉES LOG-ESPACÉES (d_j = round(10^(j/20)) dédoublonné, VÉRIFIÉ 04 §8.2) et non sur toutes les durées entières : la courbe est lisse en échelle log, une résolution fine au-delà de la minute n''apporte rien et multiplierait le volume par cent. Le calcul en SQL à la volée sur plusieurs millions de lignes est hors de portée d''un tableau de bord interactif — d''où la précalculation.';
COMMENT ON COLUMN core.activity_power_curve.kind IS
  'Nature de la courbe, explicite et sans ambiguïté d''unité. bike_power et run_power sont en watts et NE DOIVENT JAMAIS ÊTRE FUSIONNÉS (VÉRIFIÉ 04 §8.3, échelles incomparables). run_speed et swim_speed sont en m/s : la vitesse et non l''allure, pour que le meilleur effort soit toujours un MAX (une allure, en s/km, exigerait un MIN et casserait toute agrégation générique). L''allure d''affichage se dérive par 1000/value. Sans run_speed, la courbe record ignorerait les 155 activités de course sur 300 pour lesquelles l''allure est la référence historique.';
COMMENT ON COLUMN core.activity_power_curve.spans_pause IS
  'true si la fenêtre optimale enjambe une pause excisée : la moyenne est alors physiologiquement fausse (deux efforts recollés). VÉRIFIÉ 04 §8.3 : ces points doivent être exclus de la courbe record de période, tout en restant visibles sur l''activité.';

CREATE INDEX activity_power_curve_best_idx
    ON core.activity_power_curve (kind, duration_s, value DESC)
    WHERE NOT spans_pause;
COMMENT ON INDEX core.activity_power_curve_best_idx IS
  'Index de service de la courbe record de période : « meilleure valeur sur d secondes parmi les activités d''une plage ». L''ordre DESC sur value permet au planificateur de s''arrêter à la première ligne par groupe. Le filtre partiel exclut d''emblée les fenêtres à cheval sur une pause.';


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
  'Métriques calculées par activité, en FORMAT LONG (une ligne par méthode de calcul) et non en colonnes larges. Justification : une même activité peut légitimement porter plusieurs valeurs de TSS obtenues par des méthodes différentes (puissance ET fréquence cardiaque), et l''on veut pouvoir les comparer, pas en écraser une. Un format large imposerait tss_power, tss_hr, tss_gap, tss_rpe... et une colonne de plus à chaque nouvelle méthode. L''affichage large reste disponible via la vue mart.activity_wide : table étroite en écriture, vue large en lecture.';
COMMENT ON COLUMN core.activity_metric.is_canonical IS
  'Désigne LA valeur qui fait foi pour cette activité et cette métrique, résolue via core.metric_method_priority. L''index unique partiel garantit qu''il n''y en a jamais deux : l''ambiguïté est interdite par le schéma, pas par une convention de code. Toute vue de charge lit exclusivement is_canonical.';
COMMENT ON COLUMN core.activity_metric.threshold_id IS
  'Seuil effectivement utilisé pour ce calcul. Traçabilité indispensable : sans lui, on ne peut pas répondre à « ce TSS de 2025 a-t-il été calculé avec l''ancienne FTP ? » après une mise à jour de seuil.';
COMMENT ON COLUMN core.activity_metric.value IS
  'NULL est une valeur légitime et SIGNIFIANTE : elle veut dire « non calculable » et se distingue de 0. VÉRIFIÉ 04 §0.3 : quand la puissance est présente mais la FC absente (cas observé sur 4 des 19 séances de home-trainer), trimp, hrtss, decoupling et efficiency_factor doivent valoir NULL et jamais 0, sous peine de faire chuter artificiellement toutes les moyennes.';

CREATE INDEX activity_metric_metric_idx ON core.activity_metric (metric, method_code);
