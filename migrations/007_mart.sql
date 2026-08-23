-- Migration 007: vues matérialisées mart.*
-- Source : research/05-schema-postgres.md §3.6.
-- Lot L1 (garminator) -- ne pas modifier la DDL manuellement hors migration.

-- CORRECTIF APPLIQUÉ EN REVUE (seul écart assumé au texte littéral du §3.6) :
-- la vue de référence du document projette b.distance_m_swim dans la branche swim de
-- intensity_factor, alors que la CTE base ne projette aucune distance. Telle quelle, la
-- migration échoue à la création : « column b.distance_m_swim does not exist », et la chaîne
-- 001..007 n'est donc pas applicable en une passe. Le document prescrit lui-même la correction
-- dans son « avertissement d'honnêteté » (§3.6) : « elle référence b.distance_m_swim qui doit
-- être ajouté à la CTE base (a.distance_m) ». C'est exactement ce qui est fait ici : ajout de
-- a.distance_m à base, et usage de b.distance_m dans la branche swim. Aucune colonne de sortie
-- de la vue n'est modifiée.
CREATE MATERIALIZED VIEW mart.activity_load AS
WITH base AS (
    SELECT a.activity_id, a.athlete_id, a.sport_code, s.discipline,
           a.local_date, a.start_time_local, a.duration_s, a.swim_time_s, a.distance_m,
           a.np_watts, a.np_reliable, a.ngp_speed_mps, a.data_quality,
           a.imputed_seconds, a.needs_manual_rpe,
           th.threshold_id, th.ftp_watts, th.rftp_watts, th.css_m_s,
           th.ftp_pace_s_per_km, th.lthr_bpm
      FROM core.activity a
      JOIN core.sport s ON s.sport_code = a.sport_code
      LEFT JOIN core.threshold th
             ON th.athlete_id = a.athlete_id
            AND th.discipline = s.discipline
            AND th.validity @> a.local_date
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
)
SELECT b.activity_id, b.athlete_id, b.sport_code, b.discipline, b.local_date,
       b.threshold_id, b.data_quality, b.needs_manual_rpe,
       -- IF par discipline, chacun rapporté à SON seuil.
       CASE
         WHEN b.discipline = 'bike' AND b.np_watts IS NOT NULL AND b.ftp_watts  > 0
              THEN b.np_watts / b.ftp_watts
         WHEN b.discipline = 'run'  AND b.np_watts IS NOT NULL AND b.rftp_watts > 0
              THEN b.np_watts / b.rftp_watts
         WHEN b.discipline = 'swim' AND b.css_m_s > 0 AND b.swim_time_s > 0
              THEN (b.distance_m / b.swim_time_s) / b.css_m_s
       END AS intensity_factor,
       h.hr_tss,
       h.hr_seconds
  FROM base b
  LEFT JOIN hrtss h ON h.activity_id = b.activity_id;

CREATE UNIQUE INDEX activity_load_pk ON mart.activity_load (activity_id);
COMMENT ON MATERIALIZED VIEW mart.activity_load IS
  'Charge et intensité par activité, recalculées depuis les seuils EN VIGUEUR À LA DATE de chaque séance. L''index UNIQUE n''est pas décoratif : il est la condition sine qua non du REFRESH MATERIALIZED VIEW CONCURRENTLY, qui évite de verrouiller Grafana pendant le rafraîchissement. Le filtre is_parent = false empêche le double comptage des enchaînements multisport.';


CREATE MATERIALIZED VIEW mart.daily_load AS
SELECT a.athlete_id,
       a.local_date,
       sum(m.value) FILTER (WHERE lm.scale = 'tss') AS tss,
       count(*) FILTER (WHERE lm.scale = 'tss')     AS n_activities,
       sum(a.duration_s)                            AS duration_s,
       bool_or(lm.is_measured = false)              AS has_estimated_load
  FROM core.activity a
  JOIN core.activity_metric m
    ON m.activity_id = a.activity_id AND m.metric = 'tss' AND m.is_canonical
  JOIN core.load_method lm ON lm.method_code = m.method_code
 WHERE a.is_parent = false
 GROUP BY a.athlete_id, a.local_date;

