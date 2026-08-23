-- Migration 004: core : device, activity, activity_lap, swim_length
-- Source : research/05-schema-postgres.md §3, transcrit tel quel (DDL figé).
-- Lot L1 (garminator) -- ne pas modifier la DDL manuellement hors migration.

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
  'Montre, capteur de puissance, ceinture cardio, capteur de cadence — alimenté par les messages FIT device_info (mesg num 23, VÉRIFIÉ 02 §2). Utilité concrète et non théorique : le capteur de puissance vélo n''étant présent que sur 1 sortie route sur 10 (VÉRIFIÉ, cf. §0.1), c''est ici qu''on peut prouver « la puissance manque parce que le capteur était absent » plutôt que « le champ était vide ». Sert aussi à invalider un rFTP lors d''un changement de source de puissance (core.threshold.power_source).';


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
  'Une séance. La clé primaire est l''activityId Garmin lui-même, pas une clé de substitution : il est stable et c''est la seule clé qui garantisse l''idempotence face aux modifications rétroactives. RAPPEL DU BUG DE RÉFÉRENCE (VÉRIFIÉ 01, issue #187) : garmin-grafana duplique une activité quand son type ou son appareil change a posteriori, parce que ces attributs font partie du tagset InfluxDB donc de l''identité de la ligne. Ici, type et appareil sont des COLONNES, jamais des composants de clé : un changement de type est un UPDATE, pas une nouvelle ligne. Le bug est structurellement impossible.';
COMMENT ON COLUMN core.activity.start_time IS
  'Instant absolu de départ, timestamptz (donc stocké en UTC). Sert aux jointures temporelles avec le wellness et à l''ordonnancement.';
COMMENT ON COLUMN core.activity.start_time_local IS
  'Heure murale de l''athlète au départ, MATÉRIALISÉE à l''ingestion (timestamp sans fuseau, à ne jamais reconvertir). C''est elle qui répond à « ai-je couru le matin ou le soir ? ». Aucune vue Grafana ne doit contenir AT TIME ZONE (cf. §7).';
COMMENT ON COLUMN core.activity.local_date IS
  'Date locale de rattachement, matérialisée. Clé d''agrégation journalière de TOUT le projet : PMC, charge quotidienne, jointure avec le wellness (dont les endpoints Garmin sont nativement clés par calendarDate locale). Une agrégation par date UTC décalerait les séances du soir d''été d''un jour.';
COMMENT ON COLUMN core.activity.tz_offset_s IS
  'Décalage UTC en secondes au moment du départ, conservé pour pouvoir prouver a posteriori comment local_date a été calculée (et détecter un voyage / changement d''heure).';
COMMENT ON COLUMN core.activity.duration_s IS
  'Durée chrono (timer time), en SECONDES ENTIÈRES. Choix de typage délibéré : jamais le type time (antipattern relevé VÉRIFIÉ 02 §3 dans GarminDB, où time déborde à 24 h et n''est pas sommable). C''est cette colonne — et non elapsed_s — qui donne le volume réel : 1 076 042 s sur 300 activités (VÉRIFIÉ, Activities.csv), soit 298,9 h, contre 1 159 651 s de temps écoulé.';
COMMENT ON COLUMN core.activity.np_watts IS
  'Puissance normalisée recalculée par garminator au parsing du FIT (moyenne glissante 30 s, puis moyenne des puissances 4, VÉRIFIÉ 04 §1.1). N''est JAMAIS lue depuis le champ FIT session.normalized_power : celui-ci n''est renseigné que si Garmin l''a calculé en amont et sa convention de lissage n''est pas documentée.';
COMMENT ON COLUMN core.activity.np_reliable IS
  'false quand la série exploitable compte entre 30 et 300 s (NP mathématiquement définie mais peu significative) ; NULL/absent quand N < 30 s auquel cas np_watts est NULL. VÉRIFIÉ 04 §1.5.';
COMMENT ON COLUMN core.activity.avg_power_nonzero IS
  'Moyenne de puissance excluant les zéros. Stockée EN PLUS de avg_power car la différence entre les deux est le diagnostic direct de roue libre / descente, et parce que la convention Garmin sur ce point n''est pas documentée (VÉRIFIÉ 04 §1.7).';
