-- Restores this morning's real arrival times.
--
-- Why this is needed
-- ------------------
-- Today's punches were cleared during the false-match cleanup. That was
-- necessary for the minute-apart IN/OUT cycle, which was pure noise, but it
-- also removed the genuine arrivals recorded afterwards — and those were
-- correct. This puts them back.
--
-- The times come from the screen at 11:33, which listed everybody then in the
-- office with the moment each was first recognised.
--
-- ONE arrival is deliberately NOT restored: Ashish Sunil Kamble at 11:29:02 on
-- the ANITA GODOWN camera. That was the false match — an unenrolled warehouse
-- worker accepted as him. Ashish works in IT and was not in the godown, so
-- putting that row back would restore a record of something that did not
-- happen. If his real arrival time is known, add it with the last statement.
--
-- HOW THIS WORKS
-- Anyone re-recognised since the cleanup already has a new IN for today with
-- the wrong time on it. Rather than inserting duplicates, those rows are
-- UPDATED to the true arrival time. People with no row yet get one inserted.

USE face_attendance;


-- ---------------------------------------------------------------
-- 1. What is in today's log right now
-- ---------------------------------------------------------------
SELECT emp_id, name, TIME(ts) AS time, direction, camera, source
FROM events WHERE DATE(ts) = CURDATE() ORDER BY ts;


-- ---------------------------------------------------------------
-- 2. Correct the arrival time for anyone already re-recognised today
-- ---------------------------------------------------------------
UPDATE events SET ts = CONCAT(CURDATE(), ' 10:53:41'), camera = 'it'
WHERE emp_id = '220264' AND DATE(ts) = CURDATE() AND direction = 'IN';

UPDATE events SET ts = CONCAT(CURDATE(), ' 10:54:39'), camera = 'it'
WHERE emp_id = '120106' AND DATE(ts) = CURDATE() AND direction = 'IN';

UPDATE events SET ts = CONCAT(CURDATE(), ' 10:57:29'), camera = 'it'
WHERE emp_id = '220071' AND DATE(ts) = CURDATE() AND direction = 'IN';

UPDATE events SET ts = CONCAT(CURDATE(), ' 11:10:14'), camera = 'it'
WHERE emp_id = '220006' AND DATE(ts) = CURDATE() AND direction = 'IN';

UPDATE events SET ts = CONCAT(CURDATE(), ' 11:30:53'), camera = 'it'
WHERE emp_id = '220178' AND DATE(ts) = CURDATE() AND direction = 'IN';

UPDATE events SET ts = CONCAT(CURDATE(), ' 11:31:48'), camera = 'it'
WHERE emp_id = '110297' AND DATE(ts) = CURDATE() AND direction = 'IN';

-- ---------------------------------------------------------------
-- 3. Insert an arrival for anyone who has no row yet today
--
-- INSERT IGNORE is not usable here because events has an auto-increment
-- key, so a duplicate would simply be added. The SELECT ... WHERE NOT
-- EXISTS pattern inserts only when nothing is there.
-- ---------------------------------------------------------------
INSERT INTO events (emp_id, name, ts, direction, score, source, camera)
SELECT '220264', 'Yogesh Dattatray Patil', CONCAT(CURDATE(), ' 10:53:41'), 'IN', 0.0, 'camera', 'it'
FROM DUAL WHERE NOT EXISTS (
  SELECT 1 FROM events WHERE emp_id = '220264'
    AND DATE(ts) = CURDATE() AND direction = 'IN');

INSERT INTO events (emp_id, name, ts, direction, score, source, camera)
SELECT '120106', 'Suraj Maruti Patil', CONCAT(CURDATE(), ' 10:54:39'), 'IN', 0.0, 'camera', 'it'
FROM DUAL WHERE NOT EXISTS (
  SELECT 1 FROM events WHERE emp_id = '120106'
    AND DATE(ts) = CURDATE() AND direction = 'IN');

INSERT INTO events (emp_id, name, ts, direction, score, source, camera)
SELECT '220071', 'Ravi Padalkar', CONCAT(CURDATE(), ' 10:57:29'), 'IN', 0.0, 'camera', 'it'
FROM DUAL WHERE NOT EXISTS (
  SELECT 1 FROM events WHERE emp_id = '220071'
    AND DATE(ts) = CURDATE() AND direction = 'IN');

INSERT INTO events (emp_id, name, ts, direction, score, source, camera)
SELECT '220006', 'Nikhil Pandurang Bhosale', CONCAT(CURDATE(), ' 11:10:14'), 'IN', 0.0, 'camera', 'it'
FROM DUAL WHERE NOT EXISTS (
  SELECT 1 FROM events WHERE emp_id = '220006'
    AND DATE(ts) = CURDATE() AND direction = 'IN');

INSERT INTO events (emp_id, name, ts, direction, score, source, camera)
SELECT '220178', 'Bhikaji Balvant Kamble', CONCAT(CURDATE(), ' 11:30:53'), 'IN', 0.0, 'camera', 'it'
FROM DUAL WHERE NOT EXISTS (
  SELECT 1 FROM events WHERE emp_id = '220178'
    AND DATE(ts) = CURDATE() AND direction = 'IN');

INSERT INTO events (emp_id, name, ts, direction, score, source, camera)
SELECT '110297', 'Akash Pandurang Bhosale', CONCAT(CURDATE(), ' 11:31:48'), 'IN', 0.0, 'camera', 'it'
FROM DUAL WHERE NOT EXISTS (
  SELECT 1 FROM events WHERE emp_id = '110297'
    AND DATE(ts) = CURDATE() AND direction = 'IN');

-- ---------------------------------------------------------------
-- 4. Ashish Sunil Kamble — NOT restored on purpose
--
-- His 11:29:02 arrival came from the ANITA GODOWN camera and was a
-- warehouse worker misidentified as him. He works in IT. If his real
-- arrival time is known, remove the two dashes below and set the time.
-- ---------------------------------------------------------------
-- UPDATE events SET ts = CONCAT(CURDATE(), ' 09:30:00'), camera = 'it'
-- WHERE emp_id = '110309' AND DATE(ts) = CURDATE() AND direction = 'IN';

-- ---------------------------------------------------------------
-- 5. Confirm
-- ---------------------------------------------------------------
SELECT emp_id, name, TIME(ts) AS time, direction, camera
FROM events WHERE DATE(ts) = CURDATE() ORDER BY ts;
