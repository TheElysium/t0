-- Migration 001: extensions and raw / core / mart schemas
-- Source: research/05-postgres-schema.md section 3, transcribed as-is (frozen DDL).
-- Batch L1 (garminator) -- do not modify the DDL manually outside migration.

-- btree_gist: required for the exclusion constraint on overlapping
-- validity periods of thresholds (core.threshold).
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;

COMMENT ON SCHEMA raw  IS
  'Raw layer, append-only, never modified: JSON responses from Garmin API and metadata of .FIT files. Source of last resort: core must be fully reconstructible from raw without re-requesting Garmin.';
COMMENT ON SCHEMA core IS
  'Typed layer, 100% derived from raw. TRUNCATE-able and reconstructible by replaying transformations.';
COMMENT ON SCHEMA mart IS
  'Exposure layer: materialized views refreshed after each ingestion, consumed by Grafana. No application writes.';
