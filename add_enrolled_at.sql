-- Adds an enrolled_at timestamp to the employees table so the People page
-- can show when each person was enrolled. Safe to re-run.
--
-- Anyone already enrolled before this column existed will show blank/"--"
-- for their enrol date on the People page -- that history was never
-- recorded, so it's left honestly blank rather than guessed. Everyone
-- enrolled from now on gets the real date and time automatically.

ALTER TABLE employees ADD COLUMN IF NOT EXISTS enrolled_at DATETIME DEFAULT NULL;
