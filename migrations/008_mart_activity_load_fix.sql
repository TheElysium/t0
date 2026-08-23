-- Migration 008: corrige deux défauts de mart.activity_load (migration 007, figée).
-- Aucune modification de 007 : les migrations appliquées ne se réécrivent pas.
--
-- DÉFAUT 1 -- la LTHR n'était jamais trouvée, donc hr_tss était TOUJOURS NULL.
-- La CTE `base` de 007 joint core.threshold sur `th.discipline = s.discipline` uniquement.
-- Or la LTHR est portée par la ligne `discipline = 'all'` (les zones de FC sont transversales,
-- cf. addendum §5 et le DDL de core.threshold, où lthr_bpm est NULL sur les lignes bike/run/swim).
-- Conséquence : `b.lthr_bpm` était NULL pour toute activité, le `WHERE b.lthr_bpm IS NOT NULL`
-- de la CTE hrtss ne laissait passer aucune ligne, et les colonnes hr_tss / hr_seconds étaient
-- vides pour l'historique entier. C'est mart.hr_zone_time (007, ligne 146) qui avait raison :
-- elle joint bien `th.discipline = 'all'`.
-- Correction : second LEFT JOIN sur la ligne transversale, avec COALESCE donnant la priorité à
-- une éventuelle LTHR spécifique à la discipline (physiologiquement légitime : la FC seuil vélo
-- est typiquement inférieure à la FC seuil course).
--
-- DÉFAUT 2 -- intensity_factor était TOUJOURS NULL pour la course à pied.
-- La branche `run` de 007 ne connaît que `np_watts / rftp_watts`. Or rftp_watts est NULL par
-- décision explicite (addendum §5 : l'athlète a donné une allure seuil, pas une puissance seuil ;
-- ne jamais deviner un seuil). Il n'existait aucune branche allure/GAP, alors même que le TSS
-- course canonique se calcule, lui, par la méthode `run_gap`. Résultat incohérent : un TSS juste
-- à côté d'un IF vide, pour la même séance.
-- Correction : fallback sur le GAP, rapporté à l'allure seuil.
--   vitesse seuil (m/s) = 1000 / ftp_pace_s_per_km   -- 270 s/km -> 4,201681 m/s
--   IF = ngp_speed_mps / vitesse seuil = ngp_speed_mps * ftp_pace_s_per_km / 1000
-- L'ordre des branches respecte core.metric_method_priority : run_power avant run_gap. Le jour où
-- un rFTP validé par l'athlète sera renseigné, la branche puissance reprendra la main d'elle-même.
--
-- AJOUT -- colonne intensity_factor_method.
-- Règle du projet : jamais une valeur de charge sans sa méthode. Deux IF calculés par des méthodes
-- différentes ne sont pas commensurables et ne doivent pas être agrégés en aveugle.
--
-- NON CORRIGÉ, DÉLIBÉRÉMENT -- le vélo route n'a toujours pas d'intensity_factor.
-- Sans capteur de puissance, la seule charge disponible est le hrTSS. On pourrait en déduire un IF
-- par inversion (IF = sqrt(TSS / (100 * heures))), mais ce serait un raisonnement circulaire : un
-- nombre dérivé de lui-même, présenté comme une mesure d'intensité. La colonne reste NULL, ce qui
-- est honnête ; hr_tss porte l'information de charge pour ces séances.

--
-- DÉFAUT 3 -- la cascade de méthodes était recopiée en dur dans la vue.
-- 007 discriminait sur la seule `discipline`, en ignorant core.metric_method_priority. Or cette
-- matrice n'accorde PAS les mêmes méthodes à tous les sports d'une même discipline : `treadmill`
-- n'a pas droit à `run_gap` (pas de pente GPS sur un tapis, un GAP y serait une fiction). Une
-- branche GAP appliquée à toute la discipline `run` aurait donc contredit, sur tapis, la méthode
-- canonique retenue par core.activity_metric — deux chiffres divergents pour la même séance.
-- Le COMMENT de core.metric_method_priority énonce précisément ce risque : « sans elle, la cascade
-- se recopie en CASE WHEN dans chaque vue Grafana — non auditable, non testable, divergente dès la
-- deuxième copie. » Correction : les branches sont gardées par appartenance à la matrice, qui
-- redevient la source unique de vérité, exactement comme du côté Python (resolve_canonical).

DROP MATERIALIZED VIEW mart.activity_load;

