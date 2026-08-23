-- ============================================================================================
-- FIXTURE -- SEUILS RÉELS DE L'ATHLÈTE, FOURNIS LE 2026-08-19. À CHARGER MANUELLEMENT.
-- ============================================================================================
--
-- Ce fichier N'EST PAS une migration : il n'est ni nommé "NNN_....sql", ni placé dans
-- migrations/, et garminator.db.runner ne le découvre ni ne l'applique jamais
-- automatiquement (discover_migrations() ignore tout fichier hors de ce motif de nom).
--
-- Pourquoi ce fichier existe malgré tout hors migration : core.threshold n'est structurellement
-- PAS un référentiel figé comme core.sport -- les seuils changent dans le temps, avec historique
-- via la plage de validité (colonne validity, contrainte d'exclusion GiST threshold_no_overlap).
-- Semer une valeur de seuil comme si c'était une donnée de référence stable serait donc trompeur ;
-- un nouveau seuil doit toujours arriver comme un INSERT explicite, daté, jamais un UPDATE.
--
-- Origine des valeurs : communiquées directement par l'athlète le 2026-08-19 (et non plus des
-- valeurs inventées -- la version précédente de ce fichier contenait des valeurs FICTIVES en
-- attendant cette communication ; elle est désormais remplacée).
--
-- À exécuter manuellement, jamais via `python -m garminator.db migrate` :
--   psql "$GARMINATOR_TEST_POSTGRES_DSN" -f tests/db/fixtures/thresholds_example.sql
--
-- Idempotent : rejouer ce fichier ne crée pas de doublon. Attention, l'idempotence n'est PAS
-- obtenue de la même façon partout, et c'est délibéré :
--   * core.threshold : `ON CONFLICT DO NOTHING` sans cible. PostgreSQL traite ainsi également les
--     violations de contrainte d'EXCLUSION, donc threshold_no_overlap sert d'arbitre : un second
--     passage voit un chevauchement de `validity` sur (athlete_id, discipline) et ne fait rien.
--   * core.athlete : `WHERE NOT EXISTS`. `ON CONFLICT DO NOTHING` y serait un PIÈGE — la table n'a
--     AUCUNE contrainte unique ni d'exclusion dans le DDL figé (seule athlete_sex_ck, un CHECK),
--     la clause ne peut donc jamais se déclencher et chaque exécution insérerait un athlète de
--     plus. Les INSERT de seuils, qui résolvent athlete_id par `WHERE display_name = ...`,
--     tomberaient alors sur plusieurs lignes et créeraient un seuil par doublon.
--
-- Si l'athlète change de seuils, la bonne pratique est un
-- nouvel INSERT avec une nouvelle plage `validity` (borne basse = date du nouveau test), jamais un
-- UPDATE de cette ligne -- l'historique des seuils doit rester lisible, cf. 04 §2.3 : « Le TSS
-- d'une activité utilise le seuil en vigueur à sa date. Sinon une mise à jour de FTP réécrirait
-- rétroactivement tout l'historique de CTL. »
--
-- POINT NON NÉGOCIABLE -- rFTP (seuil de puissance course) laissé à NULL volontairement :
-- l'athlète a communiqué une ALLURE seuil (4:30 min/km), pas une PUISSANCE seuil. Les deux
-- échelles ne sont pas interchangeables (Running Power Garmin = modèle propriétaire non
-- documenté, VÉRIFIÉ 04 §3.2) et aucune conversion allure -> puissance fiable n'existe sans
-- mesure indépendante. rftp_watts reste donc NULL ici : ni valeur par défaut, ni dérivation
-- depuis le FTP vélo, ni estimation. Un seuil faux ne produit pas d'erreur visible : il produit
-- des nombres plausibles et faux sur tout l'historique de charge. Toute méthode de calcul de
-- charge qui dépend de rFTP (Running Power) doit donc rester INDISPONIBLE pour cet athlète tant
-- qu'aucune mesure de puissance course fiable n'est fournie -- c'est le rôle de
-- core.metric_method_priority (repli vers la méthode suivante, ex. HR-TRIMP) plutôt que d'une
-- valeur de seuil fabriquée ici.
--
-- Taille (1,82 m) et âge (30 ans) : communiqués mais non stockés -- core.threshold /
-- core.athlete n'ont pas de colonne dédiée dans le DDL figé (research/05-schema-postgres.md).
-- Non ajoutés ici : ce fichier ne redéfinit pas le schéma, hors périmètre du lot L1. Signalé au
-- rapport de fin de lot.
-- ============================================================================================

INSERT INTO core.athlete (display_name, sex, home_timezone)
SELECT 'Athlète principal', 'male', 'Europe/Paris'
 WHERE NOT EXISTS (
     SELECT 1 FROM core.athlete WHERE display_name = 'Athlète principal'
 );

-- Vélo route (home-trainer uniquement, cf. profil athlète) : FTP réelle 220 W.
-- LTHR (seuil FC, distinct de FC max/repos) non communiqué -> laissé à NULL, non dérivé.
INSERT INTO core.threshold (
    athlete_id, discipline, validity,
    ftp_watts, hr_max_bpm, hr_rest_bpm, weight_kg,
    power_source, source, note
)
SELECT a.athlete_id, 'bike', daterange('2026-05-19', NULL, '[)'),
       220, 190, 55, 70.0,
       'home_trainer', 'manual',
       'Valeurs réelles communiquées par l''athlète le 2026-08-19. LTHR vélo non communiqué (NULL).'
  FROM core.athlete a
 WHERE a.display_name = 'Athlète principal'