CREATE UNIQUE INDEX daily_load_pk ON mart.daily_load (athlete_id, local_date);
COMMENT ON MATERIALIZED VIEW mart.daily_load IS
  'Charge journalière, base de la récursion PMC. Le FILTER sur scale = ''tss'' est une protection structurelle : il rend impossible d''additionner par mégarde un TRIMP (échelle ~170/h au seuil) à des TSS (échelle 100/h au seuil) — erreur explicitement mise en garde en 04 §5.1. has_estimated_load remonte à Grafana qu''au moins une séance du jour a une charge estimée (FC ou RPE) et non mesurée.';


CREATE MATERIALIZED VIEW mart.pmc AS
WITH RECURSIVE cal AS (
    SELECT a.athlete_id,
           generate_series(s.seed_date, current_date, interval '1 day')::date AS local_date,
           s.seed_date, s.ctl AS seed_ctl, s.atl AS seed_atl
      FROM core.athlete a
      JOIN core.pmc_seed s ON s.athlete_id = a.athlete_id
),
d AS (
    SELECT c.athlete_id, c.local_date, c.seed_date, c.seed_ctl, c.seed_atl,
           COALESCE(dl.tss, 0)::double precision AS tss,
           row_number() OVER (PARTITION BY c.athlete_id ORDER BY c.local_date) AS n
      FROM cal c
      LEFT JOIN mart.daily_load dl
             ON dl.athlete_id = c.athlete_id AND dl.local_date = c.local_date
),
rec AS (
    SELECT d.athlete_id, d.local_date, d.seed_date, d.n, d.tss,
           d.seed_ctl + (d.tss - d.seed_ctl) / 42.0 AS ctl,
           d.seed_atl + (d.tss - d.seed_atl) /  7.0 AS atl,
           d.seed_ctl AS prev_ctl, d.seed_atl AS prev_atl
      FROM d WHERE d.n = 1
    UNION ALL
    SELECT d.athlete_id, d.local_date, d.seed_date, d.n, d.tss,
           r.ctl + (d.tss - r.ctl) / 42.0,
           r.atl + (d.tss - r.atl) /  7.0,
           r.ctl, r.atl
      FROM rec r
      JOIN d ON d.athlete_id = r.athlete_id AND d.n = r.n + 1
)
SELECT athlete_id, local_date, tss,
       ctl, atl,
       prev_ctl - prev_atl AS tsb,
       ctl - atl           AS tsb_same_day,
       (local_date >= seed_date + 180) AS ctl_reliable
  FROM rec;

CREATE UNIQUE INDEX pmc_pk ON mart.pmc (athlete_id, local_date);
COMMENT ON MATERIALIZED VIEW mart.pmc IS
  'Performance Management Chart : CTL (42 j), ATL (7 j), TSB. QUATRE DÉCISIONS INSCRITES DANS CETTE VUE, toutes issues de 04. (1) Variante alpha = 1/42 et 1/7 simple, et non la forme exponentielle exacte : les deux existent dans la littérature, celle-ci est retenue et documentée (04 §6.2). (2) Calendrier DENSE par generate_series : sans lui, un jour de repos serait absent et la décroissance ne se produirait pas — le CTL serait mécaniquement surestimé (04 §6.4). (3) TSB = CTL_veille - ATL_veille, la convention de référence ; tsb_same_day est exposé EN PLUS car de nombreux outils utilisent l''autre et l''écart déroute (04 §6.3). (4) Récursion CTE et non forme close : la forme close fait intervenir (1-alpha)^-j, qui atteint ~1,3e19 à 5 ans et perd toute précision en double (04 §6.7).';
