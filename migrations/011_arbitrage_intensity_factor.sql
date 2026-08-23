-- Migration 011 : arbitrage des DEUX definitions concurrentes d'intensity_factor.
--
-- LE PROBLEME. Depuis la migration 008, le meme nombre est produit a deux endroits :
--
--   (A) mart.activity_load le RECALCULE en SQL, avec sa propre resolution de methode et
--       sa colonne intensity_factor_method ;
--   (B) core.activity_metric le STOCKE, ecrit par garminator.transform.activity, et
--       mart.activity_wide le lit de la (colonne « IF » du tableau de bord charge.json).
--
-- Deux nombres, deux chemins de calcul, aucune contrainte pour les tenir d'accord. Rien
-- n'empechait deux panneaux voisins d'afficher deux IF differents pour la meme seance,
-- sans qu'aucun des deux ne soit signale comme faux.
--
-- L'ARBITRAGE, ET POURQUOI. C'est (B) qui gagne, pour une raison factuelle et non
-- esthetique : (A) N'EST LU PAR PERSONNE. Verifie exhaustivement -- aucun tableau de bord
-- n'interroge mart.activity_load (charge.json lit activity_wide et daily_load, pmc.json
-- lit pmc, puissance.json power_curve_best, zones.json hr_zone_time), et aucune autre vue
-- ne la joint (la seule jointure entre vues du schema est pmc -> daily_load). Toute la
-- chaine reellement branchee -- daily_load, puis pmc, puis activity_wide -- part de
-- core.activity_metric. mart.activity_load est une seconde implementation, complete et
-- soignee, mais orpheline.
--
-- Le cout n'etait pas nul pour autant : ops-refresh la rafraichit a chaque passage, en
-- tete de MART_VIEWS, pour zero lecteur.
--
-- CE QUI EST FAIT ICI, ET CE QUI NE L'EST PAS. La vue n'est pas supprimee. Elle recalcule
-- l'IF depuis les seuils EN VIGUEUR A LA DATE de chaque seance, ce que le chemin stocke ne
-- refait pas tout seul : corriger a posteriori une FTP erronee se propage a tout
-- l'historique par un simple REFRESH ici, alors que les lignes de core.activity_metric
-- restent sur l'ancienne valeur jusqu'a une retransformation. C'est exactement le principe
-- deja revendique par le commentaire de mart.hr_zone_time. Cette vue vaut donc comme
-- CONTRE-EXPERTISE, et c'est ce role que le commentaire ci-dessous fixe -- pour qu'on
-- cesse de la prendre pour un chemin de lecture, et qu'on cesse aussi de la « corriger »
-- en croyant reparer le tableau de bord.
--
-- Manquait par ailleurs a activity_wide le pendant de tss_method : l'IF y etait affiche
-- sans dire de quelle methode il sortait. Or « IF 0,82 » n'a pas le meme sens selon qu'il
-- vient de la puissance ou d'un GAP -- d'autant que la rFTP course est NULL par decision
-- explicite, donc que toute la course passe par le GAP.

-- Une vue materialisee ne se REPLACE pas : il faut la reconstruire. Sans danger ici,
-- aucune autre vue ne lit activity_wide (verifie ci-dessus).
DROP MATERIALIZED VIEW IF EXISTS mart.activity_wide;

CREATE MATERIALIZED VIEW mart.activity_wide AS
SELECT a.activity_id, a.local_date, a.start_time_local, a.sport_code,
       sp.label_fr AS sport, a.name, a.duration_s, a.distance_m,
       a.avg_hr, a.max_hr, a.avg_power, a.np_watts, a.np_reliable,
       a.ascent_m, a.data_quality, a.needs_manual_rpe,
       max(m.value) FILTER (WHERE m.metric = 'tss'  AND m.is_canonical) AS tss,
       max(m.method_code) FILTER (WHERE m.metric = 'tss' AND m.is_canonical) AS tss_method,
       max(m.value) FILTER (WHERE m.metric = 'tss'  AND m.method_code = 'hr_hrtss') AS tss_hr,
       max(m.value) FILTER (WHERE m.metric = 'trimp')                     AS trimp,
       max(m.value) FILTER (WHERE m.metric = 'intensity_factor')          AS intensity_factor,
       -- AJOUT 011 : le pendant de tss_method. Meme raison, meme necessite.
       max(m.method_code) FILTER (WHERE m.metric = 'intensity_factor')    AS intensity_factor_method,
       max(m.value) FILTER (WHERE m.metric = 'decoupling_pct')            AS decoupling_pct,
       max(m.value) FILTER (WHERE m.metric = 'efficiency_factor')         AS efficiency_factor
  FROM core.activity a
  JOIN core.sport sp ON sp.sport_code = a.sport_code
  LEFT JOIN core.activity_metric m ON m.activity_id = a.activity_id
 WHERE a.is_parent = false
 GROUP BY a.activity_id, sp.label_fr;

-- Index UNIQUE recree a l'identique : sans lui, pas de REFRESH ... CONCURRENTLY, donc
-- Grafana verrouille pendant chaque rafraichissement.
CREATE UNIQUE INDEX activity_wide_pk ON mart.activity_wide (activity_id);

COMMENT ON MATERIALIZED VIEW mart.activity_wide IS
  'Vue LARGE d''une ligne par activite, destinee aux tableaux de bord Grafana, et CHEMIN DE LECTURE FAISANT AUTORITE pour les metriques par seance. Concretise le motif « table etroite en ecriture, vue large en lecture » : core.activity_metric reste normalise et extensible, tandis que Grafana consomme un format tabulaire sans pivot cote tableau de bord. Les colonnes _method sont exposees volontairement -- savoir qu''un TSS vient de la FC et non de la puissance, ou qu''un IF vient du GAP et non d''une puissance seuil, change son interpretation. Migration 011 : intensity_factor_method ajoute, et arbitrage tranche en faveur de ce chemin-ci contre le recalcul SQL de mart.activity_load.';

COMMENT ON COLUMN mart.activity_wide.intensity_factor_method IS
  'Methode ayant produit intensity_factor, vocabulaire aligne sur core.load_method. NULL quand aucune methode n''est applicable -- notamment le velo route sans capteur de puissance, ou l''IF n''est volontairement pas inverse depuis le hrTSS (ce serait un nombre derive de lui-meme). Pour la course, vaut le GAP tant que rftp_watts est NULL, ce qui est le cas par decision explicite de l''athlete.';

COMMENT ON MATERIALIZED VIEW mart.activity_load IS
  'CONTRE-EXPERTISE, PAS UN CHEMIN DE LECTURE. Recalcule charge et intensite depuis les seuils EN VIGUEUR A LA DATE de chaque seance, la ou mart.activity_wide expose les valeurs STOCKEES dans core.activity_metric au moment de la transformation. Migration 011 : aucun tableau de bord ni aucune vue ne lit celle-ci, et l''arbitrage a tranche en faveur du chemin stocke -- ne branchez pas un panneau dessus sans relire 011. Son interet est la ou les deux divergent : un ecart entre son intensity_factor et celui de mart.activity_wide signifie qu''un seuil a ete corrige APRES l''ingestion et que les lignes core.activity_metric concernees sont a retransformer. C''est un outil de detection de valeurs perimees, et le seul endroit ou cette derive soit visible. Le filtre is_parent = false empeche le double comptage des enchainements multisport ; l''index UNIQUE conditionne le REFRESH CONCURRENTLY.';
