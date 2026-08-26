-- Migration 017: hand-maintained training plan (core.plan_week).
-- Source: docs/COACH_FEEDBACK.md Q9, arbitrated by docs/COACH_FEEDBACK_3.md section 2.3.
-- Batch L1 (coaching dashboards) -- do not modify the DDL manually outside migration.

-- WHY. The platform is "a coroner''s report": superb at explaining what killed the block,
-- silent about what should happen next (Coach 3). Coach 1 Q9 prescribes exactly this table
-- and explicitly rejects anything cleverer: "you''re one athlete; a planning engine is
-- over-engineering". Coach 2''s objection ("don''t invent planned data in Grafana") is
-- honored by putting the plan IN THE DATABASE, versioned and migratable, instead of as a
-- Grafana transformation -- same principle as COACH_FEEDBACK_3 section 5 point 2 (business
-- logic in SQL, never in dashboard JSON).
--
-- Deliberately dumb: one row per athlete/week/discipline, maintained in five minutes a
-- week. Downstream panels (planned vs actual hours/TSS, later forward TSB projection)
-- read it; nothing writes it except the athlete.

CREATE TABLE core.plan_week (
    athlete_id       smallint NOT NULL REFERENCES core.athlete(athlete_id),
    week_start_date  date     NOT NULL,
    discipline       text     NOT NULL,
    target_hours     real     NULL,
    target_tss       real     NULL,
    note             text     NULL,
    CONSTRAINT plan_week_pk PRIMARY KEY (athlete_id, week_start_date, discipline),
    CONSTRAINT plan_week_discipline_ck CHECK (discipline IN ('swim','bike','run','other')),
    CONSTRAINT plan_week_has_target_ck CHECK (target_hours IS NOT NULL OR target_tss IS NOT NULL)
);

COMMENT ON TABLE core.plan_week IS
  'Weekly plan targets per discipline, HAND-MAINTAINED (Coach 1 Q9, arbitrated Coach 3 section 2.3): no planning engine, no auto-fill. Coaching = comparing intent against execution; this table is what turns "what happened" into "am I on schedule", and it is the prerequisite for real tapers and for projected-TSB-through-plan (the killer feature neither coach 1 nor 2 saw, Coach 3 section 2.3). One row per (week, discipline); disciplines without a plan that week simply have no row -- absence of intent must stay distinguishable from a zero target, hence no COALESCE-to-0 anywhere downstream.';

COMMENT ON COLUMN core.plan_week.week_start_date IS
  'Monday of the planned week, by convention (ISO weeks), matching how every weekly aggregation panel groups local_date. No CHECK forcing Monday: a mid-week race block may legitimately start a "week" on any day, and the convention belongs to the writer, not the schema.';

COMMENT ON COLUMN core.plan_week.target_hours IS
  'Planned hours for the week/discipline. Nullable BY DESIGN: some blocks are planned in TSS only (e.g. tapers where time is easy but intensity matters). plan_week_has_target_ck guarantees each row carries at least ONE target so an empty row cannot masquerade as a plan.';

COMMENT ON COLUMN core.plan_week.target_tss IS
  'Planned TSS for the week/discipline, on the canonical TSS scale (same scale as mart.daily_load.tss). This column is what makes forward TSB projection a trivial recursion over the PMC math; keep values consistent with the load methods actually used, or variance panels will compare incomparable numbers.';
