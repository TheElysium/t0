-- Migration 013: adds `home_trainer` to the vocabulary of core.threshold.power_source.
-- No modification of 003: applied migrations are not rewritten
-- (the runner verifies their checksum). Same form as 010 -- only COMMENT ON,
-- neither structure nor data change.
--
-- WHY -- the column has no CHECK constraint: its vocabulary lives only in
-- this comment, that is, what an operator reads with \d+ core.threshold.
-- The template deploy/seed/athlete.sql.example already announces `home_trainer`, not the
-- comment: two diverging sources of truth, and nothing in the database to settle it.
--
-- The case is real, not theoretical: the athlete has no power meter on the
-- bike, their only measured power comes from the home-trainer. An FTP measured on a home-trainer
-- and an FTP measured at the pedals are not the same quantity -- same reasoning as
-- Garmin vs Stryd in running. Hence the rule, unchanged: switching from one to the other CLOSES the
-- validity period, we never UPDATE a threshold in place.

COMMENT ON COLUMN core.threshold.power_source IS
  'Origin of power measurement (garmin_native, stryd, powermeter_<model>, home_trainer). VERIFIED (04 section 3.2): Garmin Running Power is an undocumented proprietary model; "a 250 W Garmin and a 250 W Stryd are not the same measurement". Same caveat for home_trainer, where power is estimated by the simulator not measured at the pedals. A change in value here MUST close the validity period and invalidate the rFTP.';
