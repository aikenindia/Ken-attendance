-- =====================================================================
-- KEN Face Attendance — update for the existing VPS database
--
--   mysql -u root -p face_attendance < update.sql
--
-- Written against what is already there:
--   employees 10 · events 2139 · presence 15 · templates 59 · users 1
--
-- No table is created and no row is deleted. Only indexes are added, and
-- each is checked first, so running this twice is safe and #1061 will not
-- appear. Read-only checks at the end.
--
-- BACK UP FIRST:
--   mysqldump -u root -p face_attendance > backup_$(date +%F).sql
-- =====================================================================

USE face_attendance;


-- =====================================================================
-- 1. INDEXES
-- =====================================================================

DROP PROCEDURE IF EXISTS ken_add_index;
DELIMITER $$
CREATE PROCEDURE ken_add_index(IN tbl VARCHAR(64), IN idx VARCHAR(64),
                               IN cols VARCHAR(255), IN uniq TINYINT)
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.STATISTICS
                   WHERE table_schema = DATABASE()
                     AND table_name = tbl AND index_name = idx) THEN
        SET @q = CONCAT('ALTER TABLE `', tbl, '` ADD ',
                        IF(uniq = 1, 'UNIQUE ', ''), 'INDEX `', idx,
                        '` (', cols, ')');
        PREPARE s FROM @q; EXECUTE s; DEALLOCATE PREPARE s;
        SELECT CONCAT('CREATED  ', idx, ' on ', tbl) AS step;
    ELSE
        SELECT CONCAT('present  ', idx, ' on ', tbl) AS step;
    END IF;
END$$
DELIMITER ;

-- The hot one. Both _seed_last_punch and close_session ask for "this person's
-- newest punch today" — an equality on emp_id and a range on ts. Equality
-- column first so the engine seeks to the person and then walks their rows
-- backwards, which also satisfies the ORDER BY for free.
CALL ken_add_index('events', 'idx_events_emp_ts', '`emp_id`, `ts`', 0);

-- Whole-day reads not scoped to one person: Today, reports, the sweep.
CALL ken_add_index('events', 'idx_events_ts', '`ts`', 0);

-- Multi-area: "who was in the godown today", and the area breakdown.
CALL ken_add_index('events', 'idx_events_camera_ts', '`camera`, `ts`', 0);

CALL ken_add_index('presence', 'idx_presence_day', '`day`', 0);
CALL ken_add_index('presence', 'idx_presence_last_seen', '`last_seen`', 0);
CALL ken_add_index('templates', 'idx_templates_emp', '`emp_id`', 0);

DROP PROCEDURE IF EXISTS ken_add_index;


-- Redundant once idx_events_emp_ts exists: emp_id is its leading column, so
-- it answers everything the single-column index could. It still has to be
-- updated on every INSERT, and this table takes one on every punch.
SET @q := (SELECT IF(COUNT(*) > 0,
    'ALTER TABLE events DROP INDEX idx_events_emp',
    'SELECT ''idx_events_emp not present'' AS step')
  FROM information_schema.STATISTICS
  WHERE table_schema = DATABASE() AND table_name = 'events'
    AND index_name = 'idx_events_emp');
PREPARE s FROM @q; EXECUTE s; DEALLOCATE PREPARE s;


-- =====================================================================
-- 2. COLUMN WIDTH
--
-- The source column gained two new values, 'track' and 'moved'. Both fit in
-- VARCHAR(16), but older installs created this column narrower or without a
-- default, and a silent truncation here would make reports lie about how a
-- punch was decided.
-- =====================================================================

SET @q := (SELECT IF(CHARACTER_MAXIMUM_LENGTH < 16,
    'ALTER TABLE events MODIFY source VARCHAR(16) NOT NULL DEFAULT ''camera''',
    'SELECT ''source column is wide enough'' AS step')
  FROM information_schema.COLUMNS
  WHERE table_schema = DATABASE() AND table_name = 'events'
    AND column_name = 'source');
PREPARE s FROM @q; EXECUTE s; DEALLOCATE PREPARE s;


-- =====================================================================
-- 3. CHECKS — read only. Nothing below changes anything.
-- =====================================================================

SELECT '--- indexes now present ---' AS check_;
SELECT TABLE_NAME, INDEX_NAME,
       GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS cols
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('events', 'presence', 'templates')
GROUP BY TABLE_NAME, INDEX_NAME
ORDER BY TABLE_NAME, INDEX_NAME;


SELECT '--- duplicate punches (the repeated OUT rows) ---' AS check_;
SELECT emp_id, name, ts, direction, COUNT(*) AS copies
FROM events
GROUP BY emp_id, name, ts, direction
HAVING copies > 1
ORDER BY copies DESC, ts DESC;


SELECT '--- duplicate presence rows ---' AS check_;
SELECT emp_id, day, COUNT(*) AS copies
FROM presence GROUP BY emp_id, day HAVING copies > 1;


SELECT '--- sessions still open from previous days ---' AS check_;
SELECT e.emp_id, e.name, DATE(e.ts) AS day, e.ts AS opened_at, e.camera
FROM events e
WHERE e.direction = 'IN'
  AND e.ts < CURDATE()
  AND NOT EXISTS (SELECT 1 FROM events o
                  WHERE o.emp_id = e.emp_id AND o.direction = 'OUT'
                    AND o.ts > e.ts AND DATE(o.ts) = DATE(e.ts))
ORDER BY e.ts DESC;


SELECT '--- how each OUT was decided, last 7 days ---' AS check_;
SELECT source, COUNT(*) AS punches
FROM events
WHERE direction = 'OUT' AND ts >= CURDATE() - INTERVAL 7 DAY
GROUP BY source ORDER BY punches DESC;


SELECT '--- people with too few enrolled angles ---' AS check_;
SELECT e.emp_id, e.name, COUNT(t.template_no) AS angles
FROM employees e
LEFT JOIN templates t ON t.emp_id = e.emp_id
GROUP BY e.emp_id, e.name
HAVING angles < 6
ORDER BY angles;


SELECT '--- is the index actually used? want range or ref, not ALL ---' AS check_;
EXPLAIN SELECT ts, direction, camera FROM events
 WHERE emp_id = '220071' AND ts >= CURDATE() AND ts < CURDATE() + INTERVAL 1 DAY
 ORDER BY ts DESC LIMIT 1;


-- =====================================================================
-- 4. CLEANUP — commented out on purpose.
--
-- Run section 3 first and look at what it returned. Uncomment only the
-- statement matching a problem you actually have, and only after a backup.
-- A DELETE with a wrong JOIN removes rows silently and reports success.
-- =====================================================================

-- Duplicate punches, keeping the earliest row of each group:
-- DELETE e1 FROM events e1
-- JOIN events e2
--   ON e1.emp_id = e2.emp_id AND e1.ts = e2.ts
--  AND e1.direction = e2.direction AND e1.id > e2.id;

-- Duplicate presence rows, keeping the latest sighting:
-- DELETE p1 FROM presence p1
-- JOIN presence p2
--   ON p1.emp_id = p2.emp_id AND p1.day = p2.day
--  AND p1.last_seen < p2.last_seen;

-- Then, and only then, the unique key that prevents it recurring:
-- ALTER TABLE presence ADD UNIQUE INDEX idx_presence_emp_day (emp_id, day);


SELECT 'update.sql finished' AS result;