COMMENT ON COLUMN core.activity.imputed_seconds IS
  'Nombre de secondes de puissance reconstituées (interpolation <= 3 s, ou remplissage à 0 W sur trou sans pause). VÉRIFIÉ 04 §1.3 : au-delà de 10 % de la durée, data_quality bascule à poor. Sans cette colonne, une NP calculée sur une série trouée est indiscernable d''une NP propre.';
COMMENT ON COLUMN core.activity.swim_time_s IS
  'Temps de nage effectif, reconstruit par somme des messages FIT length de type actif. VÉRIFIÉ 04 §4.3 : « Utiliser le temps écoulé donne un sTSS surestimé de 30 à 50 % » sur une séance à répétitions. En eau libre, où les length n''existent pas, swim_time_source vaut elapsed et data_quality est plafonné à fair.';
COMMENT ON COLUMN core.activity.parent_activity_id IS
  'Renseigné sur les segments (legs) d''un enchaînement multisport ; NULL sur une activité simple ou sur le parent. VÉRIFIÉ (Activities.csv, 2 activités Multisport) : la ligne parente porte FC et puissance à « -- » et une « Allure moyenne » de 20,3 qui est en réalité une vitesse km/h. La ligne parente est donc INEXPLOITABLE telle quelle : ses totaux doivent être recalculés par somme des enfants. Toute vue de charge DOIT filtrer is_parent = false pour ne pas double-compter.';
COMMENT ON COLUMN core.activity.summary_sha256 IS
  'sha256 du résumé JSON get_activity(activity_id), canonicalisé et débarrassé des champs volatils. DEUXIÈME mécanisme de hachage, indépendant de raw.fit_file.sha256 : le cas réellement documenté du bug #187 est un changement de type ou de titre SANS nouveau fichier FIT. Le hash du FIT ne bouge alors pas ; seul celui-ci détecte la modification (cf. §6.3).';
COMMENT ON COLUMN core.activity.needs_manual_rpe IS
  'true quand ni puissance ni FC exploitables ne permettent d''estimer la charge : le TSS reste NULL (jamais 0) et l''activité est mise en file d''attente d''une saisie manuelle. VÉRIFIÉ 04 §0.3.';

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
  'Tours (message FIT lap, mesg num 19, 124 champs au profil officiel — VÉRIFIÉ 02 §2). Niveau intermédiaire conservé tel quel plutôt que recalculé : les bornes de tour sont une intention d''entraînement (départ de fractionné) qu''aucune reconstruction depuis les échantillons ne peut deviner. Les ~124 champs du profil ne sont pas tous colonnés : les usuels sont typés, le reste part dans extra.';
COMMENT ON COLUMN core.activity_lap.np_convention IS
  'Convention de calcul de la NP du tour. global_window (défaut) : la série glissante 30 s est calculée UNE FOIS sur l''activité entière puis restreinte aux indices du tour — VÉRIFIÉ 04 §1.2, convention (C) retenue. CONSÉQUENCE À ASSUMER : la NP d''un tour n''est alors PAS recalculable depuis les seules données du tour ; cette colonne existe pour que cette non-recalculabilité soit documentée en base et non découverte par un utilisateur perplexe.';


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
  'Longueurs de bassin (message FIT length, mesg num 101, 22 champs — VÉRIFIÉ 02 §2). Table INDISPENSABLE et non redondante avec les tours : en piscine, la montre n''émet PAS de message record par seconde, donc core.sample est vide et la longueur est la seule granularité fine existante. C''est la table la plus rentable du schéma : 80 activités de natation piscine (VÉRIFIÉ, Activities.csv) pour un volume dérisoire (~40 à 80 lignes par séance).';
COMMENT ON COLUMN core.swim_length.length_type IS
  'active = longueur nagée, idle = repos au mur. La distinction est ce qui rend le sTSS correct : sommer la durée des seules longueurs actives donne core.activity.swim_time_s. VÉRIFIÉ 04 §4.3.';
COMMENT ON COLUMN core.swim_length.duration_s IS
  'real et non integer : les temps de longueur FIT sont au centième de seconde et l''arrondi à la seconde sur 40 longueurs introduirait une dérive visible sur l''allure moyenne.';

CREATE INDEX swim_length_active_idx ON core.swim_length (activity_id, lap_index) WHERE length_type = 'active';
