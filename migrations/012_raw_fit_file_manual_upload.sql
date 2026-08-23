-- Migration 012 : rend raw.fit_file.garmin_activity_id NULLABLE pour l'envoi manuel (lot L7).
--
-- LE PROBLEME. src/garminator/api/ingest.py (endpoint d'envoi manuel) archive un .FIT/.TCX
-- sur le PVC en respectant la regle d'atomicite du commentaire de raw.fit_file, mais
-- n'inserait jusqu'ici aucune ligne : un envoi manuel n'a par construction aucun
-- garmin_activity_id (il ne vient pas de l'API Garmin), et la colonne etait NOT NULL
-- depuis 002. Le fichier restait donc archive mais hors de la file de transformation --
-- jamais exploite. Aucune modification de 002_raw.sql : les migrations appliquees ne se
-- reecrivent pas (le runner verifie leur checksum, voir src/garminator/db/runner.py).
--
-- CE QUI DISTINGUE DESORMAIS UNE LIGNE « GARMIN » D'UNE LIGNE « ENVOI MANUEL ».
-- Decision : NULL, et rien d'autre -- pas de colonne booleenne ou enum de provenance
-- ajoutee. Deux raisons :
--
--   1. Une colonne de provenance dupliquerait exactement ce que garmin_activity_id
--      IS NULL dit deja sans ambiguite : une ligne qui a un identifiant Garmin VIENT de
--      Garmin, une ligne qui n'en a pas ne peut venir que d'un envoi manuel (l'API
--      Garmin ne produit jamais de ligne sans activityId). Deux colonnes qui doivent
--      rester d'accord sont un defaut a occasion permanente ; NULL ne peut pas diverger
--      de lui-meme.
--   2. Cette convention n'est pas une invention de cette migration : elle est deja celle
--      de src/garminator/ops/reconcile.py (MissingFile.garmin_activity_id: int | None,
--      et format_reconcile_report qui affiche explicitement "upload manuel" quand la
--      valeur est NULL) et de src/garminator/transform/runner.py (FileOutcome expose
--      garmin_activity_id pour le rapport humain). Le code Python typait deja cette
--      colonne comme optionnelle avant que le DDL ne le permette ; cette migration
--      aligne enfin le schema sur un choix deja pris ailleurs, plutot que d'en inventer
--      un second concurrent.
--
-- IMPACT SUR LES CONTRAINTES EXISTANTES -- verifie explicitement, pas suppose.
--
--   - fit_file_sha_uq UNIQUE (sha256) : sha256 reste bytea NOT NULL, INCHANGE par cette
--     migration. C'est la seule contrainte UNIQUE que porte raw.fit_file, et elle ne
--     porte pas sur une colonne qui devient nullable ici -- le piege classique (« un
--     UNIQUE sur une colonne nullable laisse passer plusieurs NULL ») NE S'APPLIQUE PAS
--     A ELLE. Consequence directe et voulue : DEUX envois manuels de contenus distincts
--     obtiennent chacun leur ligne (sha256 different) ; DEUX envois manuels du MEME
--     contenu binaire (meme sha256, les deux avec garmin_activity_id NULL) restent
--     detectes comme un seul et meme fichier deja archive, exactement comme un
--     retelechargement Garmin identique -- l'idempotence par contenu ne depend jamais de
--     la presence ou non d'un garmin_activity_id. C'est garminator.garmin.raw_store
--     .insert_fit_file (relecture apres ON CONFLICT (sha256) DO NOTHING) qui porte cette
--     logique applicative ; corrige dans le meme lot pour ne plus supposer a tort
--     l'existing_activity_id relu non-NULL.
--   - fit_file_activity_idx (garmin_activity_id, downloaded_at DESC) : simple index
--     B-tree, pas UNIQUE -- accepte nativement des NULL entrelaces (PostgreSQL les place
--     ensemble, tries en dernier par defaut) sans changement de comportement ni
--     reconstruction necessaire.
--   - fit_file_unparsed_idx (downloaded_at) WHERE parsed_at IS NULL : ne reference pas
--     garmin_activity_id, non concernee.
--
-- RISQUE CONNU, VOLONTAIREMENT NON CORRIGE ICI (hors perimetre de ce lot -- src/garminator
-- /transform/runner.py et src/garminator/ops/reconcile.py sont hors des fichiers autorises
-- pour ce lot ; voir le rapport de fin de lot pour le detail complet). La ligne inseree par
-- src/garminator/api/ingest.py est une ligne VRAIMENT en attente (garmin_activity_id NULL,
-- parsed_at NULL, parse_error NULL, exactement comme une ligne Garmin fraichement
-- telechargee) : c'est necessaire pour que le fichier entre reellement dans la file de
-- transformation, qui etait exactement le probleme signale pour ce lot. Mais
-- src/garminator/transform/runner.py:79-92 selectionne cette colonne et la convertit sans
-- garde par int(r[1]) a la ligne 182 -- un NULL y leve TypeError et fait echouer TOUT le
-- passage de transformation (CronJob nocturne complet), pas seulement le fichier manuel
-- concerne. Ce risque est reel et immediat des le premier envoi manuel ; sa correction
-- (filtrer ou traiter differemment les lignes sans garmin_activity_id cote runner) revient
-- a un lot ulterieur qui a le droit de modifier ce fichier.

ALTER TABLE raw.fit_file ALTER COLUMN garmin_activity_id DROP NOT NULL;

-- Index dedie aux envois manuels : partiel comme fit_file_unparsed_idx (meme motif deja
-- en usage dans 002), utile a un futur outil d'inventaire ("quels fichiers sont entres
-- par la porte manuelle plutot que par Garmin ?") sans avoir a scanner toute la table.
CREATE INDEX fit_file_manual_upload_idx
    ON raw.fit_file (downloaded_at DESC)
    WHERE garmin_activity_id IS NULL;

COMMENT ON COLUMN raw.fit_file.garmin_activity_id IS
  'Identifiant d''activite Garmin, NULL pour un envoi manuel (endpoint garminator.api.ingest, lot L7) faute d''identifiant Garmin par construction. NULL est le SEUL marqueur de provenance : une ligne sans garmin_activity_id ne peut venir que d''un envoi manuel, l''API Garmin n''en produisant jamais sans activityId. Convention deja utilisee par garminator.ops.reconcile (affiche "upload manuel") et garminator.transform.runner avant meme que cette colonne ne soit devenue nullable ici (migration 012). RISQUE CONNU documente en 012 et NON corrige (hors perimetre du lot qui a introduit la nullabilite) : le selecteur de file d''attente de garminator.transform.runner (garminator/transform/runner.py:79-92, conversion int() ligne 182) plante sur une ligne portant NULL ici tant qu''elle reste "en attente" (parsed_at IS NULL) -- ce qui est pourtant l''etat normal d''une ligne d''envoi manuel fraichement inseree, puisque c''est precisement cet etat qui la fait entrer dans la file de transformation. A corriger cote runner par un lot habilite a modifier ce fichier.';

COMMENT ON TABLE raw.fit_file IS
  'Metadonnees des fichiers .FIT/.TCX archives sur le PVC, qu''ils viennent du telechargement automatique Garmin OU d''un envoi manuel (endpoint garminator.api.ingest, lot L7 ; migration 012 a rendu garmin_activity_id nullable pour ce second cas -- NULL = envoi manuel, voir le commentaire de cette colonne). Le binaire N''EST PAS en base (pas de bytea) : plusieurs Mo par fichier gonfleraient TOAST et chaque dump PostgreSQL sans rien apporter de requetable. REGLE D''ATOMICITE IMPERATIVE, valable pour les deux provenances : le fichier est ecrit ET fsync sur le PVC AVANT l''INSERT de cette ligne, jamais l''inverse. Un fichier orphelin (sans ligne) est inoffensif et rattrapable par un scan (`python -m garminator.ops reconcile`, voir docs/RUNBOOK.md) ; une ligne orpheline (sans fichier) rend la reconstructibilite de core fausse. Corollaire de sauvegarde : le dump PostgreSQL et le snapshot du PVC doivent etre pris au MEME point dans le temps (cf. §6.5).';
