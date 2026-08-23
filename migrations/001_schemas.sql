-- Migration 001: extensions et schémas raw / core / mart
-- Source : research/05-schema-postgres.md §3, transcrit tel quel (DDL figé).
-- Lot L1 (garminator) -- ne pas modifier la DDL manuellement hors migration.

-- btree_gist : indispensable pour la contrainte d'exclusion de recouvrement
-- sur les périodes de validité des seuils (core.threshold).
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;

COMMENT ON SCHEMA raw  IS
  'Couche brute, append-only, jamais modifiée : réponses JSON de l''API Garmin et métadonnées des fichiers .FIT. Vérité de dernier recours : core doit pouvoir être entièrement reconstruit depuis raw sans re-solliciter Garmin.';
COMMENT ON SCHEMA core IS
  'Couche typée, 100% dérivée de raw. TRUNCATE-able et reconstructible par rejeu des transformations.';
COMMENT ON SCHEMA mart IS
  'Couche d''exposition : vues matérialisées rafraîchies après chaque ingestion, consommées par Grafana. Aucune écriture applicative.';
