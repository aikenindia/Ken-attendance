import insightface, numpy as np, cv2, psycopg2
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")     # India timezone — used for all timestamps

app = insightface.app.FaceAnalysis(name='buffalo_l')
app.prepare(ctx_id=-1)

DB = dict(host="localhost", port=5432, dbname="face_attendance",
          user="postgres", password="Admin123")

conn = psycopg2.connect(**DB)
cur = conn.cursor()
cur.execute("SELECT t.emp_id, e.name, t.embedding "
            "FROM templates t JOIN employees e ON t.emp_id = e.emp_id;")
rows = cur.fetchall()
conn.close()

if not rows:
    print("No enrolled people found. Enrol someone first.")
    exit()

ids    = [r[0] for r in rows]
names  = [r[1] for r in rows]
matrix = np.array([r[2] for r in rows], dtype=np.float32)
print(f"Loaded {len(ids)} people. Opening camera... press Q to quit")

MATCH_THRESHOLD = 0.4
DEDUP_SECONDS = 1 * 60     # 1 minute gap required between two punches of the same person
MAX_DETECTION_DIM = 800    # resize any side longer than this before running face detection


def resize_for_detection(img):
    """Shrink large frames before face detection to cut CPU processing time,
    same optimization used in web_app.py — keeps things fast and consistent."""
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest > MAX_DETECTION_DIM:
        scale = MAX_DETECTION_DIM / longest
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        return img, scale
    return img, 1.0


def record_event(emp_id, name, score):
    """
    Records an IN/OUT punch for emp_id, but ONLY if at least DEDUP_SECONDS
    have passed since that person's last recorded punch (checked from the
    DB, not memory, so a script restart or a second camera can't create a
    duplicate punch within the dedup window). Timestamps use IST throughout,
    matching web_app.py so CCTV and browser-marked attendance line up.
    """
    now = datetime.now(IST)

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()

    # 1. Find this person's last punch (any day) to enforce the dedup gap
    cur.execute("SELECT ts FROM events WHERE emp_id=%s ORDER BY ts DESC LIMIT 1;",
                (emp_id,))
    row = cur.fetchone()
    if row:
        last_ts = row[0]
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=IST)
        gap_seconds = (now - last_ts).total_seconds()
        if gap_seconds < DEDUP_SECONDS:
            conn.close()
            remaining = int(DEDUP_SECONDS - gap_seconds)
            print(f"  SKIPPED: {name} ({emp_id}) already punched {int(gap_seconds)}s ago "
                  f"(wait {remaining}s more)")
            return None

    # 2. Decide IN/OUT by counting today's punches so far (parity-based)
    cur.execute("SELECT COUNT(*) FROM events WHERE emp_id=%s AND ts::date=CURRENT_DATE;",
                (emp_id,))
    count = cur.fetchone()[0]
    direction = "IN" if count % 2 == 0 else "OUT"     # alternate every punch

    cur.execute("INSERT INTO events (emp_id, name, ts, direction, score) "
                "VALUES (%s,%s,%s,%s,%s);",
                (emp_id, name, now, direction, float(score)))
    conn.commit()
    conn.close()
    print(f"  RECORDED: {name} ({emp_id}) {direction} at {now.strftime('%H:%M:%S')} IST")
    return direction


# Camera source — swap this line depending on what you're connecting to:
#   Webcam:         cv2.VideoCapture(0)
#   Phone IP cam:   cv2.VideoCapture("http://192.168.31.214:8080/video")
#   Hikvision CCTV: cv2.VideoCapture("rtsp://admin:PASSWORD@192.168.1.71:554/Streaming/Channels/101")
cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("ERROR: Could not open the camera stream. Check the URL/IP, port, "
          "credentials, and that this PC is on the same network as the camera.")
    exit()

while True:
    ok, frame = cap.read()
    if not ok:
        print("Lost connection to camera stream.")
        break

    small, scale = resize_for_detection(frame)
    for f in app.get(small):
        scores = matrix @ f.normed_embedding
        best = int(scores.argmax())
        box = (f.bbox / scale).astype(int)   # scale detection box back to original frame size

        if scores[best] > MATCH_THRESHOLD:
            emp_id, name = ids[best], names[best]
            record_event(emp_id, name, scores[best])
            label = f"{name} {scores[best]:.2f}"
            colour = (0, 255, 0)
        else:
            label, colour = "Unknown", (0, 0, 255)

        cv2.rectangle(frame, (box[0], box[1]), (box[2], box[3]), colour, 2)
        cv2.putText(frame, label, (box[0], box[1] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, 2)

    cv2.imshow("Live Attendance - press Q to quit", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()