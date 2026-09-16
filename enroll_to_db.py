import insightface, numpy as np, cv2, mysql.connector, json, os
import tkinter as tk
from tkinter import filedialog

app = insightface.app.FaceAnalysis(name='buffalo_l')
app.prepare(ctx_id=-1)
tk.Tk().withdraw()

# NOTE: web_app.py uses password="" for this same database. One of the two is
# wrong — whichever is right, make them match, or this script and the app will
# be talking to different places (or one of them will not connect at all).
try:
    from local_settings import DB
except Exception:
    DB = dict(host="localhost", port=3306, database="face_attendance",
              user="kenapp", password="KENCamera@2026")

# Photos are no longer stored in the database. The templates table used to have
# a "photo" BLOB column; it was dropped when photos moved onto disk, which is
# why the old INSERT below now fails with "Unknown column 'photo'". web_app.py
# reads them from here, so this script writes them to the same place.
PHOTOS_DIR = "enrolled_photos"

conn = mysql.connector.connect(**DB)
cur = conn.cursor()

def get_numbers(path):
    img = cv2.imread(path)
    if img is None:
        print("  Cannot read:", path); return None
    faces = app.get(img)
    if not faces:
        print("  No face in:", path); return None
    return faces[0].normed_embedding

emp_id = input("Enter Employee ID: ")
name   = input("Enter Name: ")

print("Select 5 photos of this person...")
paths = filedialog.askopenfilenames(title="Select 5 photos",
        filetypes=[("Images", "*.jpg *.jpeg *.png")])

vecs = []
first_photo_bytes = None
for p in paths:
    v = get_numbers(p)
    if v is not None:
        vecs.append(v)
        if first_photo_bytes is None:
            with open(p, "rb") as f:
                first_photo_bytes = f.read()

if not vecs:
    print("No usable photos."); exit()

folder = os.path.join(PHOTOS_DIR, str(emp_id))
os.makedirs(folder, exist_ok=True)
for idx, p in enumerate(paths, 1):
    try:
        with open(p, "rb") as rf, open(os.path.join(folder, f"{idx}.jpg"), "wb") as wf:
            wf.write(rf.read())
    except Exception:
        pass

cur.execute("INSERT INTO employees (emp_id, name) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE name = VALUES(name);",
            (emp_id, name))

cur.execute("SELECT COALESCE(MAX(template_no), 0) FROM templates WHERE emp_id=%s;", (emp_id,))
start_no = (cur.fetchone()[0] or 0) + 1

added = 0
for i, v in enumerate(vecs):
    t_no = start_no + i
    if t_no > 12:
        break
    vec = np.asarray(v, dtype=np.float32)
    vec = vec / np.linalg.norm(vec)
    cur.execute("INSERT INTO templates (emp_id, template_no, embedding, label) "
                "VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE embedding = VALUES(embedding), "
                "label = VALUES(label);",
                (emp_id, t_no, json.dumps(vec.tolist()), "photo"))
    added += 1

conn.commit()
conn.close()

print(f"\nSAVED — {name} (ID {emp_id}): added {added} template(s) from {len(vecs)} photo(s).")