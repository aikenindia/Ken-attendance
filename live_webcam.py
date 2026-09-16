import insightface, numpy as np, cv2, mysql.connector, json

app = insightface.app.FaceAnalysis(name='buffalo_l')
app.prepare(ctx_id=-1)

# --- load enrolled faces from database ---
conn = mysql.connector.connect(
    host="localhost", port=3306, database="face_attendance",
    user="root", password=""   # <-- your password
)
cur = conn.cursor()
cur.execute("SELECT t.emp_id, e.name, t.embedding "
            "FROM templates t JOIN employees e ON t.emp_id = e.emp_id;")
rows = cur.fetchall()
conn.close()

ids    = [r[0] for r in rows]
names  = [r[1] for r in rows]
# embedding is stored as JSON text (MySQL has no native array type)
matrix = np.array([json.loads(r[2]) for r in rows], dtype=np.float32)
print(f"Loaded {len(ids)} people. Opening camera... press Q to quit")

# --- open the webcam ---
cap = cv2.VideoCapture(0)          # 0 = laptop webcam

while True:
    ok, frame = cap.read()         # grab one frame
    if not ok:
        break

    faces = app.get(frame)         # find faces in this frame
    for f in faces:
        scores = matrix @ f.normed_embedding
        best = int(scores.argmax())
        if scores[best] > 0.4:
            label = f"{names[best]} {scores[best]:.2f}"
            colour = (0, 255, 0)   # green = known
        else:
            label = "Unknown"
            colour = (0, 0, 255)   # red = unknown

        box = f.bbox.astype(int)
        cv2.rectangle(frame, (box[0], box[1]), (box[2], box[3]), colour, 2)
        cv2.putText(frame, label, (box[0], box[1]-10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, 2)

    cv2.imshow("Live Face Recognition - press Q to quit", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()