ON CONFLICT DO NOTHING;

-- Course à pied : allure seuil réelle 4:30 min/km = 270 s/km (colonne en s/km, pas en m/s).
-- rftp_watts (seuil de puissance course) DÉLIBÉRÉMENT NULL -- voir avertissement en tête de
-- fichier : une allure n'est pas une puissance, aucune conversion fiable n'est appliquée.
INSERT INTO core.threshold (
    athlete_id, discipline, validity,
    ftp_pace_s_per_km, rftp_watts, hr_max_bpm, hr_rest_bpm, weight_kg,
    source, note
)
SELECT a.athlete_id, 'run', daterange('2026-05-19', NULL, '[)'),
       270, NULL, 190, 55, 70.0,
       'manual',
       'Valeurs réelles communiquées par l''athlète le 2026-08-19. rFTP NULL volontairement : '
       'seule une allure seuil (4:30 min/km) a été fournie, pas une puissance course. Ne pas '
       'dériver de valeur ici.'
  FROM core.athlete a
 WHERE a.display_name = 'Athlète principal'
ON CONFLICT DO NOTHING;

-- Natation : CSS réelle 1:50 min/100 m = 110 s/100 m = 1,000000 m/s.
-- css_estimated = false : valeur communiquée directement par l'athlète, pas issue du repli
-- grossier "1000 m CLM x 0.97" (04 §4.1) -- méthode d'obtention non précisée mais traitée comme
-- mesurée puisqu'aucune indication contraire n'a été donnée.
INSERT INTO core.threshold (
    athlete_id, discipline, validity,
    css_m_s, css_estimated, hr_max_bpm, hr_rest_bpm, weight_kg,
    source, note
)
SELECT a.athlete_id, 'swim', daterange('2026-05-19', NULL, '[)'),
       0.909091, false, 190, 55, 70.0,
       'manual',
       'Valeurs réelles communiquées par l''athlète le 2026-08-19 (CSS 1:50/100m = 1,000000 m/s).'
  FROM core.athlete a
 WHERE a.display_name = 'Athlète principal'
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------------------------
-- RÉTRODATAGE ASSUMÉ : borne basse de validité au 2026-05-19, soit TROIS MOIS avant la date de
-- communication réelle des seuils (2026-08-19).
--
-- Ce n'est PAS une mesure : c'est une hypothèse explicite de l'athlète, décidée le 2026-08-21,
-- selon laquelle ses seuils étaient sensiblement les mêmes sur le trimestre écoulé. Elle a été
-- préférée à un rétrodatage complet jusqu'au début du backfill (2025-04-03), jugé trop peu fiable.
--
-- Conséquence à connaître : les activités antérieures au 2026-05-19 n'ont AUCUN seuil en vigueur,
-- donc aucun TSS, donc aucune contribution à la CTL/ATL/TSB. La PMC démarre à zéro le 2026-05-19
-- et met environ six semaines à devenir représentative (la CTL est une moyenne exponentielle
-- récursive sur 42 jours). Toute progression affichée avant début juillet 2026 est un artefact
-- de démarrage, pas un gain de forme.
--
-- Si des seuils plus anciens sont retrouvés, la bonne correction n'est PAS d'élargir cette
-- période : c'est de la DÉCOUPER en plusieurs lignes de validité disjointes. La contrainte
-- d'exclusion GiST threshold_no_overlap l'impose de toute façon.
-- ---------------------------------------------------------------------------------------------

-- Transversal ('all') : FC seuil réelle 170 bpm, communiquée par l'athlète le 2026-08-21.
-- Cette ligne est la CONDITION D'EXISTENCE de mart.hr_zone_time et du hrTSS : les zones de FC
-- et le TSS par fréquence cardiaque se joignent sur discipline = 'all'. Sans elle, ces deux
-- sorties restent définitivement vides.
-- Elle compte particulièrement pour le VÉLO SUR ROUTE, qui n'a aucun capteur de puissance
-- (addendum §3) : la FC y est la seule voie possible vers une charge d'entraînement.
-- Aucune contrainte par discipline (threshold_bike_only / _run_only / _swim_only) ne s'applique
-- à 'all' : elles ne visent que les trois disciplines nommées. Les colonnes de puissance et de
-- CSS restent néanmoins NULL ici -- une charge transversale n'a pas de seuil de puissance.
-- 170/190 = 90,8 % de la FC max : cohérent, mais NON VÉRIFIÉ par le protocole (moyenne des
-- 20 dernières minutes d'un test maximal). Valeur prise telle que communiquée.
INSERT INTO core.threshold (
    athlete_id, discipline, validity,
    lthr_bpm, hr_max_bpm, hr_rest_bpm, weight_kg,
    source, note
)
SELECT a.athlete_id, 'all', daterange('2026-05-19', NULL, '[)'),
       170, 190, 55, 70.0,
       'manual',
       'FC seuil (LTHR) 170 bpm communiquée par l''athlète le 2026-08-21. Portée par la ligne '
       '''all'' car les zones de FC et le hrTSS sont transversaux aux trois disciplines. '
       'Validité rétrodatée au 2026-05-19 comme les trois autres seuils -- voir l''avertissement '
       'en tête de fichier.'
  FROM core.athlete a
 WHERE a.display_name = 'Athlète principal'
ON CONFLICT DO NOTHING;