CREATE MATERIALIZED VIEW mart.activity_load AS
WITH allowed AS (
    -- Défaut 3 : la cascade de méthodes ne se recopie pas en CASE WHEN, elle se LIT.
    SELECT p.sport_code, array_agg(p.method_code) AS methods
      FROM core.metric_method_priority p
     WHERE p.metric = 'tss'
     GROUP BY p.sport_code
),
base AS (
    SELECT a.activity_id, a.athlete_id, a.sport_code, s.discipline,
           a.local_date, a.start_time_local, a.duration_s, a.swim_time_s, a.distance_m,
           a.np_watts, a.np_reliable, a.ngp_speed_mps, a.data_quality,
           a.imputed_seconds, a.needs_manual_rpe,
           th.threshold_id, th.ftp_watts, th.rftp_watts, th.css_m_s,
           th.ftp_pace_s_per_km,
           -- Défaut 1 : la LTHR vit sur la ligne transversale ; la ligne de discipline
           -- reste prioritaire si elle en porte une un jour.
           coalesce(th.lthr_bpm, th_all.lthr_bpm) AS lthr_bpm,
           coalesce(al.methods, ARRAY[]::text[])  AS allowed_methods
      FROM core.activity a
      JOIN core.sport s ON s.sport_code = a.sport_code
      LEFT JOIN allowed al ON al.sport_code = a.sport_code
      LEFT JOIN core.threshold th
             ON th.athlete_id = a.athlete_id
            AND th.discipline = s.discipline
            AND th.validity @> a.local_date
      LEFT JOIN core.threshold th_all
             ON th_all.athlete_id = a.athlete_id
            AND th_all.discipline = 'all'
            AND th_all.validity @> a.local_date
     WHERE a.is_parent = false          -- ne jamais compter le parent multisport
),
-- hrTSS : produit scalaire histogramme x coefficients de zone.
hrtss AS (
    SELECT h.activity_id,
           sum(h.seconds / 3600.0 * z.tss_per_hour) AS hr_tss,
           sum(h.seconds)                           AS hr_seconds
      FROM core.activity_hr_histogram h
      JOIN base b ON b.activity_id = h.activity_id
      JOIN core.hr_tss_coefficient z
        ON  h.hr_bpm >= z.lo_pct_lthr * b.lthr_bpm
        AND h.hr_bpm <  z.hi_pct_lthr * b.lthr_bpm
     WHERE b.lthr_bpm IS NOT NULL
     GROUP BY h.activity_id
),
-- La méthode est résolue UNE FOIS ; la valeur en découle. Deux CASE parallèles auraient pu
-- diverger à la première retouche : ici c'est structurellement impossible.
resolved AS (
    SELECT b.*,
           CASE
             WHEN 'bike_power' = ANY(b.allowed_methods)
                  AND b.np_watts IS NOT NULL AND b.ftp_watts  > 0  THEN 'bike_power'
             WHEN 'run_power'  = ANY(b.allowed_methods)
                  AND b.np_watts IS NOT NULL AND b.rftp_watts > 0  THEN 'run_power'
             -- Défaut 2 : fallback allure/GAP, seule méthode disponible tant que rFTP est NULL.
             WHEN 'run_gap'    = ANY(b.allowed_methods)
                  AND b.ngp_speed_mps IS NOT NULL
                  AND b.ftp_pace_s_per_km > 0                      THEN 'run_gap'
             WHEN 'swim_css'   = ANY(b.allowed_methods)
                  AND b.css_m_s > 0 AND b.swim_time_s > 0          THEN 'swim_css'
           END AS intensity_factor_method
      FROM base b
)
SELECT r.activity_id, r.athlete_id, r.sport_code, r.discipline, r.local_date,
       r.threshold_id, r.data_quality, r.needs_manual_rpe,
       -- IF par méthode, chacun rapporté à SON seuil.
       CASE r.intensity_factor_method
         WHEN 'bike_power' THEN r.np_watts / r.ftp_watts
         WHEN 'run_power'  THEN r.np_watts / r.rftp_watts
         -- vitesse seuil (m/s) = 1000 / ftp_pace_s_per_km ; IF = ngp / vitesse seuil.
         WHEN 'run_gap'    THEN r.ngp_speed_mps * r.ftp_pace_s_per_km / 1000.0
         WHEN 'swim_css'   THEN (r.distance_m / r.swim_time_s) / r.css_m_s
       END AS intensity_factor,
       r.intensity_factor_method,
       h.hr_tss,
       h.hr_seconds
  FROM resolved r
  LEFT JOIN hrtss h ON h.activity_id = r.activity_id;

CREATE UNIQUE INDEX activity_load_pk ON mart.activity_load (activity_id);
COMMENT ON MATERIALIZED VIEW mart.activity_load IS
  'Charge et intensité par activité, recalculées depuis les seuils EN VIGUEUR À LA DATE de chaque séance. L''index UNIQUE n''est pas décoratif : il est la condition sine qua non du REFRESH MATERIALIZED VIEW CONCURRENTLY, qui évite de verrouiller Grafana pendant le rafraîchissement. Le filtre is_parent = false empêche le double comptage des enchaînements multisport. Migration 008 : la LTHR est désormais lue sur la ligne de seuil transversale (discipline = ''all''), sans quoi hr_tss restait NULL pour tout l''historique ; et l''intensity_factor de la course retombe sur le GAP quand rftp_watts est NULL, ce qui est le cas par décision explicite.';
COMMENT ON COLUMN mart.activity_load.intensity_factor_method IS
  'Méthode ayant produit intensity_factor, vocabulaire aligné sur core.load_method. NULL quand aucune méthode n''est applicable — notamment le vélo route sans capteur de puissance, où l''IF n''est volontairement pas inversé depuis le hrTSS (ce serait un nombre dérivé de lui-même).';
