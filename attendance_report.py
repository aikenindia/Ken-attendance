"""
KEN Face Attendance — daily report, printed to the console.

Usage:
    python attendance_report.py              today
    python attendance_report.py 2026-08-06   a specific date

Converted from the PostgreSQL version. Two kinds of change were needed: the
driver, and the hours calculation, which was quietly wrong.
"""

import sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import mysql.connector

IST = ZoneInfo("Asia/Kolkata")

# MySQL, not PostgreSQL. Differences that matter:
#   psycopg2      -> mysql.connector
#   dbname=       -> database=
#   port 5432     -> port 3306
#   user postgres -> user root
# Both drivers happen to use %s placeholders, so the queries themselves need
# only one change (see below).
DB = dict(host="localhost", port=3306, database="face_attendance",
          user="root", password="", autocommit=True)


def fetch_punches(day):
    """Every punch for the day, oldest first.

    Ordering by time is not cosmetic — the hours calculation walks the punches
    in order and relies on it.

    The date test changed with the database: PostgreSQL casts with ts::date,
    MySQL uses DATE(ts). Everything else in the query is the same.
    """
    conn = mysql.connector.connect(**DB)
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT emp_id, name, ts, direction, "
                "       COALESCE(source, 'camera') "
                "FROM events WHERE DATE(ts) = %s "
                "ORDER BY emp_id, ts;", (day,))
            return cur.fetchall()
        finally:
            cur.close()
    finally:
        # try/finally matters: without it a failed query leaves the connection
        # open, and enough of those will exhaust MySQL's connection limit.
        conn.close()


def summarise(punches, now):
    """Turns one person's punches into first in, last out, sessions and hours.

    THE FIX. The original paired punches by position — first with second, third
    with fourth, and so on — which silently assumes every day is a perfect
    IN, OUT, IN, OUT. Real days are not:

        IN  IN  OUT     somebody's OUT was missed
        OUT IN  OUT     yesterday's session closed this morning

    On the first the old code reported 2 hours instead of 9. On the second it
    measured the gap from OUT to IN — time the person was NOT here — and counted
    it as work.

    Walking the punches in order and tracking whether a session is open handles
    all of these. An IN opens one; a second IN while already open changes
    nothing; an OUT closes it. Anything left open at the end means the person
    has not left, so their time is counted up to now.
    """
    first_in = None
    last_out = None
    total_seconds = 0.0
    sessions = 0
    open_since = None
    auto = 0

    for ts, direction, source in punches:
        if source == "auto":
            auto += 1

        if direction == "IN":
            if first_in is None:
                first_in = ts
            if open_since is None:
                open_since = ts
        elif direction == "OUT":
            last_out = ts
            if open_since is not None:
                total_seconds += (ts - open_since).total_seconds()
                sessions += 1
                open_since = None

    still_in = open_since is not None
    if still_in:
        total_seconds += (now - open_since).total_seconds()
        sessions += 1

    return {
        "first_in": first_in,
        "last_out": last_out,
        "sessions": sessions,
        "hours": total_seconds / 3600.0,
        "still_in": still_in,
        "auto": auto,
    }


def hhmm(ts):
    return ts.strftime("%H:%M") if ts else "  --"


def build_report(day):
    rows = fetch_punches(day)

    people = {}
    for emp_id, name, ts, direction, source in rows:
        entry = people.setdefault(emp_id, {"name": name or "", "punches": []})
        if name:
            entry["name"] = name
        entry["punches"].append((ts, direction, source))

    print()
    print("ATTENDANCE REPORT   " + day.strftime("%d %b %Y"))
    print("=" * 78)

    if not people:
        print("No attendance recorded on this date.")
        print()
        return

    print("%-8s %-22s %-9s %-9s %-9s %-8s %s"
          % ("ID", "NAME", "FIRST IN", "LAST OUT", "SESSIONS", "HOURS", "NOTE"))
    print("-" * 78)

    now = datetime.now(IST).replace(tzinfo=None)
    total_hours = 0.0
    still_in_count = 0
    auto_total = 0

    for emp_id in sorted(people, key=str):
        data = people[emp_id]
        s = summarise(data["punches"], now)
        total_hours += s["hours"]
        if s["still_in"]:
            still_in_count += 1
        auto_total += s["auto"]

        notes = []
        if s["still_in"]:
            notes.append("still in")
        if s["auto"]:
            notes.append("%d auto" % s["auto"])

        print("%-8s %-22s %-9s %-9s %-9d %-8.2f %s"
              % (str(emp_id), data["name"][:22],
                 hhmm(s["first_in"]), hhmm(s["last_out"]),
                 s["sessions"], s["hours"], ", ".join(notes)))

    print("-" * 78)
    print("%d people   %d punches   %.1f hours total"
          % (len(people), len(rows), total_hours))

    if still_in_count:
        print()
        print("%d person(s) have not punched out. Their hours are counted up to now "
              "and will keep rising until they do." % still_in_count)

    if auto_total:
        print()
        print("%d punch(es) marked 'auto': nobody was seen leaving, so the system "
              "closed the day using the time they were last seen. Those are "
              "estimates, not observations — check them before using the hours "
              "for payroll." % auto_total)
    print()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            wanted = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        except ValueError:
            print("Date must look like 2026-08-06")
            sys.exit(1)
    else:
        wanted = datetime.now(IST).date()

    try:
        build_report(wanted)
    except mysql.connector.Error as e:
        print("Database error: %s" % e)
        print("Check that MySQL is running in XAMPP and that the DB settings at "
              "the top of this file are right.")
        sys.exit(1)