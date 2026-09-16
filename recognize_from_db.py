import insightface, numpy as np, cv2, mysql.connector, json
import tkinter as tk
from tkinter import filedialog

app = insightface.app.FaceAnalysis(name='buffalo_l')
app.prepare(ctx_id=-1)
tk.Tk().withdraw()

DB = dict(host="localhost", port=3306, database="face_attendance",
          user="root", password="")   # <-- your password

conn = mysql.connector.connect(**DB)
cur = conn.cursor()
cur.execute("SELECT t.emp_id, e.name, t.embedding "
            "FROM templates t JOIN employees e ON t.emp_id = e.emp_id;")
rows = cur.fetchall()
conn.close()

ids    = [r[0] for r in rows]
names  = [r[1] for r in rows]
# embedding is stored as JSON text (MySQL has no native array type)
matrix = np.array([json.loads(r[2]) for r in rows], dtype=np.float32)
print(f"Loaded {len(ids)} enrolled people.\n")

print("Select a photo to recognise...")
path = filedialog.askopenfilename(filetypes=[("Images", "*.jpg *.jpeg *.png")])
img = cv2.imread(path)
faces = app.get(img)

if not faces:
    print("No face found.")
else:
    new = faces[0].normed_embedding
    scores = matrix @ new
    best = int(scores.argmax())
    if scores[best] > 0.4:
        print(f"RECOGNISED: {names[best]} (ID {ids[best]}) — score {scores[best]:.2f}")
    else:
        print(f"UNKNOWN — best score {scores[best]:.2f}")