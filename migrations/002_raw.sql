-- Migration 002: schéma raw (append-only, source de vérité)
-- Source : research/05-schema-postgres.md §3, transcrit tel quel (DDL figé).
-- Lot L1 (garminator) -- ne pas modifier la DDL manuellement hors migration.

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
  'Réponse brute d''un appel à l''API Garmin (client garminconnect 0.3.2). APPEND-ONLY : aucune ligne n''est jamais modifiée ni supprimée. Chaque re-fetch d''une même journée crée une nouvelle ligne, ce qui rend l''historique des republications de Garmin (recalcul du sommeil, wellness/epoch/request) observable et auditable. Volontairement SANS contrainte UNIQUE : une unicité incluant payload_sha256 mais pas fetched_at empêcherait la conversion en hypertable. La déduplication est déportée dans raw.payload_seen.';
COMMENT ON COLUMN raw.api_response.endpoint IS
  'Nom de la méthode garminconnect appelée, ex. get_sleep_data, get_activity, get_stress_data. Sert de discriminant de parsing dans core.';
COMMENT ON COLUMN raw.api_response.scope_date IS
  'Date LOCALE de l''athlète à laquelle la réponse se rapporte (paramètre cdate des 36 méthodes journalières). NULL pour les endpoints non journaliers. Ce n''est pas une date UTC : les endpoints wellness Garmin sont nativement clés par calendarDate locale.';
COMMENT ON COLUMN raw.api_response.scope_key IS
  'Discriminant secondaire pour les endpoints non journaliers, ex. l''activityId pour get_activity. NULL sinon.';
COMMENT ON COLUMN raw.api_response.payload_sha256 IS
  'sha256 du payload jsonb sérialisé de façon canonique (clés triées, séparateurs fixes). Base de la détection de changement rétroactif (cf. §6).';

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
  'Sentinelle de déduplication, volontairement séparée de raw.api_response. Répond à la question « ce contenu exact a-t-il déjà été vu pour ce (endpoint, jour) ? » sans imposer de contrainte UNIQUE à raw.api_response — contrainte qui, n''incluant pas la colonne temporelle, bloquerait la conversion en hypertable. Table minuscule (une ligne par contenu distinct), entièrement en cache. Un INSERT ... ON CONFLICT DO UPDATE incrémentant seen_count sert à la fois de test « déjà vu » et de compteur de republications.';


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
  'Métadonnées des fichiers .FIT archivés sur le PVC. Le binaire N''EST PAS en base (pas de bytea) : ~1 090 fichiers de plusieurs Mo gonfleraient TOAST et chaque dump PostgreSQL sans rien apporter de requêtable. RÈGLE D''ATOMICITÉ IMPÉRATIVE : le fichier est écrit ET fsync sur le PVC AVANT l''INSERT de cette ligne, jamais l''inverse. Un fichier orphelin (sans ligne) est inoffensif et rattrapable par un scan ; une ligne orpheline (sans fichier) rend la reconstructibilité de core fausse. Corollaire de sauvegarde : le dump PostgreSQL et le snapshot du PVC doivent être pris au MÊME point dans le temps (cf. §6.5).';
COMMENT ON COLUMN raw.fit_file.sha256 IS
  'sha256 du binaire .FIT. UNIQUE : un même contenu n''est archivé qu''une fois. C''est le pivot de la détection de re-téléchargement effectif (§6.2). Distinct du hash du résumé JSON (§6.3).';
COMMENT ON COLUMN raw.fit_file.parser_version IS
  'Version du parseur (garmin-fit-sdk 21.213.0 + version du code garminator) ayant produit les lignes core. Permet de rejouer sélectivement les fichiers parsés par une version buguée.';

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
  'Journal d''exécution de l''ingestion. Permet de répondre à « quelle exécution a produit cette ligne ? » et de rejouer/annuler un lot. Le mode reprocess correspond au FORCE_REPROCESS_ACTIVITIES du projet de référence (VÉRIFIÉ 01 §3).';
