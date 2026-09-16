-- Removes any face angle the system stored BY ITSELF, and the attendance that
-- may have been recorded from it.
--
-- Why this is needed
-- ------------------
-- Automatic learning stored a new angle whenever somebody was recognised on a
-- view that looked unfamiliar. That is sound only while every match is correct.
-- It was paired with a lower "soft" matching threshold, and together they let an
-- unenrolled person be accepted as an employee AND that person's face be saved
-- under the employee's name.
--
-- A stored mistake does not correct itself. It makes the next wrong match
-- easier, because the wrong face is now something the system actively looks for.
-- So every automatically learned angle is removed, whether or not it was wrong:
-- there is no way to tell them apart, and the ones that were right cost only a
-- re-enrolment to replace.
--
-- Angles enrolled by a person — labelled 'photo' or 'cctv' — are untouched.
--
-- Run in phpMyAdmin, or:  mysql -u root -p < cleanup_learned.sql

USE face_attendance;


-- ---------------------------------------------------------------
-- 1. What is about to be removed
-- ---------------------------------------------------------------
SELECT t.emp_id, e.name, t.template_no, t.label
FROM templates t
LEFT JOIN employees e ON e.emp_id = t.emp_id
WHERE t.label = 'learned'
ORDER BY t.emp_id, t.template_no;


-- ---------------------------------------------------------------
-- 2. How many angles each person will be left with
--
-- Anyone dropping to zero must be re-enrolled, or they stop being recognised
-- altogether. Check this list before running step 3.
-- ---------------------------------------------------------------
SELECT t.emp_id, e.name,
       SUM(t.label <> 'learned') AS 'angles kept',
       SUM(t.label =  'learned') AS 'angles removed'
FROM templates t
LEFT JOIN employees e ON e.emp_id = t.emp_id
GROUP BY t.emp_id, e.name
ORDER BY 3 ASC;


-- ---------------------------------------------------------------
-- 3. Remove them
-- ---------------------------------------------------------------
DELETE FROM templates WHERE label = 'learned';


-- ---------------------------------------------------------------
-- 4. Today's attendance
--
-- Punches made while the wrong face was being matched cannot be trusted, and
-- there is no way to tell which were correct. Clearing today and letting
-- everybody be recognised again is quicker and more honest than auditing it.
--
-- Only today. Earlier days are left alone.
-- ---------------------------------------------------------------
DELETE FROM events   WHERE DATE(ts) = CURDATE();
DELETE FROM presence WHERE day = CURDATE();


-- ---------------------------------------------------------------
-- 5. Confirm
-- ---------------------------------------------------------------
SELECT label, COUNT(*) AS angles FROM templates GROUP BY label;
SELECT COUNT(*) AS 'punches left today' FROM events WHERE DATE(ts) = CURDATE();
