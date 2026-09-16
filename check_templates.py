"""
KEN Face Attendance — template health check.

Compares every stored face template against every other one. No camera
involved, so it separates two very different problems:

  * templates are fine, the CAMERA view is the problem
  * templates themselves carry no identity, in which case no camera
    change and no threshold change will ever help

Run on the server, inside the app's virtual environment:

    cd /opt/ken-attendance
    source venv/bin/activate        (or however the service venv is named)
    python check_templates.py
"""

import json
import itertools
import numpy as np
import mysql.connector

# Must match local_settings.py. If the app connects, these values do too.
DB = dict(host="localhost", port=3306, database="face_attendance",
          user="root", password="")

try:
    import local_settings
    if hasattr(local_settings, "DB"):
        DB = local_settings.DB
        print("[db] using the connection details from local_settings.py")
except ImportError:
    print("[db] local_settings.py not found — using the defaults in this file")


def load():
    conn = mysql.connector.connect(**DB)
    cur = conn.cursor()
    cur.execute(
        "SELECT t.emp_id, e.name, t.template_no, t.label, t.embedding "
        "FROM templates t JOIN employees e ON e.emp_id = t.emp_id "
        "ORDER BY t.emp_id, t.template_no;")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    people = {}
    for emp_id, name, template_no, label, raw in rows:
        vec = np.array(json.loads(raw), dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm == 0:
            print("  !! " + str(name) + " template " + str(template_no)
                  + " is all zeros — that row is corrupt")
            continue
        people.setdefault((str(emp_id), name or str(emp_id)), []).append(
            (template_no, label or "", vec / norm))
    return people


def main():
    people = load()
    if not people:
        print("No templates stored. Nothing to check.")
        return

    total = sum(len(v) for v in people.values())
    print("\n" + str(len(people)) + " people, " + str(total) + " templates\n")

    # -- how the labels break down ----------------------------------------
    labels = {}
    for tpl in people.values():
        for _, label, _ in tpl:
            labels[label] = labels.get(label, 0) + 1
    print("Where the templates came from:")
    for label, n in sorted(labels.items(), key=lambda kv: -kv[1]):
        print("   " + (label or "(blank)").ljust(12) + str(n))
    if labels.get("cctv", 0) == 0:
        print("   -> none captured through a CCTV camera")
    print()

    # -- same person, different photos -------------------------------------
    print("=" * 66)
    print("SAME PERSON, different photos   —   healthy is 0.45 to 0.80")
    print("=" * 66)
    same_all = []
    singles = []
    for (emp_id, name), tpl in sorted(people.items(), key=lambda kv: kv[0][1]):
        if len(tpl) < 2:
            singles.append(name)
            continue
        scores = [float(a[2] @ b[2]) for a, b in itertools.combinations(tpl, 2)]
        same_all += scores
        avg = sum(scores) / len(scores)
        flag = "   <-- LOW" if avg < 0.35 else ""
        print("%-34s n=%2d   min %.2f   avg %.2f   max %.2f%s"
              % (name[:34], len(tpl), min(scores), avg, max(scores), flag))
    if singles:
        print("\nOnly one template, cannot be checked: " + ", ".join(singles))

    # -- different people ---------------------------------------------------
    flat = [(name, vec) for (emp_id, name), tpl in people.items()
            for _, _, vec in tpl]
    diff_all = []
    worst = []
    for a, b in itertools.combinations(flat, 2):
        if a[0] == b[0]:
            continue
        s = float(a[1] @ b[1])
        diff_all.append(s)
        worst.append((s, a[0], b[0]))

    print("\n" + "=" * 66)
    print("DIFFERENT PEOPLE   —   healthy is under 0.30")
    print("=" * 66)
    if diff_all:
        print("%d pairs   avg %.2f   max %.2f"
              % (len(diff_all), sum(diff_all) / len(diff_all), max(diff_all)))
        worst.sort(reverse=True)
        print("\nClosest confusable pairs:")
        for s, one, two in worst[:8]:
            mark = "   <-- too close" if s >= 0.35 else ""
            print("   %.2f   %s  vs  %s%s" % (s, one[:24], two[:24], mark))

    # -- a template sitting near the centre of everything -------------------
    print("\n" + "=" * 66)
    print("TEMPLATES THAT MATCH EVERYONE   —   these cause false positives")
    print("=" * 66)
    centre = []
    for (emp_id, name), tpl in people.items():
        others = [v for (e2, n2), t2 in people.items() if n2 != name
                  for _, _, v in t2]
        if not others:
            continue
        others = np.array(others, dtype=np.float32)
        for template_no, label, vec in tpl:
            mean_to_others = float((others @ vec).mean())
            centre.append((mean_to_others, name, template_no, label))
    centre.sort(reverse=True)
    for score, name, template_no, label in centre[:6]:
        mark = "   <-- suspect, consider deleting" if score >= 0.20 else ""
        print("   %.3f   %-30s template %-3s %s%s"
              % (score, name[:30], template_no, label, mark))

    # -- the verdict ---------------------------------------------------------
    print("\n" + "=" * 66)
    print("VERDICT")
    print("=" * 66)
    if not same_all:
        print("Nobody has two templates, so same-person scores cannot be")
        print("measured. Enrol at least two angles for a few people and")
        print("run this again.")
        return

    same_avg = sum(same_all) / len(same_all)
    diff_avg = sum(diff_all) / len(diff_all) if diff_all else 0.0
    print("same person   avg %.2f" % same_avg)
    print("different     avg %.2f" % diff_avg)
    print("separation        %.2f" % (same_avg - diff_avg))
    print()

    if same_avg < 0.30:
        print("BROKEN. A person's own two photos barely match each other.")
        print("The embeddings carry almost no identity, so no camera change")
        print("and no threshold change will help. Something in the enrolment")
        print("path is wrong — send this whole output back.")
    elif same_avg < 0.45:
        print("WEAK. The templates hold some identity but not much. Likely")
        print("small, blurry or heavily angled enrolment photos. Re-enrol")
        print("with clear front-on shots before touching anything else.")
    elif (same_avg - diff_avg) < 0.25:
        print("The templates separate people poorly. Look at the confusable")
        print("pairs above and re-enrol those people.")
    else:
        print("HEALTHY. The stored templates are fine — the problem is that")
        print("the CAMERA view does not resemble them. Enrol through each")
        print("camera, from where people actually sit, using the CCTV")
        print("capture panel on the Enrol page.")


if __name__ == "__main__":
    main()