COMMENT ON COLUMN mart.pmc.ctl_reliable IS
  'false sur les 180 PREMIERS JOURS suivant le seed. VÉRIFIÉ 04 §6.5 : la valeur d''amorçage domine encore le CTL pendant plusieurs mois, et un CTL affiché en croissance sur cette période est un artefact d''initialisation, pas une progression d''entraînement. Grafana doit griser cette plage plutôt que de la masquer.';


CREATE MATERIALIZED VIEW mart.power_curve_best AS
SELECT DISTINCT ON (pc.kind, pc.duration_s)
       pc.kind, pc.duration_s, pc.value, pc.activity_id, a.local_date
  FROM core.activity_power_curve pc
  JOIN core.activity a ON a.activity_id = pc.activity_id
 WHERE NOT pc.spans_pause
 ORDER BY pc.kind, pc.duration_s, pc.value DESC;

CREATE UNIQUE INDEX power_curve_best_pk ON mart.power_curve_best (kind, duration_s);
COMMENT ON MATERIALIZED VIEW mart.power_curve_best IS
  'Courbe record tous temps, par nature de courbe. DISTINCT ON exploite directement l''index core.activity_power_curve_best_idx. La colonne activity_id répond à la question réellement posée par l''athlète : « quel jour ai-je fait ce record ? ». Les fenêtres à cheval sur une pause sont exclues (04 §8.3). Une courbe record sur période glissante se dérive de la même table en ajoutant un filtre sur local_date : elle n''a pas besoin de sa propre vue matérialisée.';


CREATE MATERIALIZED VIEW mart.hr_zone_time AS
SELECT h.activity_id, a.local_date, a.sport_code,
       z.zone_code, z.zone_order,
       sum(h.seconds) AS seconds
  FROM core.activity_hr_histogram h
  JOIN core.activity a ON a.activity_id = h.activity_id
  JOIN core.threshold th
    ON th.athlete_id = a.athlete_id AND th.discipline = 'all'
   AND th.validity @> a.local_date
  JOIN core.hr_tss_coefficient z
    ON  h.hr_bpm >= z.lo_pct_lthr * th.lthr_bpm
    AND h.hr_bpm <  z.hi_pct_lthr * th.lthr_bpm
 GROUP BY h.activity_id, a.local_date, a.sport_code, z.zone_code, z.zone_order;

CREATE UNIQUE INDEX hr_zone_time_pk ON mart.hr_zone_time (activity_id, zone_code);
COMMENT ON MATERIALIZED VIEW mart.hr_zone_time IS
  'Temps par zone de FC, DÉRIVÉ et non stocké. C''est la contrepartie visible du choix de l''histogramme à 1 bpm : corriger un LTHR ou redéfinir les bornes de zones se propage à tout l''historique par un simple REFRESH, sans réingestion d''aucune activité.';


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
       max(m.value) FILTER (WHERE m.metric = 'decoupling_pct')            AS decoupling_pct,
       max(m.value) FILTER (WHERE m.metric = 'efficiency_factor')         AS efficiency_factor
  FROM core.activity a
  JOIN core.sport sp ON sp.sport_code = a.sport_code
  LEFT JOIN core.activity_metric m ON m.activity_id = a.activity_id
 WHERE a.is_parent = false
 GROUP BY a.activity_id, sp.label_fr;

CREATE UNIQUE INDEX activity_wide_pk ON mart.activity_wide (activity_id);
COMMENT ON MATERIALIZED VIEW mart.activity_wide IS
  'Vue LARGE d''une ligne par activité, destinée aux tables Grafana. Concrétise le motif « table étroite en écriture, vue large en lecture » : core.activity_metric reste normalisé et extensible, tandis que Grafana consomme un format tabulaire sans pivot côté tableau de bord. tss_method est exposé volontairement — savoir qu''un TSS vient de la FC et non de la puissance change son interprétation.';
