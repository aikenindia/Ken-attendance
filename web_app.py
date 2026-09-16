"""KEN Face Attendance — FastAPI application."""

import io, os, base64, threading, time, json, socket, asyncio, ssl, math
from collections import deque
from urllib.parse import urlparse, urlunparse, unquote, quote
import urllib.request

try:
    import local_settings as _local
except ImportError:
    _local = None

INFERENCE_THREADS = getattr(_local, "INFERENCE_THREADS", 2)
os.environ.setdefault("OMP_NUM_THREADS", str(INFERENCE_THREADS))
os.environ.setdefault("OPENBLAS_NUM_THREADS", str(INFERENCE_THREADS))
os.environ.setdefault("MKL_NUM_THREADS", str(INFERENCE_THREADS))
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|framedrop;1|stimeout;5000000"

FIRST_FRAME_TIMEOUT = 15

from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
import mysql.connector
from mysql.connector import pooling
import insightface, numpy as np, cv2
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

cv2.setNumThreads(INFERENCE_THREADS)

BUILD = "2026-08-27 · Camera log lines now show the full address (with ?channel=... ) so similar candidates aren't indistinguishable"

IST = ZoneInfo("Asia/Kolkata")


DB = dict(host="localhost", port=3306, database="face_attendance",
          user="root", password="", autocommit=True)


CAMERAS = [
    {
        "id": "it",
        "name": "IT Department",
        "url": [
            "http://admin:%40Ken%40123@203.109.35.75:8081"
            "/ISAPI/Streaming/channels/101/httpPreview",
            "http://admin:%40Ken%40123@203.109.35.75:8081"
            "/ISAPI/Streaming/channels/101/picture",
        ],
        "recognition": True,
        "exit_zone": (0.62, 0.58, 1.0, 1.0),
        "watch_zone": (0.45, 0.45, 1.0, 1.0),
    },
    {
        "id": "unit1",
        "name": "KEN Global Unit 1",
        "host": "https://203.109.35.73:8443",
        "user": "admin",
        "password": "@Ken@321",
        "recognition": True,
        "exit_zone": (0.0, 0.0, 1.0, 1.0),
        "mode": "doorway",
        "approach_means": "in",
    },
    {
        "id": "entry",
        "name": "ANITA GODOWN",
        "host": "203.109.35.76",
        "user": "admin",
        "password": "",
        "recognition": True,
        "exit_zone": (0.0, 0.0, 1.0, 1.0),
        "mode": "doorway",
        "approach_means": "in",
    },
]

DEFAULT_CAMERA = "it"

SNAPSHOT_INTERVAL = 0.05
SNAPSHOT_WORKERS = 3


def camera_ids():
    return [c["id"] for c in CAMERAS]


def get_camera(cam_id):
    """The camera with this id, or the default if the id is unknown."""
    for cam in CAMERAS:
        if cam["id"] == cam_id:
            return cam
    for cam in CAMERAS:
        if cam["id"] == DEFAULT_CAMERA:
            return cam
    return CAMERAS[0]


ROOM_DET_SIZE = getattr(_local, "ROOM_DET_SIZE", (1920, 1920))
ROOM_INTERVAL = getattr(_local, "ROOM_INTERVAL", 0.5)

# How sure the model must be before it counts something as a face at all.
# insightface's own default (0.5) is tuned for crowded, low-quality CCTV
# frames where being fussy avoids false alarms. A single, close-up webcam
# enrolment photo carries no such risk — one clear person, filling the
# frame — so a photo with a real face was still getting turned away purely
# for not clearing that CCTV-grade bar. Lowering it here applies to every
# use of face_app/exit_app (attendance recognition included), which only
# makes both more forgiving, never stricter.
FACE_DET_THRESH = getattr(_local, "FACE_DET_THRESH", 0.35)

EXIT_DET_SIZE = (640, 640)
EXIT_INTERVAL = 0.5

# Enrolment and the other on-demand endpoints (Enrol page, webcam test
# page) always see one clear, close, front-facing face -- nothing like a
# room-wide, multi-person camera frame -- so they gain nothing from
# detecting at the room camera's high resolution. Running at that size
# anyway was the main reason "Enrol" and the instant per-photo check felt
# slow even after they stopped waiting on the live cameras: a much bigger
# image was being fed through the detector than the photo actually needed.
# Matching EXIT_DET_SIZE here (already proven fast and accurate for a
# single close-up face) cuts that per-photo detection time sharply.
INTERACTIVE_DET_SIZE = getattr(_local, "INTERACTIVE_DET_SIZE", (640, 640))
INTERACTIVE_MAX_DIM = getattr(_local, "INTERACTIVE_MAX_DIM", 960)

SEAT_PERSIST_SECONDS = 6.0

MOTION_MIN_DELTA = 2.0

MAX_DETECTION_DIM = getattr(_local, "MAX_DETECTION_DIM", 1920)

STREAM_MAX_WIDTH = getattr(_local, "STREAM_MAX_WIDTH", 1600)
STREAM_FPS = getattr(_local, "STREAM_FPS", 15)
STREAM_JPEG_QUALITY = 85
UNWATCHED_ENCODE_INTERVAL = 3.0


MATCH_THRESHOLD = getattr(_local, "MATCH_THRESHOLD", 0.40)
SIDE_MATCH_THRESHOLD = getattr(_local, "SIDE_MATCH_THRESHOLD", 0.28)
MATCH_MARGIN = getattr(_local, "MATCH_MARGIN", 0.08)

IDENTIFY_MIN_FACE_PCT = getattr(_local, "IDENTIFY_MIN_FACE_PCT", 0.018)
IDENTIFY_COMFORTABLE_PCT = getattr(_local, "IDENTIFY_COMFORTABLE_PCT", 0.08)
IDENTIFY_SMALL_PENALTY = getattr(_local, "IDENTIFY_SMALL_PENALTY", 0.02)

REREAD_BELOW_PCT = getattr(_local, "REREAD_BELOW_PCT", 0.0)
REREAD_PAD = 0.45

PRESENCE_MIN_SCORE = getattr(_local, "PRESENCE_MIN_SCORE", 0.38)

SCORE_DEBUG = getattr(_local, "SCORE_DEBUG", True)

SOFT_MATCH_ENABLED = getattr(_local, "SOFT_MATCH_ENABLED", False)
SOFT_MATCH_THRESHOLD = getattr(_local, "SOFT_MATCH_THRESHOLD", 0.28)
SOFT_MATCH_CONFIRMATIONS = getattr(_local, "SOFT_MATCH_CONFIRMATIONS", 3)
SOFT_MATCH_WINDOW = getattr(_local, "SOFT_MATCH_WINDOW", 30)

SIDE_CONFIRM_COUNT = getattr(_local, "SIDE_CONFIRM_COUNT", 2)
SIDE_CONFIRM_WINDOW = getattr(_local, "SIDE_CONFIRM_WINDOW", 12)

AUTO_LEARN = getattr(_local, "AUTO_LEARN", False)
AUTO_LEARN_MIN_SCORE = getattr(_local, "AUTO_LEARN_MIN_SCORE", 0.50)
AUTO_LEARN_MAX_SCORE = getattr(_local, "AUTO_LEARN_MAX_SCORE", 0.80)
AUTO_LEARN_COOLDOWN = getattr(_local, "AUTO_LEARN_COOLDOWN", 600)

DEDUP_SECONDS = 60

TEMPLATE_CACHE_SECONDS = 60

MAX_TEMPLATES_PER_PERSON = 12


YOLO_ENABLED = False
YOLO_MODEL = "yolov8n.pt"
YOLO_CONF = 0.35
YOLO_INTERVAL = 0.4

SHOW_UNKNOWN_BOXES = getattr(_local, "SHOW_UNKNOWN_BOXES", False)

FACE_IN_PERSON_MIN = 0.35

TRACK_LOST_SECONDS = 6.0

TRACK_DOOR_MEMORY = 10.0

DEFAULT_EXIT_ZONE = (0.62, 0.58, 1.0, 1.0)

EXIT_MIN_FACE_PCT = 0.10

EXIT_CONFIRM_SECONDS = 45

EXIT_ARM_EXPIRY = 300

EXIT_CHECK_SECONDS = 15

AUTO_CHECKOUT_ENABLED = getattr(_local, "AUTO_CHECKOUT_ENABLED", True)

DOORWAY_TREND_SAMPLES = 3
DOORWAY_TREND_WINDOW = 4.0
DOORWAY_TREND_CHANGE = 0.35

DAY_ENDS_AT = (20, 0)

PRESENCE_STALE_MINUTES = 20

PRESENCE_WRITE_SECONDS = getattr(_local, "PRESENCE_WRITE_SECONDS", 120)

STATIC_AFTER_SECONDS = 15
STATIC_AFTER_HITS = 5

ZONE_DEBUG = getattr(_local, "ZONE_DEBUG", True)

PHOTOS_DIR = "enrolled_photos"
os.makedirs(PHOTOS_DIR, exist_ok=True)

LOGO_B64 = ""

try:
    LOGO_BYTES = base64.b64decode(LOGO_B64) if LOGO_B64 else b""
except Exception:
    LOGO_BYTES = b""

if _local is not None:
    for _name in dir(_local):
        if _name.isupper():
            globals()[_name] = getattr(_local, _name)
_osnet_instance = None
_osnet_lock = threading.Lock()
_active_room_gallery = {}
_gallery_lock = threading.Lock()
OSNET_MODEL_PATH = getattr(_local, "OSNET_MODEL_PATH", "models/osnet_x0_25.onnx")
REID_MATCH_THRESHOLD = getattr(_local, "REID_MATCH_THRESHOLD", 0.70)

class OSNetExtractor:
    def __init__(self, model_path=OSNET_MODEL_PATH):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(model_path, sess_options=opts)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self.mean = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(1, 3, 1, 1)
        self.std = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(1, 3, 1, 1)

    def extract_crop(self, crop_bgr):
        if crop_bgr is None or crop_bgr.size == 0 or crop_bgr.shape[0] < 20 or crop_bgr.shape[1] < 10:
            return None
        try:
            img = cv2.resize(crop_bgr, (128, 256))
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            img = np.transpose(img, (2, 0, 1))
            batch = np.zeros((16, 3, 256, 128), dtype=np.float32)
            batch[0] = img
            batch = (batch - self.mean) / self.std
            out = self.session.run([self.output_name], {self.input_name: batch})[0]
            vec = out[0]
            norm = np.linalg.norm(vec)
            if norm > 1e-6:
                vec /= norm
            return vec
        except Exception as e:
            print("[osnet] extraction error:", e)
            return None

def get_osnet_extractor():
    global _osnet_instance
    if _osnet_instance is None and os.path.exists(OSNET_MODEL_PATH):
        with _osnet_lock:
            if _osnet_instance is None:
                try:
                    _osnet_instance = OSNetExtractor(OSNET_MODEL_PATH)
                    print("[osnet] Re-ID model loaded successfully from", OSNET_MODEL_PATH)
                except Exception as e:
                    print("[osnet] Failed to load Re-ID model:", e)
    return _osnet_instance

def update_active_gallery(emp_id, name, body_crop_bgr, camera_id="it"):
    """Locks OSNet clothing features into active room gallery upon verified face match.

    Keyed by (camera_id, emp_id), not emp_id alone — a clothing signature
    captured on one camera must never be matched against a body crop from a
    different camera (see match_active_gallery's camera_id requirement).
    """
    extractor = get_osnet_extractor()
    if extractor is None or body_crop_bgr is None:
        return
    reid_vec = extractor.extract_crop(body_crop_bgr)
    if reid_vec is None:
        return
    now = time.time()
    key = (camera_id, emp_id)
    with _gallery_lock:
        if key in _active_room_gallery:
            prev = _active_room_gallery[key]["reid_vec"]
            new_vec = 0.75 * prev + 0.25 * reid_vec
            norm = np.linalg.norm(new_vec)
            if norm > 1e-6:
                new_vec /= norm
            _active_room_gallery[key]["reid_vec"] = new_vec
            _active_room_gallery[key]["last_seen"] = now
            _active_room_gallery[key]["name"] = name
        else:
            _active_room_gallery[key] = {
                "name": name,
                "reid_vec": reid_vec,
                "last_seen": now,
                "camera_id": camera_id
            }

def match_active_gallery(body_crop_bgr, camera_id, threshold=REID_MATCH_THRESHOLD):
    """Matches a body crop (front or back view) against THIS camera's own
    active-appearance gallery only — camera_id is required, not optional,
    so a clothing match can never cross from one camera's feed to another's.
    """
    extractor = get_osnet_extractor()
    if extractor is None or body_crop_bgr is None:
        return None, None, 0.0
    vec = extractor.extract_crop(body_crop_bgr)
    if vec is None:
        return None, None, 0.0
    best_emp, best_name, best_sim = None, None, 0.0
    now = time.time()
    with _gallery_lock:
        for (cam_id, emp_id), info in list(_active_room_gallery.items()):
            if cam_id != camera_id:
                continue
            if (now - info["last_seen"]) > 7200:
                continue
            sim = float(vec @ info["reid_vec"])
            if sim > best_sim:
                best_emp, best_name, best_sim = emp_id, info["name"], sim
    if best_sim >= threshold:
        return best_emp, best_name, best_sim
    return None, None, best_sim


_pool = None
_pool_lock = threading.Lock()


def db():
    """Returns a pooled MySQL connection."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = pooling.MySQLConnectionPool(
                    pool_name="ken_attendance", pool_size=8,
                    pool_reset_session=False, **DB)
    return _pool.get_connection()


_templates_lock = threading.Lock()
_templates = {"ids": [], "names": [], "matrix": None, "id_array": None,
              "read_at": 0.0}


def load_templates(force=False):
    now = time.time()
    with _templates_lock:
        fresh = (now - _templates["read_at"]) < TEMPLATE_CACHE_SECONDS
        if not force and fresh and _templates["read_at"] > 0:
            return (_templates["ids"], _templates["names"],
                    _templates["matrix"], _templates["id_array"])

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT t.emp_id, e.name, t.embedding "
                "FROM templates t JOIN employees e ON t.emp_id = e.emp_id;")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if rows:
        ids = [r[0] for r in rows]
        names = [r[1] for r in rows]
        matrix = np.array([json.loads(r[2]) for r in rows], dtype=np.float32)
        id_array = np.array([str(i) for i in ids])
    else:
        ids, names, matrix, id_array = [], [], None, None

    with _templates_lock:
        _templates.update(ids=ids, names=names, matrix=matrix,
                          id_array=id_array, read_at=now)
    return ids, names, matrix, id_array


_learn_last = {}
_learn_lock = threading.Lock()


def learn_angle(emp_id, name, embedding, score):
    """Stores a confidently recognised but unfamiliar view as a new template."""
    if not AUTO_LEARN:
        return False
    if score < AUTO_LEARN_MIN_SCORE or score > AUTO_LEARN_MAX_SCORE:
        return False

    now = time.time()
    with _learn_lock:
        if now - _learn_last.get(emp_id, 0) < AUTO_LEARN_COOLDOWN:
            return False
        _learn_last[emp_id] = now

    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(template_no), 0), COUNT(*) "
                    "FROM templates WHERE emp_id=%s;", (emp_id,))
        highest, held = cur.fetchone()
        if held >= MAX_TEMPLATES_PER_PERSON:
            cur.close()
            conn.close()
            return False

        vec = np.asarray(embedding, dtype=np.float32)
        vec = vec / np.linalg.norm(vec)
        cur.execute("INSERT INTO templates (emp_id, template_no, embedding, label) "
                    "VALUES (%s,%s,%s,%s);",
                    (emp_id, int(highest) + 1, json.dumps(vec.tolist()), "learned"))
        conn.commit()
        cur.close()
        conn.close()
        invalidate_templates()
        print("[learn] " + str(name) + " (" + str(emp_id) + "): new angle stored, "
              "score " + ("%.2f" % score) + ", now holding " + str(held + 1))
        return True
    except Exception as e:
        print("[learn] could not store an angle for " + str(emp_id) + ": " + str(e))
        return False


def invalidate_templates():
    """Call after any enrol or delete so the next detection pass re-reads."""
    with _templates_lock:
        _templates["read_at"] = 0.0


_state_lock = threading.Lock()
_last_punch = {}


def day_bounds(day):
    """Half-open [start, next_day) for a date — the sargable form."""
    if isinstance(day, str):
        day = datetime.strptime(day, "%Y-%m-%d").date()
    start = datetime.combine(day, datetime.min.time())
    return start, start + timedelta(days=1)


def attendance_day(now=None):
    """The date a punch belongs to."""
    if now is None:
        now = datetime.now(IST).replace(tzinfo=None)
    return now.date()


def _seed_last_punch(emp_id, day):
    """Reads this person's most recent punch FOR THE GIVEN DAY."""
    conn = db()
    cur = conn.cursor()
    start, end = day_bounds(day)
    cur.execute("SELECT ts, direction, camera FROM events "
                "WHERE emp_id=%s AND ts >= %s AND ts < %s "
                "ORDER BY ts DESC LIMIT 1;", (emp_id, start, end))
    row = cur.fetchone()
    cur.close()
    conn.close()
    value = (row[0], row[1], day, row[2] or "") if row else (None, None, day, "")
    with _state_lock:
        _last_punch[emp_id] = value
    return value


def _current_state(emp_id, day):
    """Cached last punch for today, re-read from the database if the cache is empty or was filled o..."""
    with _state_lock:
        cached = _last_punch.get(emp_id)
    if cached is None or cached[2] != day:
        cached = _seed_last_punch(emp_id, day)
    return cached[0], cached[1], cached[3]


_presence_lock = threading.Lock()
_presence_written = {}


def touch_presence(emp_id, name, now):
    """Records that this person was seen, without creating a punch."""
    key = str(emp_id)
    stamp = time.time()
    with _presence_lock:
        last = _presence_written.get(key, 0)
        if stamp - last < PRESENCE_WRITE_SECONDS:
            return False
        _presence_written[key] = stamp

    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO presence (emp_id, name, day, first_seen, last_seen, sightings) "
                "VALUES (%s,%s,%s,%s,%s,1) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), "
                "last_seen=VALUES(last_seen), sightings=sightings+1;",
                (emp_id, name, now.date(), now, now))
    conn.commit()
    cur.close()
    conn.close()
    return True


def _write_punch(emp_id, name, score, direction, now, source="camera", camera=""):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO events (emp_id, name, ts, direction, score, source, camera) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s);",
                (emp_id, name, now, direction, float(score), source, camera))
    conn.commit()
    cur.close()
    conn.close()
    with _state_lock:
        _last_punch[emp_id] = (now, direction, attendance_day(now), camera)


def close_session(emp_id, name, at, source, camera=""):
    """Writes the OUT that closes this person's open IN, or does nothing."""
    day = attendance_day(at)

    conn = db()
    cur = conn.cursor()
    start, end = day_bounds(day)
    cur.execute("SELECT ts, direction, camera FROM events "
                "WHERE emp_id=%s AND ts >= %s AND ts < %s "
                "ORDER BY ts DESC LIMIT 1;", (emp_id, start, end))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or row[1] != "IN":
        with _state_lock:
            if row:
                _last_punch[emp_id] = (row[0], row[1], day, row[2] or "")
        return False

    in_ts = row[0]
    if at <= in_ts:
        at = in_ts + timedelta(seconds=1)

    _write_punch(emp_id, name, 0.0, "OUT", at, source=source, camera=camera)
    return True


TRACK_MOVES = getattr(_local, "TRACK_MOVES", False)

MOVE_QUIET_SECONDS = getattr(_local, "MOVE_QUIET_SECONDS", 3)

_move_lock = threading.Lock()
_seen_on = {}


def note_seen_on(emp_id, camera):
    """Records that this camera saw this person just now."""
    if not camera:
        return
    with _move_lock:
        _seen_on[(str(emp_id), camera)] = time.time()


def quiet_on(emp_id, camera):
    """Seconds since that camera last saw them; inf if it never has."""
    if not camera:
        return float("inf")
    with _move_lock:
        last = _seen_on.get((str(emp_id), camera))
    return float("inf") if last is None else (time.time() - last)


def clear_moves(emp_id):
    with _move_lock:
        for key in [k for k in _seen_on if k[0] == str(emp_id)]:
            _seen_on.pop(key, None)


def latest_seen_camera(emp_id, fallback=""):
    """Whichever camera most recently recognised this person, for anyone
    already checked in. record_attendance_event() calls note_seen_on() on
    EVERY camera that sees them, all day, regardless of where their IN
    punch came from (a "move" between cameras doesn't write a new punch —
    see TRACK_MOVES). So the "Department" shown for someone in the office
    should come from here, not from their IN punch's camera — otherwise
    someone who punched IN at one location and later walked into a
    different one (e.g. Anita Godown -> IT) keeps showing the OLD location
    for the rest of the day, even though every camera has been correctly
    recognising them at the new one all along."""
    best_cam, best_ts = None, 0.0
    with _move_lock:
        for (eid, cam), ts in _seen_on.items():
            if eid == str(emp_id) and ts > best_ts:
                best_cam, best_ts = cam, ts
    return best_cam or fallback


def record_attendance_event(emp_id, name, score, camera="", presence_min_score=None,
                            allow_new_in=True):
    """Handles a single recognition.

    allow_new_in=False is for a camera that is not allowed to originate a
    fresh IN for someone who isn't already checked in (an "OUT only"
    doorway camera, e.g. Unit 2). It still touches presence/last-seen
    above, and still returns "present" for someone already IN — it only
    refuses to be the one that punches a brand-new IN into existence.
    """
    now = datetime.now(IST).replace(tzinfo=None)
    presence_floor = PRESENCE_MIN_SCORE if presence_min_score is None else presence_min_score

    if score >= presence_floor:
        touch_presence(emp_id, name, now)
        note_seen_on(emp_id, camera)

    last_ts, last_direction, last_camera = _current_state(emp_id,
                                                          attendance_day(now))

    if last_direction == "IN":
        same_place = (not camera) or (camera == last_camera)
        if same_place or not TRACK_MOVES:
            return {"status": "present", "emp_id": emp_id, "name": name}

        if score < PRESENCE_MIN_SCORE:
            return {"status": "present", "emp_id": emp_id, "name": name}

        away = quiet_on(emp_id, last_camera)
        if away < MOVE_QUIET_SECONDS:
            return {"status": "present", "emp_id": emp_id, "name": name}

        if away == float("inf"):
            seen = get_presence_for_date(attendance_day(now).strftime("%Y-%m-%d"))
            row = seen.get(emp_id) or {}
            last_seen = row.get("last_seen")
            away = (now - last_seen).total_seconds() if last_seen else 0.0
            if away < MOVE_QUIET_SECONDS:
                return {"status": "present", "emp_id": emp_id, "name": name}

        disarm_exit(emp_id, name)
        left_at = now - timedelta(seconds=min(away, 3600.0))
        close_session(emp_id, name, left_at, "moved", last_camera)

        if not allow_new_in:
            print("[move] " + str(name) + ": " + camera_name(last_camera)
                  + " -> OUT (seen at " + camera_name(camera) + ", an OUT-only camera)")
            return {"status": "recorded", "emp_id": emp_id, "name": name,
                    "direction": "OUT", "time": left_at.strftime("%H:%M:%S"),
                    "moved_from": last_camera}

        _write_punch(emp_id, name, score, "IN", now, camera=camera)
        print("[move] " + str(name) + ": " + camera_name(last_camera)
              + " -> " + camera_name(camera))
        return {"status": "recorded", "emp_id": emp_id, "name": name,
                "direction": "IN", "time": now.strftime("%H:%M:%S"),
                "moved_from": last_camera}

    if last_ts is not None and (now - last_ts).total_seconds() < DEDUP_SECONDS:
        return {"status": "cooldown", "emp_id": emp_id, "name": name,
                "remaining": int(DEDUP_SECONDS - (now - last_ts).total_seconds())}

    if not allow_new_in:
        return {"status": "ignored", "emp_id": emp_id, "name": name}

    clear_moves(emp_id)
    _write_punch(emp_id, name, score, "IN", now, camera=camera)
    return {"status": "recorded", "emp_id": emp_id, "name": name,
            "direction": "IN", "time": now.strftime("%H:%M:%S")}


_exit_lock = threading.Lock()
_exit_armed = {}

EXIT_ARM_MIN_HOLD_SECONDS = getattr(_local, "EXIT_ARM_MIN_HOLD_SECONDS", 3.0)
_exit_candidate_lock = threading.Lock()
_exit_candidates = {}


def exit_condition_held(emp_id, hold_seconds=None):
    """True once 'at the exit' has been continuously true for the hold time."""
    hold = EXIT_ARM_MIN_HOLD_SECONDS if hold_seconds is None else hold_seconds
    now = time.time()
    with _exit_candidate_lock:
        first = _exit_candidates.get(emp_id)
        if first is None:
            _exit_candidates[emp_id] = now
            return False
        return (now - first) >= hold


def exit_condition_reset(emp_id):
    with _exit_candidate_lock:
        _exit_candidates.pop(emp_id, None)


def arm_exit(emp_id, name, camera):
    """Notes that this person is at the exit, close to the camera."""
    with _exit_lock:
        if emp_id in _exit_armed:
            return False
        _exit_armed[emp_id] = {"at": datetime.now(IST).replace(tzinfo=None),
                               "name": name, "camera": camera}
    print("[exit] " + str(name) + " is at the exit — watching")
    return True


def disarm_exit(emp_id, name=""):
    """Cancels a pending exit because the person is back in the room."""
    with _exit_lock:
        armed = _exit_armed.pop(emp_id, None)
    exit_condition_reset(emp_id)
    if armed:
        print("[exit] " + str(name or armed["name"]) + " came back — exit cancelled")
    return bool(armed)


def armed_count():
    with _exit_lock:
        return len(_exit_armed)


def confirm_exits():
    """Writes OUT for anyone who armed the exit and then stopped being seen."""
    now = datetime.now(IST).replace(tzinfo=None)
    day = attendance_day(now)
    presence = get_presence_for_date(day.strftime("%Y-%m-%d"))

    with _exit_lock:
        pending = list(_exit_armed.items())

    written = 0
    for emp_id, armed in pending:
        seen = presence.get(emp_id, {})
        seen_last = seen.get("last_seen")
        last_seen = max(seen_last, armed["at"]) if seen_last else armed["at"]
        quiet = (now - last_seen).total_seconds()
        armed_for = (now - armed["at"]).total_seconds()

        if quiet < EXIT_CONFIRM_SECONDS:
            if armed_for > EXIT_ARM_EXPIRY:
                disarm_exit(emp_id, armed["name"])
            continue

        if close_session(emp_id, armed["name"], last_seen, "exit",
                         armed["camera"]):
            written += 1
            print("[exit] " + str(armed["name"]) + " marked OUT at "
                  + last_seen.strftime("%H:%M:%S")
                  + " — passed the exit zone and did not come back")
        disarm_exit(emp_id, armed["name"])
    return written


def close_end_of_day():
    """The safety net, and the only time-based rule left."""
    now = datetime.now(IST).replace(tzinfo=None)
    today = attendance_day(now)
    today_start, today_end = day_bounds(today)
    closed = 0

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT e.emp_id, e.name, e.ts, p.last_seen "
        "FROM events e "
        "JOIN ( SELECT emp_id, DATE(ts) AS d, MAX(ts) AS ts FROM events "
        "       WHERE ts < %s GROUP BY emp_id, DATE(ts) ) latest "
        "  ON latest.emp_id = e.emp_id AND latest.ts = e.ts "
        "LEFT JOIN presence p ON p.emp_id = e.emp_id AND p.day = DATE(e.ts) "
        "WHERE e.ts < %s AND e.direction='IN';",
        (today_start, today_start))
    older = cur.fetchall()

    todays = []
    if (now.hour, now.minute) >= DAY_ENDS_AT:
        cur.execute(
            "SELECT e.emp_id, e.name, e.ts, p.last_seen "
            "FROM events e "
            "JOIN ( SELECT emp_id, MAX(ts) AS ts FROM events "
            "       WHERE ts >= %s AND ts < %s GROUP BY emp_id ) latest "
            "  ON latest.emp_id = e.emp_id AND latest.ts = e.ts "
            "LEFT JOIN presence p ON p.emp_id = e.emp_id AND p.day = %s "
            "WHERE e.ts >= %s AND e.ts < %s AND e.direction='IN';",
            (today_start, today_end, today, today_start, today_end))
        todays = cur.fetchall()

    cur.close()
    conn.close()

    for emp_id, name, in_ts, last_seen in list(older) + list(todays):
        seen_at = max(last_seen, in_ts) if last_seen else in_ts
        if close_session(emp_id, name, seen_at, "eod"):
            closed += 1
            print("[day-end] " + str(name) + " closed at "
                  + seen_at.strftime("%H:%M:%S") + " — never seen leaving")
        disarm_exit(emp_id, name)

    return closed


def auto_checkout_stale_presence():
    """Auto-closes people whose current IN camera cannot reliably confirm a
    real exit (a room-view camera with no doorway/zone signal strong enough
    to arm), once they've been unseen for that camera's own timeout.

    Scoped per-camera on purpose: only a camera whose "settings" carries
    stale_out_minutes is affected. Unit 1 and Anita Godown have no such
    setting, so nothing here changes their behaviour — their exit-zone /
    doorway detection is already reliable and this must not race it or
    close someone who is just sitting outside any camera's view.

    Switched off by AUTO_CHECKOUT_ENABLED=False (this deployment's setting,
    in local_settings.py) -- management asked for the "AUTO" OUT punches
    turned off after they kept firing seconds after a real IN. The person
    just stays marked IN on that camera until something reliable closes
    them out (they punch OUT for real, the exit-zone/doorway confirms it,
    or day-end at 20:00 catches an abandoned IN as a safety net).
    """
    if not AUTO_CHECKOUT_ENABLED:
        return 0

    now = datetime.now(IST).replace(tzinfo=None)
    today = attendance_day(now)
    today_start, today_end = day_bounds(today)

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT e.emp_id, e.name, e.ts, e.camera, p.last_seen "
        "FROM events e "
        "JOIN ( SELECT emp_id, MAX(ts) AS ts FROM events "
        "       WHERE ts >= %s AND ts < %s GROUP BY emp_id ) latest "
        "  ON latest.emp_id = e.emp_id AND latest.ts = e.ts "
        "LEFT JOIN presence p ON p.emp_id = e.emp_id AND p.day = %s "
        "WHERE e.ts >= %s AND e.ts < %s AND e.direction='IN';",
        (today_start, today_end, today, today_start, today_end))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    closed = 0
    for emp_id, name, in_ts, camera, last_seen in rows:
        if not camera:
            continue
        cam = next((c for c in CAMERAS if c["id"] == camera), None)
        if cam is None:
            continue
        minutes = cam.get("settings", {}).get("stale_out_minutes")
        if not minutes:
            continue
        seen_at = max(last_seen, in_ts) if last_seen else in_ts
        quiet = (now - seen_at).total_seconds() / 60.0
        if quiet < minutes:
            continue
        if close_session(emp_id, name, seen_at, "auto", camera):
            closed += 1
            print("[stale-auto] " + str(name) + " marked OUT at "
                  + seen_at.strftime("%H:%M:%S") + " — unseen "
                  + str(int(quiet)) + " min on " + camera_name(camera))
        disarm_exit(emp_id, name)
    return closed


def exit_loop():
    """Runs the exit confirmer on a short timer, and says what it did."""
    while True:
        try:
            sweep_all_trackers()
            confirm_exits()
            auto_checkout_stale_presence()
            close_end_of_day()
        except Exception as e:
            import traceback
            print("[exit-loop] FAILED: " + repr(e))
            traceback.print_exc()
        time.sleep(EXIT_CHECK_SECONDS)


def forget_person_state(emp_id):
    with _state_lock:
        _last_punch.pop(emp_id, None)
    clear_moves(emp_id)
    disarm_exit(emp_id)


def person_photo_dir(emp_id):
    return os.path.join(PHOTOS_DIR, str(emp_id))


def list_person_photos(emp_id):
    """Sorted [(index, filepath)] for every photo on disk for this person."""
    folder = person_photo_dir(emp_id)
    if not os.path.isdir(folder):
        return []
    files = [f for f in os.listdir(folder) if f.lower().endswith(".jpg")]
    out = []
    for f in files:
        try:
            out.append((int(os.path.splitext(f)[0]), os.path.join(folder, f)))
        except ValueError:
            continue
    out.sort(key=lambda t: t[0])
    return out


_size_history = {}
_size_median = {}
_size_lock = threading.Lock()


def note_face_size(cam_id, emp_id, pct):
    """Remembers how big this person's face usually looks to this camera."""
    if pct <= 0:
        return
    with _size_lock:
        key = (cam_id, str(emp_id))
        seen = _size_history.get(key)
        if seen is None:
            seen = deque(maxlen=60)
            _size_history[key] = seen
        seen.append(pct)
        _size_median.pop(key, None)


def typical_face_size(cam_id, emp_id):
    """The median of recent sightings, or None until there are enough."""
    with _size_lock:
        key = (cam_id, str(emp_id))
        cached = _size_median.get(key)
        if cached is not None:
            return cached
        held = _size_history.get(key)
        if held is None or len(held) < 6:
            return None
        ordered = sorted(held)
        median = ordered[len(ordered) // 2]
        _size_median[key] = median
        return median


def looks_close(cam_id, emp_id, pct, min_pct=None):
    """Whether this face is big enough to mean the person is at the camera."""
    floor = EXIT_MIN_FACE_PCT if min_pct is None else min_pct
    typical = typical_face_size(cam_id, emp_id)
    if typical:
        return pct >= max(floor, typical * 1.6)
    return pct >= floor


def face_size_pct(y1, y2, frame_h):
    """Face box height as a fraction of the frame height — the single-frame proxy for how close the..."""
    if frame_h <= 0:
        return 0.0
    return max(0.0, (y2 - y1) / float(frame_h))


def in_zone(x1, y1, x2, y2, frame_w, frame_h, zone_pct):
    """True if the CENTRE of the face box falls inside the given zone."""
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    return (zone_pct[0] * frame_w <= cx <= zone_pct[2] * frame_w
            and zone_pct[1] * frame_h <= cy <= zone_pct[3] * frame_h)


def box_iou(a, b):
    """Overlap between two boxes, 0."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    area_a = max(1.0, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1.0, (bx2 - bx1) * (by2 - by1))
    return inter / (area_a + area_b - inter)


def box_contains(outer, inner):
    """Fraction of the inner box that falls inside the outer one."""
    ix1, iy1 = max(outer[0], inner[0]), max(outer[1], inner[1])
    ix2, iy2 = min(outer[2], inner[2]), min(outer[3], inner[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_inner = max(1.0, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return inter / area_inner


def box_contains_expanded(outer, inner):
    """Fraction of inner (face) inside outer (person), with outer expanded for seated/head posture misalignments."""
    pw = max(1.0, outer[2] - outer[0])
    ph = max(1.0, outer[3] - outer[1])
    exp_outer = (outer[0] - 0.20 * pw, outer[1] - 0.40 * ph,
                 outer[2] + 0.20 * pw, outer[3] + 0.10 * ph)
    return box_contains(exp_outer, inner)


def extract_appearance_descriptor(frame, box):
    """Extracts a normalized 256-d spatial HSV color-texture descriptor of top and bottom clothing."""
    if frame is None or frame.size == 0:
        return None
    x1, y1, x2, y2 = [int(v) for v in box]
    h, w = frame.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0 or crop.shape[0] < 12 or crop.shape[1] < 12:
        return None
    try:
        resized = cv2.resize(crop, (60, 120), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
        top = hsv[:60, :]
        bot = hsv[60:, :]
        hist_top = cv2.calcHist([top], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256])
        hist_bot = cv2.calcHist([bot], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256])
        vec = np.concatenate([hist_top.flatten(), hist_bot.flatten()])
        norm = np.linalg.norm(vec)
        if norm <= 0:
            return None
        return vec / norm
    except Exception:
        return None


_yolo_lock = threading.Lock()
_yolo_models = {}
_yolo_failed = {"why": None}


def load_yolo(cam_id="shared"):
    """Loads YOLO once, or records why it could not be loaded."""
    with _yolo_lock:
        if cam_id in _yolo_models:
            return _yolo_models[cam_id]
        if _yolo_failed["why"]:
            return None
        try:
            from ultralytics import YOLO
            model = YOLO(YOLO_MODEL)
            _yolo_models[cam_id] = model
            print("[yolo] loaded " + YOLO_MODEL + " for " + str(cam_id))
            return model
        except Exception as e:
            _yolo_failed["why"] = str(e)
            print("[yolo] NOT loaded (" + str(e) + "). Falling back to the "
                  "face-only exit rule. Install with:  pip install ultralytics")
            return None


class PersonTrack:
    """One person as the detector follows them, named or not yet named."""

    __slots__ = ("track_id", "emp_id", "name", "score", "box", "first_at",
                 "last_at", "in_door", "door_at", "seen_room", "punched_in")

    def __init__(self, track_id, box, now):
        self.track_id = track_id
        self.emp_id = None
        self.name = None
        self.score = 0.0
        self.box = box
        self.first_at = now
        self.last_at = now
        self.in_door = False
        self.door_at = 0.0
        self.seen_room = False
        self.punched_in = False

    @property
    def named(self):
        return self.emp_id is not None


class CameraTracker:
    """Person tracking for one camera."""

    def __init__(self, cam):
        self.cam = cam
        self.lock = threading.Lock()
        self.model_lock = threading.Lock()
        self.tracks = {}
        self.recent_named_history = {}
        self.door_zone = cam.get("door_zone", cam.get("exit_zone",
                                                      DEFAULT_EXIT_ZONE))

    def _inherit_identity_if_overlapping(self, track, now, frame=None):
        """Strict: No identity guessing without face recognition. Tracks remain unnamed until InsightFace verifies identity."""
        pass


    def update(self, frame, sx=1.0, sy=1.0):
        """Runs the detector and refreshes every track."""
        model = load_yolo(self.cam["id"])
        if model is None:
            return []
        with self.model_lock:
            results = model.track(frame, persist=True, classes=[0],
                                  conf=YOLO_CONF, verbose=False,
                                  tracker="bytetrack.yaml")
        if not results:
            return []

        boxes = results[0].boxes
        if boxes is None or boxes.id is None:
            return []

        fh, fw = int(frame.shape[0] * sy), int(frame.shape[1] * sx)
        now = time.time()
        drawable = []
        named_now = []

        ids = boxes.id.int().tolist()
        coords = boxes.xyxy.tolist()

        with self.lock:
            for track_id, xyxy in zip(ids, coords):
                box = (float(xyxy[0] * sx), float(xyxy[1] * sy),
                       float(xyxy[2] * sx), float(xyxy[3] * sy))
                track = self.tracks.get(track_id)
                if track is None:
                    track = PersonTrack(track_id, box, now)
                    self.tracks[track_id] = track
                track.box = box
                track.last_at = now

                at_door = in_zone(box[0], box[1], box[2], box[3], fw, fh,
                                  self.door_zone)
                track.in_door = at_door
                if at_door:
                    track.door_at = now
                else:
                    track.seen_room = True

                if track.named:
                    named_now.append((track.emp_id, track.name))
                    self.recent_named_history[track.emp_id] = {
                        "box": box, "name": track.name, "score": track.score, "last_seen": now
                    }

                drawable.append({
                    "box": [int(v) for v in box],
                    "label": ((str(track.name) + " (" + str(track.emp_id) + ")")
                              if track.named
                              else ("unidentified #" + str(track_id))),
                    "named": track.named,
                    "at_door": at_door,
                })

        stamp = datetime.now(IST).replace(tzinfo=None)
        for emp_id, name in named_now:
            try:
                touch_presence(emp_id, name, stamp)
                note_seen_on(emp_id, self.cam["id"])
            except Exception as e:
                print("[track] presence update failed for "
                      + str(emp_id) + ": " + str(e))
        return drawable


    def attach_identity(self, face_box, emp_id, name, score, frame=None):
        """Puts a name on whichever track this recognised face sits inside strictly from InsightFace face recognition."""
        best, best_fit = None, 0.0
        with self.lock:
            for track in self.tracks.values():
                fit = box_contains_expanded(track.box, face_box)
                if fit > best_fit:
                    best, best_fit = track, fit

            if best is None or best_fit < FACE_IN_PERSON_MIN:
                return None

            for t in list(self.tracks.values()):
                if t.track_id != best.track_id and t.emp_id == emp_id:
                    t.emp_id = None
                    t.name = None

            best.emp_id = emp_id
            best.name = name
            best.score = max(score, best.score)
            now = time.time()
            self.recent_named_history[emp_id] = {
                "box": best.box, "name": name, "score": score, "last_seen": now
            }
            
            now_dt = datetime.now(IST).replace(tzinfo=None)
            _, last_dir, _ = _current_state(emp_id, attendance_day(now_dt))
            if last_dir == "IN":
                best.seen_room = True

            return best

    def find_named_track(self, face_box):
        """Returns named PersonTrack if face_box falls inside/near a named track."""
        with self.lock:
            for track in self.tracks.values():
                if track.named:
                    fit = box_contains_expanded(track.box, face_box)
                    if fit >= 0.25:
                        return track
        return None


    def sweep(self):
        """Closes tracks that have gone, and punches OUT the ones that left."""
        now = time.time()
        leaving = []

        with self.lock:
            for track_id, track in list(self.tracks.items()):
                gone_for = now - track.last_at
                if gone_for < TRACK_LOST_SECONDS:
                    continue

                del self.tracks[track_id]

                if not track.named:
                    continue
                if not track.seen_room:
                    continue
                if track.door_at == 0.0:
                    continue
                if (now - track.door_at) > 30.0:
                    continue

                leaving.append((track.emp_id, track.name, track.last_at))

        written = 0
        for emp_id, name, last_at in leaving:
            at = datetime.fromtimestamp(last_at, IST).replace(tzinfo=None)
            if close_session(emp_id, name, at, "track", self.cam["id"]):
                written += 1
                disarm_exit(emp_id, name)
                print("[track] " + str(name) + " OUT at "
                      + at.strftime("%H:%M:%S")
                      + " — walked through the doorway and did not return")
        return written

    def named_count(self):
        with self.lock:
            return sum(1 for t in self.tracks.values() if t.named)

    def count(self):
        with self.lock:
            return len(self.tracks)


_trackers = {}
_trackers_lock = threading.Lock()


def get_tracker(cam):
    with _trackers_lock:
        if cam["id"] not in _trackers:
            _trackers[cam["id"]] = CameraTracker(cam)
        return _trackers[cam["id"]]


def sweep_all_trackers():
    total = 0
    with _trackers_lock:
        every = list(_trackers.values())
    for tracker in every:
        try:
            total += tracker.sweep()
        except Exception as e:
            print("[track] sweep failed for " + tracker.cam["id"] + ": " + str(e))
    return total


def reread_face(frame, x1, y1, x2, y2, app):
    """A fresh embedding taken from the full-resolution frame, or None."""
    h, w = frame.shape[:2]
    bw, bh = x2 - x1, y2 - y1
    pad = int(max(bw, bh) * REREAD_PAD)
    cx1, cy1 = max(0, x1 - pad), max(0, y1 - pad)
    cx2, cy2 = min(w, x2 + pad), min(h, y2 + pad)
    crop = frame[cy1:cy2, cx1:cx2]
    if crop.size == 0:
        return None

    ch = crop.shape[0]
    if ch < 320:
        scale = 320.0 / ch
        crop = cv2.resize(crop, (int(crop.shape[1] * scale), 320),
                          interpolation=cv2.INTER_CUBIC)
    try:
        # BUG FIX (11 Sep): this call used to run outside _face_model_lock.
        # face_app/exit_app are shared by every camera's room_detector AND
        # exit_detector thread (six cameras' worth of loops on the two
        # models). The lock exists precisely because insightface's detector
        # keeps small internal caches with no locking of its own — two
        # threads calling .get() on the same model at the same instant can
        # corrupt each other's result, and the failure mode is silent: a
        # clean, well-lit, front-on face comes back with zero detections,
        # not an error. Every other .get() call on these two shared models
        # already went through this lock; this was the one left out, and it
        # ran on every re-read of a small/far face — exactly the doorway
        # cameras' most common case. A person recognised fine on one camera
        # (own detection pass never overlapped another thread's) while
        # never once catching on another (constant contention from the
        # other camera's own loop) is the exact symptom this produces.
        with _face_model_lock:
            again = app.get(crop)
    except Exception:
        return None
    if not again:
        return None
    again.sort(key=lambda f: (f.bbox[3] - f.bbox[1]), reverse=True)
    return again[0]


def resize_for_detection(img, max_dim=None):
    """Shrink oversized frames before face detection to cut CPU time."""
    limit = MAX_DETECTION_DIM if max_dim is None else max_dim
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest > limit:
        scale = limit / longest
        img = cv2.resize(img, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA)
    return img


_MODULES = ['detection', 'recognition']

face_app = insightface.app.FaceAnalysis(name='buffalo_l', allowed_modules=_MODULES)
face_app.prepare(ctx_id=-1, det_size=ROOM_DET_SIZE, det_thresh=FACE_DET_THRESH)

exit_app = insightface.app.FaceAnalysis(name='buffalo_l', allowed_modules=_MODULES)
exit_app.prepare(ctx_id=-1, det_size=EXIT_DET_SIZE, det_thresh=FACE_DET_THRESH)

# face_app/exit_app are shared by every room camera's detector thread and
# every exit-camera thread — now six cameras' worth, each running both,
# so twelve loops non-stop. insightface's detector keeps small internal
# caches that are not written with any locking, so two threads calling
# .get() on the same model at the same moment can corrupt each other's
# result — usually surfacing as a clean, well-lit photo silently coming
# back with zero faces. Every call below goes through this one lock so
# only one thread ever runs a model at a time.
_face_model_lock = threading.Lock()

# Enrolment and the other on-demand, click-and-wait endpoints (the Enrol
# page's instant per-photo check, the webcam test page) used to share the
# lock and model above with all twelve of those always-on camera loops.
# With that many loops constantly
# re-grabbing the lock, a person clicking "Enrol" could end up waiting in
# a long queue behind live camera detections — sometimes long enough that
# it looked like enrolling had simply stopped working. This second model
# and lock exist only for the interactive endpoints below, so enrolling
# someone never has to wait on the live cameras.
interactive_app = insightface.app.FaceAnalysis(name='buffalo_l', allowed_modules=_MODULES)
interactive_app.prepare(ctx_id=-1, det_size=INTERACTIVE_DET_SIZE, det_thresh=FACE_DET_THRESH)
_interactive_model_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=start_all_cameras, daemon=True).start()
    threading.Thread(target=exit_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


ip_cam_lock = threading.Lock()


def _blank_state():
    return {
        "phase": "idle",
        "running": False,
        "url": None,
        "thread": None,
        "stop_flag": False,
        "latest_jpeg": None,
        "viewers": 0,
        "faces": [],
        "error": None,
        "room_error": None,
        "exit_error": None,
        "found_brand": None,
        "track_error": None,
        "timing": {"room_ms": 0, "exit_ms": 0, "exit_ran": 0, "exit_skipped": 0,
                   "faces": 0, "track_ms": 0, "tracks": 0},
    }


ip_cam_states = {cam["id"]: _blank_state() for cam in CAMERAS}


def cam_state(cam_id):
    with ip_cam_lock:
        if cam_id not in ip_cam_states:
            ip_cam_states[cam_id] = _blank_state()
        return ip_cam_states[cam_id]


def camera_problems():
    """Every camera currently reporting a fault, for the Today page banner."""
    out = []
    for cam in CAMERAS:
        if not cam.get("enabled", True):
            continue
        st = cam_state(cam["id"])
        with ip_cam_lock:
            for kind, msg in (("stream", st["error"]),
                              ("room recognition", st["room_error"]),
                              ("exit recognition", st["exit_error"]),
                              ("person tracking", st["track_error"])):
                if msg:
                    out.append(cam["name"] + " — " + kind + ": " + str(msg))
    return out


def run_recognition_pipeline(cam, holder, stop_event):
    """Everything that happens once frames are arriving, whichever way they got here."""
    st = cam_state(cam["id"])
    zone = cam.get("exit_zone", DEFAULT_EXIT_ZONE)
    recognise = cam.get("recognition", True)
    doorway = cam.get("mode") == "doorway"
    approach_in = cam.get("approach_means", "in") == "in"

    _settings = cam.get("settings", {})

    def S(key, default):
        return _settings.get(key, default)

    marks_in = S("marks_in", True)
    marks_out = S("marks_out", True)

    recent_seat_faces = {}
    seat_lock = threading.Lock()

    soft_seen = {}
    side_seen = {}
    face_frame_h = [1]
    trend = {}
    static_boxes = {}
    zone_toggle_fired = {}
    tracker = get_tracker(cam)
    person_boxes = {"boxes": []}

    def is_static(box):
        """True if this face box has not moved for a very long time."""
        key = tuple(int(v) // 12 for v in box)
        now = time.time()
        seen = static_boxes.setdefault(key, [now, 0])
        seen[1] += 1
        if len(static_boxes) > 400:
            static_boxes.clear()
        return (now - seen[0]) > STATIC_AFTER_SECONDS and seen[1] > STATIC_AFTER_HITS

    def walking_direction(emp_id, pct):
        """"approach", "recede" or None from how this face is changing size."""
        now = time.time()
        seen = trend.setdefault(emp_id, [])
        seen.append((now, pct))
        while seen and (now - seen[0][0]) > DOORWAY_TREND_WINDOW:
            seen.pop(0)
        if len(seen) < DOORWAY_TREND_SAMPLES:
            return None
        first, last = seen[0][1], seen[-1][1]
        if first <= 0:
            return None
        change = (last - first) / first
        if change >= DOORWAY_TREND_CHANGE:
            trend.pop(emp_id, None)
            return "approach"
        if change <= -DOORWAY_TREND_CHANGE:
            trend.pop(emp_id, None)
            return "recede"
        return None

    def identify(face, ids, names, matrix, id_array, pct=1.0):
        """Best match for this face, or (None, None, score) if not confident."""
        if matrix is None:
            return None, None, 0.0

        match_threshold = S("match_threshold", MATCH_THRESHOLD)
        side_match_threshold = S("side_match_threshold", SIDE_MATCH_THRESHOLD)
        match_margin = S("match_margin", MATCH_MARGIN)
        soft_match_enabled = S("soft_match_enabled", SOFT_MATCH_ENABLED)
        soft_match_threshold = S("soft_match_threshold", SOFT_MATCH_THRESHOLD)
        soft_match_confirmations = S("soft_match_confirmations", SOFT_MATCH_CONFIRMATIONS)
        soft_match_window = S("soft_match_window", SOFT_MATCH_WINDOW)

        scores = matrix @ face.normed_embedding
        best = int(scores.argmax())
        score = float(scores[best])
        emp_id, name = ids[best], names[best]

        rival_mask = id_array != str(emp_id)
        rival = float(scores[rival_mask].max()) if rival_mask.any() else 0.0
        margin = score - rival

        is_side = False
        if hasattr(face, "kps") and face.kps is not None and len(face.kps) >= 5:
            kps = face.kps
            d_left = float(np.linalg.norm(kps[0] - kps[2]))
            d_right = float(np.linalg.norm(kps[1] - kps[2]))
            if d_left > 0 and d_right > 0:
                ratio = max(d_left, d_right) / max(1e-5, min(d_left, d_right))
                if ratio > 1.6:
                    is_side = True

        need_score = side_match_threshold if is_side else match_threshold

        if SCORE_DEBUG:
            k = min(3, scores.shape[0])
            idx = np.argpartition(-scores, k - 1)[:k]
            idx = idx[np.argsort(-scores[idx])]
            top = [(names[i], float(scores[i])) for i in idx]
            if score < need_score:
                verdict = ("REJECTED score %.2f < %.2f" % (score, need_score))
            elif margin < match_margin:
                verdict = ("REJECTED margin %.2f < %.2f" % (margin, match_margin))
            elif is_side:
                verdict = "score OK, confirming (side profile)"
            else:
                verdict = "accepted"
            fb = face.bbox.astype(int)
            fh_pct = (fb[3] - fb[1]) / float(max(1, face_frame_h[0]))
            print("[score] " + cam["id"] + "  " + verdict
                  + "  face " + str(int(fh_pct * 100)) + "% at "
                  + str((int(fb[0]), int(fb[1]))) + "  "
                  + " | ".join(str(n) + " " + ("%.3f" % s) for n, s in top))

        if score >= need_score and margin >= match_margin and is_side:
            now = time.time()
            seen = side_seen.get(emp_id)
            if seen is None or (now - seen[1]) > SIDE_CONFIRM_WINDOW:
                side_seen[emp_id] = [1, now]
            else:
                seen[0] += 1
                seen[1] = now
                if seen[0] >= SIDE_CONFIRM_COUNT:
                    side_seen.pop(emp_id, None)
                    soft_seen.pop(emp_id, None)
                    learn_angle(emp_id, name, face.normed_embedding, score)
                    print("[side] " + str(name) + " accepted after "
                          + str(SIDE_CONFIRM_COUNT) + " side-angle passes, score "
                          + ("%.2f" % score))
                    return emp_id, name, score
            return None, None, score

        if score >= need_score and margin >= match_margin:
            soft_seen.pop(emp_id, None)
            side_seen.pop(emp_id, None)
            learn_angle(emp_id, name, face.normed_embedding, score)
            return emp_id, name, score

        if score >= need_score and margin < match_margin:
            if not SCORE_DEBUG:
                print("[match] refused: " + str(name) + " scored "
                      + ("%.2f" % score) + " but the next person scored "
                      + ("%.2f" % rival) + " — too close to call")
            return None, None, score

        if soft_match_enabled and score >= soft_match_threshold \
                and margin >= match_margin:
            now = time.time()
            seen = soft_seen.get(emp_id)
            if seen is None or (now - seen[1]) > soft_match_window:
                soft_seen[emp_id] = [1, now]
            else:
                seen[0] += 1
                seen[1] = now
                if seen[0] >= soft_match_confirmations:
                    soft_seen.pop(emp_id, None)
                    print("[soft] " + str(name) + " accepted after "
                          + str(soft_match_confirmations) + " passes, score "
                          + ("%.2f" % score))
                    return emp_id, name, score
            return None, None, score

        return None, None, score

    def judge(emp_id, name, score, x1, y1, x2, y2, fw, fh):
        """Decides what this sighting means, and returns (event, at_exit, pct)."""
        presence_min_score = S("presence_min_score", PRESENCE_MIN_SCORE)
        exit_min_face_pct = S("exit_min_face_pct", EXIT_MIN_FACE_PCT)
        exit_arm_hold = S("exit_arm_min_hold_seconds", EXIT_ARM_MIN_HOLD_SECONDS)

        pct = face_size_pct(y1, y2, fh)
        note_face_size(cam["id"], emp_id, pct)

        at_exit = in_zone(x1, y1, x2, y2, fw, fh, zone)
        close = looks_close(cam["id"], emp_id, pct, min_pct=exit_min_face_pct)
        armed = False

        tracked = None
        if YOLO_ENABLED:
            tracked = tracker.attach_identity((x1, y1, x2, y2), emp_id, name,
                                              score)

        if doorway:
            moving = walking_direction(emp_id, pct)
            if moving == "approach" and not approach_in and marks_out:
                armed = arm_exit(emp_id, name, cam["id"])
            elif moving == "recede" and approach_in and marks_out:
                armed = arm_exit(emp_id, name, cam["id"])
            elif moving:
                disarm_exit(emp_id, name)
        else:
            instant_toggle = S("instant_toggle", False)
            if at_exit and close and score >= presence_min_score:
                if exit_condition_held(emp_id, hold_seconds=exit_arm_hold):
                    if instant_toggle:
                        if not zone_toggle_fired.get(emp_id):
                            zone_toggle_fired[emp_id] = True
                            today = attendance_day(datetime.now(IST).replace(tzinfo=None))
                            last_ts, last_direction, last_camera = _current_state(emp_id, today)
                            if last_direction == "IN":
                                now = datetime.now(IST).replace(tzinfo=None)
                                if close_session(emp_id, name, now, "auto", cam["id"]):
                                    disarm_exit(emp_id, name)
                                    armed = True
                                    print("[zone-toggle] " + str(name)
                                          + " marked OUT — big/frontal face at exit zone")
                    else:
                        armed = arm_exit(emp_id, name, cam["id"])
            else:
                exit_condition_reset(emp_id)
                disarm_exit(emp_id, name)
                zone_toggle_fired.pop(emp_id, None)

        event = record_attendance_event(emp_id, name, score, camera=cam["id"],
                                        presence_min_score=presence_min_score,
                                        allow_new_in=marks_in)
        if armed and event["status"] == "present":
            event = dict(event, status="leaving")
        return event, at_exit, pct

    def label_for(name, emp_id, event, pct, at_exit):
        text = str(name) + " (" + str(emp_id) + ")"
        colour = (87, 122, 27)
        if event is not None:
            status = event["status"]
            if status == "recorded" and event.get("direction") == "IN":
                colour = (87, 122, 27)
                text += " · IN"
            elif status == "recorded" and event.get("direction") == "OUT":
                colour = (43, 58, 178)
                text += " · OUT"
            elif status == "leaving":
                colour = (43, 58, 178)
                text += " · LEAVING"
            elif status == "present":
                colour = (87, 122, 27)
                text += " · HERE"
            elif status == "cooldown":
                colour = (87, 122, 27)
                text += " · HERE"
        if ZONE_DEBUG and emp_id:
            exit_min_face_pct = S("exit_min_face_pct", EXIT_MIN_FACE_PCT)
            usual = typical_face_size(cam["id"], emp_id)
            need = max(exit_min_face_pct,
                       usual * 1.6) if usual else exit_min_face_pct
            text += "  [" + str(int(round(pct * 100))) + "%"
            text += " need " + str(int(round(need * 100))) + "%"
            if usual:
                text += " usual " + str(int(round(usual * 100))) + "%"
            if at_exit:
                text += " AT-EXIT"
            text += "]"
        elif ZONE_DEBUG:
            text += "  [" + str(int(round(pct * 100))) + "%]"
        return text, colour

    room_faces = {"faces": []}

    def room_detector():
        while not stop_event.is_set():
            frame = holder["frame"]
            if frame is None or not holder["ok"]:
                time.sleep(0.3)
                continue
            frame = frame.copy()
            started = time.time()
            try:
                reread_below_pct = S("reread_below_pct", REREAD_BELOW_PCT)
                max_detection_dim = S("max_detection_dim", MAX_DETECTION_DIM)
                small = resize_for_detection(frame, max_dim=max_detection_dim)
                sx = frame.shape[1] / small.shape[1]
                sy = frame.shape[0] / small.shape[0]
                with _face_model_lock:
                    found = face_app.get(small)
                ids, names, matrix, id_array = load_templates()
                fh, fw = frame.shape[:2]
                face_frame_h[0] = small.shape[0]

                if SCORE_DEBUG and not found:
                    print("[face] " + cam["id"] + " no face detected in the "
                          "room pass — too small, too side-on, or too dark")

                out = []
                for face in found:
                    b = face.bbox.astype(float)
                    x1 = int(b[0] * sx); y1 = int(b[1] * sy)
                    x2 = int(b[2] * sx); y2 = int(b[3] * sy)

                    if is_static([x1, y1, x2, y2]):
                        continue

                    pct = face_size_pct(y1, y2, fh)
                    judged = face
                    if pct < reread_below_pct:
                        better = reread_face(frame, x1, y1, x2, y2, face_app)
                        if better is not None:
                            judged = better
                    emp_id, name, score = identify(judged, ids, names, matrix,
                                                   id_array, pct)
                    if emp_id is None:
                        if SHOW_UNKNOWN_BOXES and tracker.find_named_track([x1, y1, x2, y2]) is None:
                            out.append({"box": [x1, y1, x2, y2], "label": "Unknown",
                                        "color": (60, 58, 178), "event": None})
                        continue

                    event, at_exit, pct = judge(emp_id, name, score,
                                                x1, y1, x2, y2, fw, fh)
                    text, colour = label_for(name, emp_id, event, pct, at_exit)
                    face_entry = {"box": [x1, y1, x2, y2], "label": text,
                                  "color": colour, "event": event, "emp_id": emp_id}
                    out.append(face_entry)

                    with seat_lock:
                        recent_seat_faces[emp_id] = {
                            "box": [x1, y1, x2, y2], "label": text, "color": colour, "event": event, "ts": time.time()
                        }

                    bx1 = max(0, x1 - int((x2 - x1) * 0.8))
                    by1 = max(0, y1 - int((y2 - y1) * 0.3))
                    bx2 = min(fw, x2 + int((x2 - x1) * 0.8))
                    by2 = min(fh, y2 + int((y2 - y1) * 5.0))
                    body_crop = frame[by1:by2, bx1:bx2]
                    update_active_gallery(emp_id, name, body_crop, cam["id"])

                now_ts = time.time()
                active_seats = []
                current_emp_ids = {f.get("emp_id") for f in out if f.get("emp_id")}
                seat_persist = S("seat_persist_seconds", SEAT_PERSIST_SECONDS)
                with seat_lock:
                    for emp_id, info in list(recent_seat_faces.items()):
                        if emp_id not in current_emp_ids and (now_ts - info["ts"]) <= seat_persist:
                            label_text = str(info["label"]).split("  [")[0]
                            if "· HERE" not in label_text and "· IN" not in label_text:
                                label_text += " · HERE"
                            active_seats.append({
                                "box": info["box"], "label": label_text, "color": (87, 122, 27), "event": info["event"]
                            })
                            touch_presence(emp_id, info.get("label", "").split(" (")[0], datetime.now(IST).replace(tzinfo=None))

                room_faces["faces"] = out + active_seats
                with ip_cam_lock:
                    st["timing"]["room_ms"] = int((time.time() - started) * 1000)
                    st["timing"]["faces"] = len(out)
                    st["faces"] = out + exit_faces["faces"]
                    st["room_error"] = None
            except Exception as e:
                import traceback
                traceback.print_exc()
                with ip_cam_lock:
                    st["room_error"] = "Room recognition failed: " + str(e)
            time.sleep(S("room_interval", ROOM_INTERVAL))

    exit_faces = {"faces": []}
    prev_gray = {"img": None}

    def corner_moved(crop):
        tiny = cv2.cvtColor(cv2.resize(crop, (96, 96)), cv2.COLOR_BGR2GRAY)
        before = prev_gray["img"]
        prev_gray["img"] = tiny
        if before is None:
            return True
        return float(cv2.absdiff(tiny, before).mean()) >= MOTION_MIN_DELTA

    def exit_detector():
        while not stop_event.is_set():
            frame = holder["frame"]
            if frame is None or not holder["ok"]:
                time.sleep(0.2)
                continue
            try:
                exit_interval = S("exit_interval", EXIT_INTERVAL)
                reread_below_pct = S("reread_below_pct", REREAD_BELOW_PCT)
                exit_arm_hold = S("exit_arm_min_hold_seconds", EXIT_ARM_MIN_HOLD_SECONDS)

                fh, fw = frame.shape[:2]
                watch = cam.get("watch_zone", zone)
                cx1 = int(watch[0] * fw); cy1 = int(watch[1] * fh)
                cx2 = int(watch[2] * fw); cy2 = int(watch[3] * fh)
                crop = frame[cy1:cy2, cx1:cx2].copy()
                if crop.size == 0:
                    time.sleep(exit_interval)
                    continue

                if not corner_moved(crop):
                    exit_faces["faces"] = []
                    with ip_cam_lock:
                        st["timing"]["exit_skipped"] += 1
                    time.sleep(exit_interval)
                    continue

                started = time.time()
                with _face_model_lock:
                    found = exit_app.get(crop)
                ids, names, matrix, id_array = load_templates()
                out = []

                for face in found:
                    b = face.bbox.astype(float)
                    x1 = int(b[0]) + cx1; y1 = int(b[1]) + cy1
                    x2 = int(b[2]) + cx1; y2 = int(b[3]) + cy1

                    if is_static([x1, y1, x2, y2]):
                        continue

                    pct = face_size_pct(y1, y2, fh)
                    judged = face
                    if pct < reread_below_pct:
                        better = reread_face(frame, x1, y1, x2, y2, exit_app)
                        if better is not None:
                            judged = better
                    emp_id, name, score = identify(judged, ids, names, matrix,
                                                   id_array, pct)
                    if emp_id is None:
                        continue

                    event, at_exit, pct = judge(emp_id, name, score,
                                                x1, y1, x2, y2, fw, fh)
                    text, colour = label_for(name, emp_id, event, pct, at_exit)
                    out.append({"box": [x1, y1, x2, y2], "label": text,
                                "color": colour, "event": event})

                if not found:
                    emp_id, name, sim = match_active_gallery(crop, camera_id=cam["id"])
                    if emp_id is not None:
                        if exit_condition_held("osnet:" + str(emp_id), hold_seconds=exit_arm_hold):
                            arm_exit(emp_id, name, cam["id"])
                            if SCORE_DEBUG:
                                print("[osnet-exit] " + str(name) + " armed for exit via OSNet back-view matching (sim: " + ("%.2f" % sim) + ")")

                exit_faces["faces"] = out
                with ip_cam_lock:
                    st["timing"]["exit_ms"] = int((time.time() - started) * 1000)
                    st["timing"]["exit_ran"] += 1
                    st["faces"] = room_faces["faces"] + out
                    st["exit_error"] = None
            except Exception as e:
                import traceback
                traceback.print_exc()
                with ip_cam_lock:
                    st["exit_error"] = "Exit recognition failed: " + str(e)
            time.sleep(S("exit_interval", EXIT_INTERVAL))

    def person_tracker():
        """The fast loop that follows bodies, not faces."""
        while not stop_event.is_set():
            frame = holder["frame"]
            if frame is None or not holder["ok"]:
                time.sleep(0.3)
                continue
            started = time.time()
            try:
                small = frame
                if small.shape[1] > 960:
                    scale = 960.0 / small.shape[1]
                    small = cv2.resize(small, (960, int(small.shape[0] * scale)),
                                       interpolation=cv2.INTER_AREA)
                sx = frame.shape[1] / small.shape[1]
                sy = frame.shape[0] / small.shape[0]

                found = tracker.update(small, sx=sx, sy=sy)
                out = []
                for person in found:
                    if person["named"] or SHOW_UNKNOWN_BOXES:
                        b = person["box"]
                        out.append({
                            "box": [int(b[0]), int(b[1]), int(b[2]), int(b[3])],
                            "label": person["label"],
                            "color": (0, 220, 0) if person["named"] else (140, 140, 140),
                            "event": None,
                        })
                person_boxes["boxes"] = out

                with ip_cam_lock:
                    st["timing"]["track_ms"] = int((time.time() - started) * 1000)
                    st["timing"]["tracks"] = len(out)
                    st["track_error"] = None
            except Exception as e:
                with ip_cam_lock:
                    st["track_error"] = "Person tracking failed: " + str(e)
            time.sleep(YOLO_INTERVAL)

    room_thread = exit_thread = track_thread = None
    if recognise:
        room_thread = threading.Thread(target=room_detector, daemon=True)
        room_thread.start()
        exit_thread = threading.Thread(target=exit_detector, daemon=True)
        exit_thread.start()
        if YOLO_ENABLED and load_yolo(cam["id"]) is not None:
            track_thread = threading.Thread(target=person_tracker, daemon=True)
            track_thread.start()

    no_frame_since = time.time()
    last_seq = -1
    last_encode_at = 0.0
    frame_budget = 1.0 / max(STREAM_FPS, 1)

    while True:
        loop_start = time.time()

        with ip_cam_lock:
            if st["stop_flag"]:
                break

        try:
            frame = holder["frame"]
            seq = holder["seq"]

            if frame is None or not holder["ok"]:
                if time.time() - no_frame_since > 5:
                    with ip_cam_lock:
                        st["error"] = "Lost the camera stream. Reconnecting."
                time.sleep(0.2)
                continue
            no_frame_since = time.time()

            if seq == last_seq:
                time.sleep(0.01)
                continue
            last_seq = seq

            with ip_cam_lock:
                watched = st["viewers"] > 0
            if not watched and (loop_start - last_encode_at) < UNWATCHED_ENCODE_INTERVAL:
                time.sleep(0.2)
                continue
            last_encode_at = loop_start

            frame = frame.copy()
            fh, fw = frame.shape[:2]

            out_scale = 1.0
            if fw > STREAM_MAX_WIDTH:
                out_scale = STREAM_MAX_WIDTH / fw
                frame = cv2.resize(frame, (int(fw * out_scale), int(fh * out_scale)),
                                   interpolation=cv2.INTER_AREA)

            if ZONE_DEBUG and recognise and not doorway:
                oh, ow = frame.shape[:2]
                cv2.rectangle(frame,
                              (int(zone[0] * ow), int(zone[1] * oh)),
                              (int(zone[2] * ow) - 1, int(zone[3] * oh) - 1),
                              (61, 160, 216), 2)
                cv2.putText(frame, "EXIT ZONE",
                            (int(zone[0] * ow) + 6, int(zone[1] * oh) + 20),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (61, 160, 216), 2)

            for f in person_boxes["boxes"] + room_faces["faces"] + exit_faces["faces"]:
                x1, y1, x2, y2 = [int(v * out_scale) for v in f["box"]]
                cv2.rectangle(frame, (x1, y1), (x2, y2), f["color"], 2)
                cv2.putText(frame, f["label"], (x1, max(y1 - 8, 14)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, f["color"], 2)

            ok, buf = cv2.imencode('.jpg', frame,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), STREAM_JPEG_QUALITY])
            if ok:
                with ip_cam_lock:
                    st["latest_jpeg"] = buf.tobytes()

            spent = time.time() - loop_start
            if spent < frame_budget:
                time.sleep(frame_budget - spent)

        except Exception as e:
            with ip_cam_lock:
                st["error"] = "Video error: " + str(e)
            time.sleep(1)

    stop_event.set()
    if room_thread is not None:
        room_thread.join(timeout=2)
    if exit_thread is not None:
        exit_thread.join(timeout=2)
    if track_thread is not None:
        track_thread.join(timeout=3)


def _safe_url(url):
    """The address with any password removed, so it can be shown on screen."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "?"
        if parsed.port:
            host += ":" + str(parsed.port)
        # The query string (?channel=1&subtype=0 and similar) is what tells
        # several otherwise-identical camera candidates apart -- dropping it
        # made every "no answer from .../video.cgi" line for a list of
        # candidates print the exact same text, so there was no way to tell
        # afterwards which of them had actually been tried. It never carries
        # the password (that's in the netloc, handled separately below), so
        # there is nothing sensitive lost by keeping it.
        path = parsed.path or ""
        if parsed.query:
            path += "?" + parsed.query
        return parsed.scheme + "://" + host + path
    except Exception:
        return "the configured address"


def is_snapshot_url(url):
    return url.lower().startswith(("http://", "https://"))


_camera_ssl = ssl.create_default_context()
_camera_ssl.check_hostname = False
_camera_ssl.verify_mode = ssl.CERT_NONE


def build_snapshot_fetcher(url):
    """Prepares an HTTP fetcher for a Hikvision still-image URL."""
    parsed = urlparse(url)
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")

    host = parsed.hostname or ""
    if parsed.port:
        host += ":" + str(parsed.port)
    clean = urlunparse((parsed.scheme, host, parsed.path,
                        parsed.params, parsed.query, parsed.fragment))

    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, clean, user, password)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_camera_ssl),
        urllib.request.HTTPDigestAuthHandler(manager),
        urllib.request.HTTPBasicAuthHandler(manager))
    return opener, clean


def ip_camera_worker(cam):
    st = cam_state(cam["id"])

    url, brand = resolve_camera_url(cam)
    if not url:
        with ip_cam_lock:
            st["error"] = (
                "Could not find a working address for " + cam["name"] + " at "
                + str(cam.get("host", "")) + ". Every known path was tried over both "
                "http and https and none returned a picture. This is an unusual make. "
                "To find its address: open the camera's own page in Chrome, press F12 "
                "for developer tools, click the Network tab, then reload the live view. "
                "The request that returns the picture is the address — paste it into "
                "CAMERAS as \"url\" with the username and password in front, like "
                "https://admin:PASSWORD@host:port/that/path.")
            st["phase"] = "failed"
            st["running"] = False
        return

    with ip_cam_lock:
        st["url"] = url
        st["found_brand"] = brand
        st["phase"] = "connecting"

    if is_snapshot_url(url):
        if (brand or "").endswith("MJPEG stream") or cam.get("mjpeg"):
            return mjpeg_camera_worker(cam, url)
        return snapshot_camera_worker(cam, url)
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        with ip_cam_lock:
            st["error"] = (
                "Can't reach the camera at " + _safe_url(url) + ". If that is a "
                "192.168.x.x address it only works from inside the office network — "
                "from anywhere else use the camera's public address instead. Check "
                "the address, port and password in CAMERAS at the top of this file. A camera "
                "given a \"host\" instead of a \"url\" has its path found automatically, "
                "which is usually the easier way round.")
            st["phase"] = "failed"
            st["running"] = False
        return

    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass

    stop_event = threading.Event()

    holder = {"frame": None, "ok": False, "seq": 0}

    def grabber():
        interval = 1.0 / max(STREAM_FPS, 1)
        last_decode = 0.0
        while not stop_event.is_set():
            if not cap.grab():
                holder["ok"] = False
                time.sleep(0.05)
                continue
            now = time.time()
            if now - last_decode < interval:
                continue
            ok, frame = cap.retrieve()
            if ok and frame is not None:
                last_decode = now
                holder["frame"] = frame
                holder["ok"] = True
                holder["seq"] += 1
            else:
                holder["ok"] = False
                time.sleep(0.02)

    grabber_thread = threading.Thread(target=grabber, daemon=True)
    grabber_thread.start()

    waited = time.time()
    while holder["seq"] == 0:
        if time.time() - waited > FIRST_FRAME_TIMEOUT:
            stop_event.set()
            grabber_thread.join(timeout=2)
            cap.release()
            with ip_cam_lock:
                st["error"] = (
                    "Connected to " + _safe_url(url) + " but no video arrived within "
                    + str(FIRST_FRAME_TIMEOUT) + " seconds. The address answered, so "
                    "the problem is usually the stream path or the password. Try the "
                    "same URL in VLC — if VLC cannot play it, neither can this.")
                st["phase"] = "failed"
                st["running"] = False
            return
        if stop_event.is_set():
            return
        time.sleep(0.2)

    with ip_cam_lock:
        st["phase"] = "live"

    run_recognition_pipeline(cam, holder, stop_event)

    stop_event.set()
    grabber_thread.join(timeout=2)
    cap.release()
    with ip_cam_lock:
        st["running"] = False


def _looks_torn(raw):
    """True if these JPEG bytes were cut off mid-transfer, not real video.

    A complete JPEG file always ends with the two-byte "end of image"
    marker (0xFFD9). A frame that arrived over a shaky/high-jitter link and
    got cut off partway through is missing it -- cv2.imdecode does not
    always fail on that; it can decode the top of the picture correctly
    and leave the rest a flat grey fill, which is what a torn frame from a
    jittery camera link looks like. Checking for the marker catches a
    truncated frame directly, without guessing from the picture content --
    a real camera pointed at a plain floor or wall is not a torn frame,
    and must not be treated as one.
    """
    return not raw.endswith(b"\xff\xd9")


def mjpeg_camera_worker(cam, url):
    """Feeds the pipeline from a held-open multipart HTTP stream."""
    st = cam_state(cam["id"])
    stop_event = threading.Event()
    holder = {"frame": None, "ok": False, "seq": 0}
    stats = {"bytes": 0, "frames": 0, "torn": 0, "decode_fail": 0}

    def reader():
        misses = 0
        while not stop_event.is_set():
            try:
                opener, clean = build_snapshot_fetcher(url)
                reply = opener.open(clean, timeout=10)
                boundary = _boundary_of(reply)
                if boundary is None:
                    raise ValueError("that address is not a video stream")

                def got(raw):
                    if _looks_torn(raw):
                        stats["torn"] += 1
                        return
                    frame = cv2.imdecode(np.frombuffer(raw, np.uint8),
                                         cv2.IMREAD_COLOR)
                    if frame is None:
                        stats["decode_fail"] += 1
                        return
                    holder["frame"] = frame
                    holder["ok"] = True
                    holder["seq"] += 1

                with ip_cam_lock:
                    st["error"] = None
                misses = 0
                read_mjpeg_frames(reply, boundary, stop_event, got, stats)
                try:
                    reply.close()
                except Exception:
                    pass
            except Exception as e:
                misses += 1
                holder["ok"] = False
                print("[camera] " + cam["name"] + ": connect attempt failed -- "
                      + repr(e))
                if misses >= 3:
                    with ip_cam_lock:
                        st["error"] = "Video stream keeps dropping: " + str(e)
                time.sleep(2)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    waited = time.time()
    while holder["seq"] == 0:
        if time.time() - waited > FIRST_FRAME_TIMEOUT:
            stop_event.set()
            detail = (str(stats["bytes"]) + " bytes received, " + str(stats["frames"])
                      + " frame marker(s) found, " + str(stats["torn"])
                      + " looked corrupted, " + str(stats["decode_fail"])
                      + " failed to read")
            print("[camera] " + cam["name"] + ": no picture within "
                  + str(FIRST_FRAME_TIMEOUT) + "s -- " + detail)

           
            with ip_cam_lock:
                st["error"] = ("Opened the video stream at " + _safe_url(url)
                               + " but no pictures arrived (" + detail + ").")
                st["phase"] = "failed"
                st["running"] = False
            return
        if stop_event.is_set():
            return
        time.sleep(0.2)

    with ip_cam_lock:
        st["phase"] = "live"

    run_recognition_pipeline(cam, holder, stop_event)

    stop_event.set()
    reader_thread.join(timeout=3)
    with ip_cam_lock:
        st["running"] = False


def snapshot_camera_worker(cam, url):
    """Runs the same recognition pipeline, fed by repeated still images."""
    st = cam_state(cam["id"])
    try:
        opener, clean = build_snapshot_fetcher(url)
    except Exception as e:
        with ip_cam_lock:
            st["error"] = "That camera address could not be read: " + str(e)
            st["phase"] = "failed"
            st["running"] = False
        return

    try:
        with opener.open(clean, timeout=8) as reply:
            probe = reply.read()
        if cv2.imdecode(np.frombuffer(probe, np.uint8), cv2.IMREAD_COLOR) is None:
            raise ValueError("the reply was not an image — check the URL path")
    except Exception as e:
        with ip_cam_lock:
            st["error"] = (
                "Could not fetch a picture from the camera: " + str(e)
                + ". Check the address, port, username and password, and that "
                "the Hikvision login page opens in a browser at that address.")
            st["phase"] = "failed"
            st["running"] = False
        return

    stop_event = threading.Event()
    holder = {"frame": None, "ok": False, "seq": 0}
    misses = {"n": 0}

    def fetcher():
        while not stop_event.is_set():
            started = time.time()
            try:
                own_opener, own_url = build_snapshot_fetcher(url)
                with own_opener.open(own_url, timeout=8) as reply:
                    data = reply.read()
                frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    raise ValueError("reply was not a readable image")
                holder["frame"] = frame
                holder["ok"] = True
                holder["seq"] += 1
                misses["n"] = 0
                with ip_cam_lock:
                    st["error"] = None
            except Exception as e:
                misses["n"] += 1
                if misses["n"] >= 5 * SNAPSHOT_WORKERS:
                    holder["ok"] = False
                    with ip_cam_lock:
                        st["error"] = "Lost the camera: " + str(e)
                time.sleep(0.5)
            spent = time.time() - started
            if spent < SNAPSHOT_INTERVAL:
                time.sleep(SNAPSHOT_INTERVAL - spent)

    threads = []
    for _ in range(max(1, SNAPSHOT_WORKERS)):
        t = threading.Thread(target=fetcher, daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.05)

    with ip_cam_lock:
        st["phase"] = "live"

    run_recognition_pipeline(cam, holder, stop_event)

    stop_event.set()
    for t in threads:
        t.join(timeout=3)
    with ip_cam_lock:
        st["running"] = False


def start_camera_connection(cam):
    """Starts, or confirms already running, the worker for one camera."""
    st = cam_state(cam["id"])

    with ip_cam_lock:
        already_ok = (st["running"] and st["error"] is None
                      and st["phase"] in ("connecting", "finding", "live"))
    if already_ok:
        return {"status": "already_running"}

    cam.pop("_found_url", None)
    cam.pop("_found_brand", None)

    old_thread = st.get("thread")
    with ip_cam_lock:
        st["stop_flag"] = True

    if old_thread is not None and old_thread.is_alive():
        old_thread.join(timeout=8)

    with ip_cam_lock:
        st["stop_flag"] = False
        st["running"] = True
        st["phase"] = "connecting"
        st["error"] = None
        st["room_error"] = None
        st["exit_error"] = None
        st["faces"] = []
        st["latest_jpeg"] = None

    t = threading.Thread(target=ip_camera_worker, args=(cam,), daemon=True)
    st["thread"] = t
    t.start()

    def watchdog():
        deadline = time.time() + FIRST_FRAME_TIMEOUT + 5
        while time.time() < deadline:
            time.sleep(0.5)
            with ip_cam_lock:
                phase = st["phase"]
            if phase == "finding":
                deadline = time.time() + FIRST_FRAME_TIMEOUT + 5
                continue
            if phase != "connecting":
                return
        with ip_cam_lock:
            if st["phase"] == "connecting":
                st["error"] = (
                    "No response from " + _safe_url(st["url"] or cam.get("host", "?"))
                    + " after "
                    + str(FIRST_FRAME_TIMEOUT + 5) + " seconds. Nothing is answering "
                    "at that address from this machine. Check the address in CAMERAS, "
                    "and that the camera's own login page opens in a browser.")
                st["phase"] = "failed"
                st["running"] = False

    threading.Thread(target=watchdog, daemon=True).start()
    return {"status": "connecting"}


def start_all_cameras():
    for cam in CAMERAS:
        if not cam.get("enabled", True):
            print("[camera] " + cam["name"] + ": disabled in local_settings, not starting")
            continue
        try:
            start_camera_connection(cam)
        except Exception as e:
            st = cam_state(cam["id"])
            with ip_cam_lock:
                st["error"] = "Could not start: " + str(e)
                st["phase"] = "failed"


@app.get("/api/cameras")
def list_cameras():
    """What exists and how each one is doing, for the camera picker.

    Disabled cameras ARE listed (marked with "enabled": False) so someone
    can deliberately switch one on from the Live Camera page when needed.
    What must never happen is a disabled camera starting on its own: it's
    skipped at server boot (start_all_cameras) and selecting its tab does
    NOT auto-connect the way an enabled camera's tab does -- only an
    explicit "Start this camera" click does, and that click is logged. That
    distinction is what the Anita Godown incident was missing: the old code
    let it be started as a side effect of just opening its tab.
    """
    out = []
    for cam in CAMERAS:
        st = cam_state(cam["id"])
        with ip_cam_lock:
            out.append({
                "id": cam["id"],
                "name": cam["name"],
                "recognition": bool(cam.get("recognition", True)),
                "enabled": bool(cam.get("enabled", True)),
                "phase": st["phase"],
                "error": (st["error"] or st["room_error"] or st["exit_error"]
                          or st["track_error"]),
            })
    return {"cameras": out, "default": DEFAULT_CAMERA}


@app.post("/api/ip_camera/connect")
async def ip_camera_connect(cam: str = Form(default="")):
    camera = get_camera(cam)
    if not camera.get("enabled", True):
        print("[camera] " + camera["name"] + ": manually started from the "
              "Live Camera page despite being disabled in local_settings.py")
    return start_camera_connection(camera)


@app.post("/api/ip_camera/stop")
async def ip_camera_stop(cam: str = Form(default="")):
    st = cam_state(get_camera(cam)["id"])
    with ip_cam_lock:
        st["stop_flag"] = True
        st["running"] = False
        st["phase"] = "idle"
    return {"status": "stopped"}


@app.get("/api/ip_camera/status")
async def ip_camera_status(cam: str = ""):
    camera = get_camera(cam)
    st = cam_state(camera["id"])
    with ip_cam_lock:
        problem = (st["error"] or st["room_error"] or st["exit_error"]
                   or st["track_error"])
        return {
            "id": camera["id"],
            "name": camera["name"],
            "recognition": bool(camera.get("recognition", True)),
            "running": st["running"],
            "phase": st["phase"],
            "error": problem,
            "found_url": _safe_url(st["url"]) if st.get("url") else "",
            "found_brand": st.get("found_brand") or "",
            "timing": dict(st["timing"]),
            "armed": armed_count(),
            "faces": [{"box": f["box"], "label": f["label"], "event": f["event"]}
                      for f in st["faces"]],
        }


@app.get("/api/ip_camera/frame")
async def ip_camera_frame(cam: str = ""):
    st = cam_state(get_camera(cam)["id"])
    with ip_cam_lock:
        jpeg = st["latest_jpeg"]
    if jpeg is None:
        return Response(status_code=404)
    return Response(content=jpeg, media_type="image/jpeg")


async def mjpeg_generator(cam_id, request):
    """Pushes the latest frame as a multipart stream, which browsers render natively as video insid..."""
    st = cam_state(cam_id)
    boundary = b"--frame\r\n"
    delay = 1.0 / max(STREAM_FPS, 1)

    with ip_cam_lock:
        st["viewers"] += 1
    try:
        async for chunk in _mjpeg_frames(st, boundary, delay, request):
            yield chunk
    finally:
        with ip_cam_lock:
            st["viewers"] = max(0, st["viewers"] - 1)


async def _mjpeg_frames(st, boundary, delay, request):
    idle_since = None
    last_sent = None

    while True:
        if await request.is_disconnected():
            break

        with ip_cam_lock:
            jpeg = st["latest_jpeg"]
            running = st["running"]

        if not running:
            if idle_since is None:
                idle_since = time.time()
            elif time.time() - idle_since > 15:
                break
        else:
            idle_since = None

        if jpeg is not None and jpeg is not last_sent:
            last_sent = jpeg
            yield (boundary + b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")

        await asyncio.sleep(delay)


@app.get("/api/ip_camera/mjpeg")
async def ip_camera_mjpeg(request: Request, cam: str = ""):
    return StreamingResponse(
        mjpeg_generator(get_camera(cam)["id"], request),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"})


SNAPSHOT_PATHS = [
    ("Hikvision",        "/ISAPI/Streaming/channels/101/picture"),
    ("Hikvision (old)",  "/Streaming/channels/1/picture"),
    ("Dahua / CP Plus",  "/cgi-bin/snapshot.cgi"),
    ("Dahua (channel)",  "/cgi-bin/snapshot.cgi?channel=1"),
    ("XM / Xiongmai",    "/webcapture.jpg?command=snap&channel=1"),
    ("Hisilicon",        "/Snapshot/1/RemoteImageCapture?ImageFormat=2"),
    ("Hisilicon (alt)",  "/cgi-bin/hi3510/snap.cgi?&-getstream"),
    ("Reolink",          "/cgi-bin/api.cgi?cmd=Snap&channel=0"),
    ("Axis",             "/axis-cgi/jpg/image.cgi"),
    ("Vivotek",          "/cgi-bin/viewer/video.jpg"),
    ("ONVIF profile",    "/onvif-http/snapshot?Profile_1"),
    ("ONVIF (media)",    "/onvif/media/snapshot"),
    ("Generic",          "/snapshot.jpg"),
    ("Generic",          "/snap.jpg"),
    ("Generic",          "/image.jpg"),
    ("Generic",          "/jpg/image.jpg"),
    ("Generic",          "/image/jpeg.cgi"),
    ("Generic",          "/tmpfs/auto.jpg"),
    ("Generic",          "/videostream.cgi"),
    ("Sunell / Nexivue", "/action/snap?cam=0"),
    ("Generic",          "/cgi-bin/snapshot.cgi?chn=0"),
    ("Generic",          "/snapshot?channel=0"),
    ("Generic",          "/live/0/jpeg.jpg"),
    ("Generic",          "/img/snapshot.cgi?size=3"),
    ("Generic",          "/cgi-bin/currentpic.cgi"),
    ("Generic",          "/web/cgi-bin/hi3510/snap.cgi"),
    ("Generic",          "/onvif/snapshot"),
    ("Generic",          "/api/snapshot"),
    ("Generic",          "/GetSnapshot/0"),
    ("Generic",          "/jpg/1/image.jpg"),
    ("Generic",          "/media/cam0/still.jpg"),
]


RTSP_PATHS = [
    ("Hikvision",        "/Streaming/Channels/101"),
    ("Hikvision (sub)",  "/Streaming/Channels/102"),
    ("Dahua / CP Plus",  "/cam/realmonitor?channel=1&subtype=0"),
    ("Dahua (sub)",      "/cam/realmonitor?channel=1&subtype=1"),
    ("XM / Xiongmai",    "/user=__USER__&password=__PASS__&channel=1&stream=0.sdp"),
    ("Hisilicon",        "/11"),
    ("Hisilicon (sub)",  "/12"),
    ("ONVIF",            "/onvif1"),
    ("Generic",          "/live"),
    ("Generic",          "/live/ch0"),
    ("Generic",          "/h264"),
    ("Generic",          "/media/video1"),
    ("Generic",          "/stream1"),
]


def _port_open(host, port, timeout=1.5):
    """Whether anything is listening, checked in about a second."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _host_port_of(url, default_port):
    try:
        parsed = urlparse(url)
        return parsed.hostname, (parsed.port or default_port)
    except Exception:
        return None, default_port


def probe_rtsp(url, timeout=4.0):
    """True if this RTSP address actually delivers a decodable frame."""
    host, port = _host_port_of(url, 554)
    if host and not _port_open(host, port):
        return False

    was = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", "")
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
        "rtsp_transport;tcp|stimeout;" + str(int(timeout * 1_000_000)))
    cap = None
    try:
        cap = cv2.VideoCapture(url)
        if not cap.isOpened():
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            ok, frame = cap.read()
            if ok and frame is not None:
                return True
        return False
    except Exception:
        return False
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = was


def discover_rtsp_url(host, user="admin", password="", port=554, timeout=4.0):
    """Finds a working RTSP address, or (None, None) if there is none."""
    scheme, host = _clean_host(host)
    if not host:
        return None, None
    if ":" in host:
        host = host.split(":", 1)[0]
    base = host + ":" + str(port)
    if not _port_open(host, port):
        return None, None

    safe_user, safe_pass = _credentials_for_url(user, password)
    for brand, path in RTSP_PATHS:
        filled = path.replace("__USER__", user).replace("__PASS__", password)
        candidate = "rtsp://" + safe_user + ":" + safe_pass + "@" + base + filled
        if probe_rtsp(candidate, timeout=timeout):
            return candidate, brand
    return None, None


MJPEG_PATHS = [
    ("Dahua / CP Plus",  "/cgi-bin/mjpg/video.cgi?channel=1&subtype=1"),
    ("Dahua (main)",     "/cgi-bin/mjpg/video.cgi?channel=1&subtype=0"),
    ("Axis",             "/axis-cgi/mjpg/video.cgi"),
    ("Generic",          "/videostream.cgi"),
    ("Generic",          "/video.cgi"),
    ("Generic",          "/mjpg/video.mjpg"),
    ("Generic",          "/video.mjpg"),
    ("Generic",          "/cgi-bin/hi3510/mjpegstream.cgi?-snap=1"),
    ("Hikvision",        "/ISAPI/Streaming/channels/102/httpPreview"),
    ("Generic",          "/live/0/mjpeg.jpg"),
    ("Generic",          "/cgi-bin/mjpeg?chn=0"),
    ("Generic",          "/action/stream?cam=0"),
]


def _boundary_of(response):
    """The separator between frames, taken from the Content-Type header."""
    ctype = response.headers.get("Content-Type", "") or ""
    if "multipart" not in ctype.lower():
        return None
    marker = "boundary="
    if marker not in ctype:
        return b"--frame"
    bound = ctype.split(marker, 1)[1].strip().strip('"').split(";")[0]
    if not bound.startswith("--"):
        bound = "--" + bound
    return bound.encode("ascii", "ignore")


def read_mjpeg_frames(response, boundary, stop_event, on_frame, stats=None):
    """Pulls JPEGs out of a multipart HTTP stream until it stops or is told to.

    stats, if given, is a dict this fills in as it goes ("bytes", "frames")
    so a caller that never gets a picture can tell whether the connection
    delivered nothing at all, or delivered data that never formed a
    complete JPEG.
    """
    buf = b""
    while not stop_event.is_set():
        chunk = response.read(16384)
        if not chunk:
            return
        buf += chunk
        if stats is not None:
            stats["bytes"] = stats.get("bytes", 0) + len(chunk)

        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                if len(buf) > 1_000_000:
                    buf = buf[-2:]
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                buf = buf[start:]
                break
            frame = buf[start:end + 2]
            buf = buf[end + 2:]
            if stats is not None:
                stats["frames"] = stats.get("frames", 0) + 1
            on_frame(frame)


def discover_mjpeg_url(host, user="admin", password="", timeout=4.0):
    """Finds an MJPEG stream address, or (None, None) if the camera has none."""
    scheme, host = _clean_host(host)
    if not host:
        return None, None
    safe_user, safe_pass = _credentials_for_url(user, password)
    for brand, path in MJPEG_PATHS:
        candidate = scheme + "://" + safe_user + ":" + safe_pass + "@" + host + path
        try:
            opener, clean = build_snapshot_fetcher(candidate)
            reply = opener.open(clean, timeout=timeout)
            boundary = _boundary_of(reply)
            if boundary is None:
                reply.close()
                continue
            got = {"ok": False}
            stop = threading.Event()

            def first(frame):
                if cv2.imdecode(np.frombuffer(frame, np.uint8),
                                cv2.IMREAD_COLOR) is not None:
                    got["ok"] = True
                    stop.set()

            reader = threading.Thread(
                target=lambda: read_mjpeg_frames(reply, boundary, stop, first),
                daemon=True)
            reader.start()
            reader.join(timeout=timeout)
            stop.set()
            try:
                reply.close()
            except Exception:
                pass
            if got["ok"]:
                return candidate, brand
        except Exception:
            continue
    return None, None


def _credentials_for_url(user, password):
    """URL-encodes a username and password for embedding in an address."""
    return quote(user or "", safe=""), quote(password or "", safe="")


def _clean_host(host):
    """Splits a configured host into (scheme, host:port)."""
    host = (host or "").strip()
    scheme = "http"
    if host.startswith("https://"):
        scheme, host = "https", host[len("https://"):]
    elif host.startswith("http://"):
        host = host[len("http://"):]
    return scheme, host.rstrip("/").split("/", 1)[0]


def discover_snapshot_url(host, user="admin", password="", timeout=3.0):
    """Finds a working still-image address by trying the known paths in turn."""
    scheme, host = _clean_host(host)
    if not host:
        return None, None
    safe_user, safe_pass = _credentials_for_url(user, password)

    for brand, path in SNAPSHOT_PATHS:
        candidate = scheme + "://" + safe_user + ":" + safe_pass + "@" + host + path
        try:
            opener, clean = build_snapshot_fetcher(candidate)
            with opener.open(clean, timeout=timeout) as reply:
                data = reply.read(400000)
            if cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) is not None:
                return candidate, brand
        except Exception:
            continue
    return None, None


def _works(url, timeout=4.0):
    """True if this one address actually delivers a picture, whatever kind it is."""
    if url.lower().startswith(("rtsp://", "rtsps://")):
        return probe_rtsp(url, timeout=timeout)

    host, port = _host_port_of(url, 80)
    if host and not _port_open(host, port):
        return False

    try:
        opener, clean = build_snapshot_fetcher(url)
        reply = opener.open(clean, timeout=timeout)
        boundary = _boundary_of(reply)
        if boundary is not None:
            got = {"ok": False}
            stop = threading.Event()

            def first(raw):
                if cv2.imdecode(np.frombuffer(raw, np.uint8),
                                cv2.IMREAD_COLOR) is not None:
                    got["ok"] = True
                    stop.set()

            reader = threading.Thread(
                target=lambda: read_mjpeg_frames(reply, boundary, stop, first),
                daemon=True)
            reader.start()
            reader.join(timeout=timeout)
            stop.set()
            try:
                reply.close()
            except Exception:
                pass
            return got["ok"]
        data = reply.read(400000)
        reply.close()
        return cv2.imdecode(np.frombuffer(data, np.uint8),
                            cv2.IMREAD_COLOR) is not None
    except Exception:
        return False


def resolve_camera_url(cam):
    """The address to actually use for this camera."""
    if cam.get("_found_url"):
        return cam["_found_url"], cam.get("_found_brand")

    configured = cam.get("url")
    if isinstance(configured, str) and configured:
        return configured, None

    st = cam_state(cam["id"])

    if isinstance(configured, (list, tuple)) and configured:
        with ip_cam_lock:
            st["phase"] = "finding"
        for candidate in configured:
            kind = "RTSP" if candidate.lower().startswith(("rtsp://", "rtsps://")) else "HTTP"
            if _works(candidate):
                cam["_found_url"] = candidate
                cam["_found_brand"] = kind
                print("[camera] " + cam["name"] + ": using the " + kind
                      + " address " + _safe_url(candidate))
                return candidate, kind
            print("[camera] " + cam["name"] + ": no answer from "
                  + _safe_url(candidate) + ", trying the next")
        return configured[0], None

    with ip_cam_lock:
        st["phase"] = "finding"

    host = cam.get("host", "")
    user = cam.get("user", "admin")
    password = cam.get("password", "")

    scheme, hostport = _clean_host(host)
    other = ("http://" if scheme == "https" else "https://") + hostport
    attempts = [host, other]

    url = brand = None
    for where in attempts:
        url, brand = discover_rtsp_url(where, user, password)
        if url:
            brand = brand + " RTSP"
            break
        url, brand = discover_mjpeg_url(where, user, password)
        if url:
            brand = brand + " MJPEG stream"
            break
        url, brand = discover_snapshot_url(where, user, password)
        if url:
            brand = brand + " snapshots (no stream offered)"
            break
    if url:
        cam["_found_url"] = url
        cam["_found_brand"] = brand
        print("[camera] " + cam["name"] + ": found a working address — "
              + brand + " style, " + _safe_url(url))
        print("[camera] " + cam["name"] + ": to skip this search next time, put "
              "this in CAMERAS —")
        print('[camera]     "url": "' + url + '",')
    return url, brand


@app.post("/api/camera/probe")
async def camera_probe(host: str = Form(...), user: str = Form(default="admin"),
                       password: str = Form(default="")):
    """Tries every known snapshot path against one camera and reports what works."""
    scheme, host = _clean_host(host)

    results = []
    working = []
    for brand, path in SNAPSHOT_PATHS:
        safe_user, safe_pass = _credentials_for_url(user, password)
        probe_url = scheme + "://" + safe_user + ":" + safe_pass + "@" + host + path
        try:
            opener, clean = build_snapshot_fetcher(probe_url)
            with opener.open(clean, timeout=5) as reply:
                data = reply.read(400000)
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                results.append({"brand": brand, "path": path,
                                "ok": False, "note": "answered, but not a picture"})
                continue
            h, w = img.shape[:2]
            results.append({"brand": brand, "path": path, "ok": True,
                            "note": str(w) + "x" + str(h) + " image"})
            working.append({"brand": brand, "path": path,
                            "url": "http://" + user + ":" + password + "@" + host + path,
                            "size": str(w) + "x" + str(h)})
        except Exception as e:
            note = str(e)
            if len(note) > 70:
                note = note[:70] + "…"
            results.append({"brand": brand, "path": path, "ok": False, "note": note})

    return {"status": "ok", "host": host, "results": results, "working": working}


@app.get("/static/logo.png")
def logo_png():
    """The sidebar logo."""
    if not LOGO_BYTES:
        return Response(status_code=404)
    return Response(content=LOGO_BYTES, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})


import hmac
import hashlib
import secrets
from fastapi.responses import JSONResponse

FALLBACK_USERS = {
    "admin": {"password": "KenAdmin@2026", "role": "admin"},
}

USER_CACHE_SECONDS = 60

_users_cache = {"at": 0.0, "users": {}}


def load_users(force=False):
    """Accounts from the database, falling back to FALLBACK_USERS."""
    now = time.time()
    if not force and (now - _users_cache["at"]) < USER_CACHE_SECONDS:
        if _users_cache["users"]:
            return _users_cache["users"]

    found = {}
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT username, password, role FROM users WHERE active = 1;")
        for username, password, role in cur.fetchall():
            found[username] = {"password": password, "role": role or "admin"}
        cur.close()
        conn.close()
    except Exception:
        found = {}

    if not found:
        found = dict(FALLBACK_USERS)

    _users_cache.update(at=now, users=found)
    return found


SECRET_KEY = getattr(_local, "SECRET_KEY", "change-this-to-a-long-random-string-before-going-public")

SESSION_COOKIE = "ken_session"
SESSION_HOURS = getattr(_local, "SESSION_HOURS", 12)

SIGN_OUT_ON_BROWSER_CLOSE = getattr(_local, "SIGN_OUT_ON_BROWSER_CLOSE", True)

PUBLIC_PATHS = {"/login", "/logout", "/static/logo.png", "/favicon.ico"}

ADMIN_PREFIXES = ("/enroll", "/delete")


def hash_password(plain):
    """Turns a password into a pbkdf2 hash to store instead of the real one."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), 200_000)
    return "pbkdf2$" + salt + "$" + digest.hex()


def _password_ok(supplied, stored):
    """Constant-time check, accepting either a hash or a plain password."""
    if stored.startswith("pbkdf2$"):
        try:
            _, salt, expected = stored.split("$", 2)
        except ValueError:
            return False
        got = hashlib.pbkdf2_hmac("sha256", supplied.encode(), salt.encode(),
                                  200_000).hex()
        return hmac.compare_digest(got, expected)
    return hmac.compare_digest(supplied, stored)


def _sign(payload):
    return hmac.new(SECRET_KEY.encode(), payload.encode(),
                    hashlib.sha256).hexdigest()


def make_session(username, role):
    """A cookie value carrying who you are, until when, and a signature."""
    expires = int(time.time()) + SESSION_HOURS * 3600
    payload = username + "|" + role + "|" + str(expires)
    return payload + "|" + _sign(payload)


def read_session(cookie):
    """Who this cookie says you are, or None if it is missing, tampered with, or past its expiry."""
    if not cookie:
        return None
    try:
        payload, signature = cookie.rsplit("|", 1)
        if not hmac.compare_digest(signature, _sign(payload)):
            return None
        username, role, expires = payload.split("|")
        if int(expires) < time.time():
            return None
        if username not in load_users():
            return None
        return {"user": username, "role": role}
    except Exception:
        return None


LOGIN_BODY = """
<div style="max-width:380px;margin:6vh auto;">
  <div class="head" style="justify-content:flex-start;margin-bottom:18px;">
    <div><div class="eyebrow">KEN Attendance</div><h1>Sign in</h1></div>
  </div>
  __ERROR__
  <div class="panel">
    <form method="post" action="/login">
      <input type="hidden" name="next" value="__NEXT__">
      <label for="username">Username</label>
      <input id="username" name="username" autofocus autocomplete="username">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password">
      <button class="btn" type="submit" style="margin-top:20px;width:100%;
              justify-content:center;">Sign in</button>
    </form>
  </div>
  <p class="note">Attendance records without anyone signed in. Sign in to view it.</p>
</div>
"""


@app.get("/login", response_class=HTMLResponse)
def login_page(next: str = "/", error: str = ""):
    banner = ('<div class="panel" style="border-color:var(--madder);'
              'color:var(--madder);padding:12px 16px;">' + esc(error) + '</div>'
              if error else "")
    body = (LOGIN_BODY.replace("__ERROR__", banner)
                      .replace("__NEXT__", esc(next or "/")))
    return page("Sign in", body, "")


@app.post("/login")
def do_login(username: str = Form(default=""), password: str = Form(default=""),
             next: str = Form(default="/")):
    account = load_users().get(username.strip())
    if not account or not _password_ok(password, account["password"]):
        return RedirectResponse(
            "/login?error=" + quote("That username and password did not match.")
            + "&next=" + quote(next or "/"), status_code=303)

    target = next if (next or "").startswith("/") else "/"
    reply = RedirectResponse(target, status_code=303)
    reply.set_cookie(
        SESSION_COOKIE, make_session(username.strip(),
                                     account.get("role", "admin")),
        max_age=None if SIGN_OUT_ON_BROWSER_CLOSE else SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
    )
    return reply


@app.get("/logout")
def do_logout():
    reply = RedirectResponse("/login", status_code=303)
    reply.delete_cookie(SESSION_COOKIE)
    return reply


@app.middleware("http")
async def require_sign_in(request, call_next):
    """Everything is private unless it is explicitly listed as public."""
    path = request.url.path

    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)

    session = read_session(request.cookies.get(SESSION_COOKIE, ""))
    if not session:
        if path.startswith("/api/"):
            return JSONResponse({"error": "Not signed in."}, status_code=401)
        return RedirectResponse("/login?next=" + quote(path), status_code=303)

    if session["role"] != "admin" and path.startswith(ADMIN_PREFIXES):
        if path.startswith("/api/"):
            return JSONResponse(
                {"error": "This account cannot change anything."},
                status_code=403)
        return RedirectResponse(
            "/login?error=" + quote("That account cannot enrol or remove people."),
            status_code=303)

    request.state.session = session
    return await call_next(request)


STYLES = """
:root{
  --ink:#14162B;
  --navy:#232653;
  --gold:#D8A03D;
  --gold-dim:#8E6822;
  --paper:#EDEDF2;
  --card:#FFFFFF;
  --line:#DEDEE7;
  --text:#1B1D2E;
  --muted:#6B6E88;
  --green:#1B7A57;
  --green-bg:#E4F2EC;
  --madder:#B23A2B;
  --madder-bg:#FAEAE7;
  --amber:#8A6410;
  --amber-bg:#FBF1DE;
  --rail:232px;
  --sans:'Inter',-apple-system,'Segoe UI',Roboto,sans-serif;
  --display:'Archivo','Inter',-apple-system,'Segoe UI',sans-serif;
  --mono:'IBM Plex Mono','SFMono-Regular',Consolas,'Courier New',monospace;
}
*{box-sizing:border-box;margin:0;padding:0;}
html{-webkit-text-size-adjust:100%;}
body{
  font-family:var(--sans);background:var(--paper);color:var(--text);
  font-size:14px;line-height:1.5;letter-spacing:-0.005em;
  -webkit-font-smoothing:antialiased;
}
a{color:inherit;text-decoration:none;}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px;border-radius:4px;}

/* ---------- left rail ---------- */
.rail{
  position:fixed;inset:0 auto 0 0;width:var(--rail);background:var(--ink);
  display:flex;flex-direction:column;z-index:20;
  /* selvedge: the finished edge of woven cloth */
  box-shadow:inset -1px 0 0 rgba(216,160,61,.28);
}
.rail-brand{display:flex;align-items:center;gap:11px;padding:22px 20px 24px;}
.rail-brand img{height:34px;width:auto;background:#fff;border-radius:6px;padding:3px;}
.rail-brand b{
  display:block;font-family:var(--display);font-weight:700;font-size:15px;
  color:#fff;letter-spacing:.16em;line-height:1.1;
}
.rail-brand span{
  display:block;font-size:10.5px;color:var(--gold);letter-spacing:.2em;
  text-transform:uppercase;margin-top:3px;font-weight:600;
}
.rail nav{display:flex;flex-direction:column;gap:1px;padding:0 12px;}
.rail nav a{
  display:flex;align-items:center;gap:11px;padding:10px 12px;border-radius:8px;
  color:#9C9FBF;font-size:13.5px;font-weight:500;transition:background .12s,color .12s;
}
.rail nav a svg{width:17px;height:17px;flex:none;stroke-width:1.7;}
.rail nav a{white-space:nowrap;}
.rail nav a:hover{background:rgba(255,255,255,.05);color:#fff;}
.rail nav a.on{background:var(--navy);color:#fff;box-shadow:inset 2px 0 0 var(--gold);}
.rail-foot{margin-top:auto;padding:18px 20px;border-top:1px solid rgba(255,255,255,.07);}
.pulse{display:flex;align-items:center;gap:8px;font-size:11.5px;color:#9C9FBF;
  letter-spacing:.08em;text-transform:uppercase;font-weight:600;}
.pulse i{
  width:7px;height:7px;border-radius:50%;background:var(--gold);flex:none;
  animation:beat 2.4s ease-in-out infinite;
}
@keyframes beat{0%,100%{opacity:1;}50%{opacity:.28;}}
.rail-foot small{display:block;margin-top:9px;font-size:11px;color:#5A5D7C;}

/* ---------- page frame ---------- */
.wrap{margin-left:var(--rail);padding:30px 34px 56px;max-width:1320px;}
.head{display:flex;align-items:baseline;justify-content:space-between;
  gap:20px;flex-wrap:wrap;margin-bottom:22px;}
.eyebrow{font-size:11px;letter-spacing:.19em;text-transform:uppercase;
  color:var(--muted);font-weight:600;margin-bottom:6px;}
h1{font-family:var(--display);font-size:27px;font-weight:700;letter-spacing:-.026em;}
h2{font-family:var(--display);font-size:15px;font-weight:600;letter-spacing:-.01em;
  margin-bottom:12px;}
.stamp{font-family:var(--mono);font-size:12.5px;color:var(--muted);}

/* ---------- stat strip ---------- */
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);
  border-radius:12px;overflow:hidden;margin-bottom:26px;}
.strip div{background:var(--card);padding:14px 18px;}
.strip b{display:block;font-family:var(--mono);font-size:23px;font-weight:600;
  letter-spacing:-.02em;}
.strip span{display:block;font-size:10.5px;letter-spacing:.15em;text-transform:uppercase;
  color:var(--muted);font-weight:600;margin-top:3px;}
/* the headline figure: how many people are in the building right now */
.strip div.lead{background:var(--ink);}
.strip div.lead b{color:var(--gold);font-size:31px;}
.strip div.lead span{color:#9C9FBF;}

/* ---------- tabs ---------- */
.tabs{display:flex;gap:4px;margin-bottom:16px;border-bottom:1px solid var(--line);
  flex-wrap:wrap;}
.tab-btn{background:transparent;border:none;padding:10px 16px;font-size:13.5px;
  font-weight:600;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;
  font-family:var(--sans);transition:color .12s,border-color .12s;}
.tab-btn:hover{color:var(--text);}
.tab-btn.on{color:var(--ink);border-bottom-color:var(--gold);}
.tab-btn.disabled-cam{color:var(--muted);font-style:italic;opacity:.7;}

/* ---------- surfaces ---------- */
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:22px;margin-bottom:18px;}
.panel-tight{padding:0;overflow-x:auto;}
.note{font-size:12.5px;color:var(--muted);margin-bottom:14px;max-width:62ch;}

/* A recognition pass that has crashed loops quietly forever. Without this it
   was invisible on the main screen — the board looked normal while the exit
   detector had been dead for hours. */
.fault{background:var(--madder-bg);border:1px solid var(--madder);
  color:var(--madder);border-radius:10px;padding:12px 16px;margin-bottom:16px;
  font-size:13px;}
.fault b{display:block;font-family:var(--display);margin-bottom:4px;}
.fault div{font-family:var(--mono);font-size:12px;margin-top:3px;}

/* ---------- table ---------- */
table{width:100%;border-collapse:collapse;min-width:600px;}
thead th{
  background:var(--card);text-align:left;font-size:10.5px;letter-spacing:.15em;
  text-transform:uppercase;color:var(--muted);font-weight:600;
  padding:13px 18px;border-bottom:1px solid var(--line);
  position:sticky;top:0;z-index:2;
}
tbody td{padding:11px 18px;border-bottom:1px solid #F1F1F6;font-size:13.5px;
  vertical-align:middle;}
tbody tr:last-child td{border-bottom:none;}
tbody tr:hover td{background:#FAFAFD;}
td.num,th.num{font-family:var(--mono);font-size:13px;letter-spacing:-.01em;}
.avatar{width:32px;height:32px;border-radius:50%;object-fit:cover;
  background:#E6E6EE;display:block;}
.avatar.ring{box-shadow:0 0 0 2px var(--gold);}
.blank{width:32px;height:32px;border-radius:50%;background:#E6E6EE;}
.empty{text-align:center;color:var(--muted);padding:44px 18px;font-size:13.5px;}
.empty b{display:block;font-family:var(--display);font-size:15px;color:var(--text);
  margin-bottom:5px;}

/* ---------- pills ---------- */
.pill{display:inline-block;padding:3px 11px;border-radius:20px;font-size:11px;
  font-weight:600;letter-spacing:.06em;text-transform:uppercase;font-family:var(--mono);}
.pill.in{background:var(--green-bg);color:var(--green);}
.pill.out{background:var(--madder-bg);color:var(--madder);}
.pill.yes{background:var(--green-bg);color:var(--green);}
.pill.no{background:var(--madder-bg);color:var(--madder);}
.pill.warn{background:var(--amber-bg);color:var(--amber);}

/* ---------- controls ---------- */
.btn{
  display:inline-flex;align-items:center;gap:7px;background:var(--ink);color:#fff;
  border:1px solid var(--ink);padding:9px 17px;border-radius:8px;font-size:13px;
  font-weight:600;font-family:var(--sans);cursor:pointer;transition:background .12s;
}
.btn:hover{background:var(--navy);}
.btn:disabled{opacity:.5;cursor:not-allowed;}
.btn.ghost{background:transparent;color:var(--text);border-color:var(--line);}
.btn.ghost:hover{background:#F4F4F9;}
.btn.gold{background:var(--gold);border-color:var(--gold);color:#2A1D05;}
.btn.gold:hover{background:#C58F2F;}
.btn.danger{background:var(--madder);border-color:var(--madder);}
.btn.small{padding:5px 12px;font-size:12px;}
.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center;}

/* ---------- confirm modal ----------
   Replaces the browser's native confirm()/alert() boxes -- those show the
   raw domain name ("attendance.kenhrms.com says") and can't be styled,
   which reads as unfinished for a production system. Used via the
   kenConfirm() helper defined in page()'s footer script. */
.kc-overlay{position:fixed;inset:0;background:rgba(20,22,43,.55);
  display:none;align-items:center;justify-content:center;z-index:999;padding:16px;}
.kc-overlay.show{display:flex;}
.kc-modal{background:var(--card);border-radius:12px;max-width:420px;width:100%;
  padding:24px 26px;box-shadow:0 24px 60px rgba(20,22,43,.4);font-family:var(--sans);}
.kc-modal h3{margin:0 0 10px;font-size:16px;color:var(--ink);font-weight:700;}
.kc-modal p{margin:0 0 22px;font-size:13.5px;color:var(--text);line-height:1.55;}
.kc-modal .row{justify-content:flex-end;}

label{display:block;font-size:11px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted);font-weight:600;margin:16px 0 6px;}
/* Every text-like input, not just a couple of types. The original list left
   out password, so the password box on the sign-in page missed width:100% and
   all the styling — it rendered narrow and unstyled next to the username box,
   which looked like a broken layout rather than a missing selector. */
input[type=text],input[type=password],input[type=email],input[type=number],
input[type=date],input[type=search],input:not([type]),select{
  width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;
  font-size:13.5px;font-family:var(--sans);background:#fff;color:var(--text);
}
input[type=file]{font-size:13px;font-family:var(--sans);}
input:focus,select:focus{border-color:var(--ink);outline:none;
  box-shadow:0 0 0 3px rgba(35,38,83,.09);}

/* ---------- video ---------- */
/* The feed is 960px wide from the server, and left alone it stretched to fill
   whatever width the panel had — on a wide monitor that is an enormous picture
   for something you only glance at. */
.stage{background:#0B0C16;border-radius:11px;overflow:hidden;line-height:0;
  border:1px solid var(--line);max-width:640px;}
.stage img,.stage video{width:100%;display:block;}
.readout{margin-top:12px;padding:12px 15px;border-radius:9px;background:#F4F4F9;
  border:1px solid var(--line);font-size:13.5px;font-family:var(--mono);}
.log{margin-top:12px;display:flex;flex-direction:column;gap:6px;
  max-height:280px;overflow-y:auto;}
.log div{padding:8px 13px;border-radius:7px;background:#fff;
  border:1px solid var(--line);border-left:3px solid var(--muted);font-size:12.5px;
  font-family:var(--mono);}
.err{color:var(--madder);font-size:12.5px;margin-top:8px;}
.shots{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px;}
.shots figure{text-align:center;}
.shots img{width:82px;height:82px;object-fit:cover;border-radius:8px;
  border:1px solid var(--line);display:block;}
.shots a{font-size:11.5px;color:var(--madder);}
.shots figcaption{margin-top:2px;line-height:1.3;max-width:82px;}
.add-tile{width:44px;height:44px;border-radius:8px;border:1.5px dashed var(--line);
  background:#fff;color:var(--muted);font-size:22px;line-height:1;cursor:pointer;
  display:inline-flex;align-items:center;justify-content:center;margin-top:12px;
  padding:0;}
.add-tile:hover{border-color:var(--madder);color:var(--madder);}
.gallery{display:flex;gap:14px;flex-wrap:wrap;}
.gallery figure{text-align:center;}
.gallery img{width:146px;height:146px;object-fit:cover;border-radius:10px;
  border:1px solid var(--line);display:block;}
.gallery a{font-size:12px;color:var(--muted);display:inline-block;margin-top:5px;}
.banner{background:var(--green-bg);color:var(--green);padding:11px 15px;
  border-radius:9px;font-size:13.5px;margin-bottom:16px;font-weight:500;}

/* ---------- responsive ---------- */
@media (max-width:900px){
  :root{--rail:0px;}
  .rail{position:static;width:100%;flex-direction:row;align-items:center;
    flex-wrap:wrap;padding:0 14px;box-shadow:inset 0 -1px 0 rgba(216,160,61,.28);}
  .rail-brand{padding:14px 10px 14px 0;}
  .rail nav{flex-direction:row;overflow-x:auto;padding:0 0 10px;flex:1 1 100%;}
  .rail-foot{display:none;}
  .wrap{margin-left:0;padding:20px 16px 44px;}
  .strip{grid-template-columns:1fr;}
}
@media (prefers-reduced-motion:reduce){
  *{animation:none !important;transition:none !important;}
}
"""


ICONS = {
    "today": '<path d="M3 9h18M7 3v3m10-3v3M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z"/>',
    "live":  '<path d="M2 7a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z"/><path d="m15 10 5-3.5v11L15 14z"/>',
    "enrol": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/>',
    "people":'<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    "report":'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 15h6M9 11h2"/>',
}

NAV = [("/", "today", "Today"),
       ("/mark", "live", "Live camera"),
       ("/enroll", "enrol", "Enrol"),
       ("/people", "people", "People"),
       ("/reports", "report", "Reports")]


def page(title, body, active="/", who=None):
    links = ""
    for href, icon, text in NAV:
        cls = ' class="on"' if href == active else ""
        links += ('<a href="' + href + '"' + cls + '>'
                  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                  'stroke-linecap="round" stroke-linejoin="round">'
                  + ICONS[icon] + '</svg>' + text + '</a>')

    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + title + ' — KEN Attendance</title>'
        '<link rel="icon" href="/static/logo.png">'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700&'
        'family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&'
        'display=swap" rel="stylesheet">'
        '<style>' + STYLES + '</style></head><body>'
        '<aside class="rail">'
        '<div class="rail-brand"><img src="/static/logo.png" alt="">'
        '<span><b>KEN</b><span>Attendance</span></span></div>'
        '<nav>' + links + '</nav>'
        '<div class="rail-foot"><span class="pulse"><i></i>System running</span>'
        '<small>KEN India Group · Ichalkaranji<br>' + BUILD + '</small>'
        '<small style="margin-top:8px;"><a href="/logout" '
        'style="color:var(--gold);">Sign out</a></small></div>'
        '</aside>'
        '<main class="wrap">' + body + '</main>'
        '<div class="kc-overlay" id="kcOverlay">'
        '<div class="kc-modal">'
        '<h3 id="kcTitle">Please confirm</h3>'
        '<p id="kcMessage"></p>'
        '<div class="row">'
        '<button type="button" class="btn ghost" id="kcCancel">Cancel</button>'
        '<button type="button" class="btn danger" id="kcOk">OK</button>'
        '</div></div></div>'
        '<script>'
        '(function(){'
        'var ov=document.getElementById("kcOverlay"),'
        'ti=document.getElementById("kcTitle"),'
        'ms=document.getElementById("kcMessage"),'
        'okBtn=document.getElementById("kcOk"),'
        'cnBtn=document.getElementById("kcCancel"),'
        'pending=null;'
        'window.kenConfirm=function(message,opts){'
        'opts=opts||{};'
        'ti.textContent=opts.title||"Please confirm";'
        'ms.textContent=message;'
        'okBtn.textContent=opts.okText||"OK";'
        'okBtn.className="btn"+(opts.danger===false?"":" danger");'
        'ov.classList.add("show");'
        'return new Promise(function(resolve){pending=resolve;});'
        '};'
        'function close(result){ov.classList.remove("show");'
        'if(pending){var r=pending;pending=null;r(result);}}'
        'okBtn.onclick=function(){close(true);};'
        'cnBtn.onclick=function(){close(false);};'
        'ov.addEventListener("click",function(e){if(e.target===ov)close(false);});'
        'document.addEventListener("submit",function(e){'
        'var f=e.target;'
        'if(f.classList&&f.classList.contains("kc-confirm-form")&&!f.dataset.kcConfirmed){'
        'e.preventDefault();'
        'window.kenConfirm(f.dataset.kcMessage||"Are you sure?",'
        '{okText:f.dataset.kcOkText||"OK",title:f.dataset.kcTitle||"Please confirm"})'
        '.then(function(ok){if(ok){f.dataset.kcConfirmed="1";f.submit();}});'
        '}'
        '});'
        '})();'
        '</script>'
        '</body></html>'
    )


def esc(value):
    """Minimal HTML escaping for names and IDs coming out of the database."""
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def get_punches_for_date(day_str):
    """Every individual punch for the day, oldest first."""
    conn = db()
    cur = conn.cursor()
    start, end = day_bounds(day_str)
    cur.execute("SELECT emp_id, name, ts, direction, source, camera FROM events "
                "WHERE ts >= %s AND ts < %s ORDER BY ts;", (start, end))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"emp_id": e, "name": n or "", "ts": t, "direction": d,
             "source": s or "camera", "camera": c or ""}
            for e, n, t, d, s, c in rows]


def presence_from(punches):
    """emp_id -> direction of that person's most recent punch today."""
    latest = {}
    for p in punches:
        latest[p["emp_id"]] = p["direction"]
    return latest


def get_presence_for_date(day_str):
    """emp_id -> first seen, last seen and sighting count for that day."""
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT emp_id, name, first_seen, last_seen, sightings "
                "FROM presence WHERE day = %s;", (day_str,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {e: {"name": n or "", "first_seen": f, "last_seen": l, "sightings": c}
            for e, n, f, l, c in rows}


def enrolled_people():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT emp_id, name FROM employees ORDER BY emp_id;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(r[0], r[1] or "") for r in rows]


def in_office_now(punches, presence):
    """People whose most recent punch is an IN, with how long since the camera last saw them."""
    last_in = {}
    for p in punches:
        if p["direction"] == "IN":
            last_in[p["emp_id"]] = (p["ts"], p.get("camera", ""))
        else:
            last_in.pop(p["emp_id"], None)

    with _exit_lock:
        armed = set(_exit_armed.keys())

    now = datetime.now(IST).replace(tzinfo=None)
    rows = []
    for emp_id, (in_ts, in_camera) in last_in.items():
        seen = presence.get(emp_id, {})
        last_seen = seen.get("last_seen") or in_ts
        quiet = (now - last_seen).total_seconds() / 60.0
        rows.append({
            "emp_id": emp_id,
            "name": seen.get("name") or "",
            "in_ts": in_ts,
            "last_seen": last_seen,
            "quiet_minutes": int(quiet),
            "stale": quiet >= PRESENCE_STALE_MINUTES,
            "leaving": emp_id in armed,
            "sightings": seen.get("sightings", 0),
            "camera": latest_seen_camera(emp_id, in_camera),
        })
    if rows:
        names = {}
        for p in punches:
            if p["name"]:
                names[p["emp_id"]] = p["name"]
        for r in rows:
            if not r["name"]:
                r["name"] = names.get(r["emp_id"], "")
    rows.sort(key=lambda r: r["in_ts"])
    return rows


def day_summary(punches):
    """One row per person for the whole day."""
    by_person = {}
    for p in punches:
        by_person.setdefault(p["emp_id"], {"name": p["name"], "punches": []})
        by_person[p["emp_id"]]["punches"].append(p)

    now = datetime.now(IST).replace(tzinfo=None)

    reported_day = punches[0]["ts"].date() if punches else now.date()
    if reported_day < now.date():
        ceiling = datetime.combine(reported_day, datetime.max.time()).replace(microsecond=0)
    else:
        ceiling = now

    out = []
    for emp_id, data in by_person.items():
        rows = data["punches"]
        ins = [r["ts"] for r in rows if r["direction"] == "IN"]
        outs = [r["ts"] for r in rows if r["direction"] == "OUT"]

        seconds = 0.0
        open_in = None
        inferred = False
        for r in rows:
            if r["direction"] == "IN" and open_in is None:
                open_in = r["ts"]
            elif r["direction"] == "OUT" and open_in is not None:
                seconds += (r["ts"] - open_in).total_seconds()
                open_in = None
                if r.get("source") in ("eod", "auto"):
                    inferred = True
        still_in = open_in is not None
        if still_in:
            seconds += max(0.0, (ceiling - open_in).total_seconds())

        out.append({
            "emp_id": emp_id,
            "name": data["name"],
            "first_in": min(ins) if ins else None,
            "last_out": max(outs) if outs else None,
            "in_count": len(ins),
            "out_count": len(outs),
            "punches": len(rows),
            "hours": round(seconds / 3600.0, 2),
            "still_in": still_in,
            "inferred": inferred,
        })
    out.sort(key=lambda r: str(r["emp_id"]))
    return out


AVATAR_FALLBACK = ("onerror=\"this.replaceWith(Object.assign("
                   "document.createElement('div'),{className:'blank'}))\"")


def avatar_cell(emp_id, ring=False):
    cls = "avatar ring" if ring else "avatar"
    return ('<td><img class="' + cls + '" src="/photo/' + esc(emp_id) + '" alt="" '
            + AVATAR_FALLBACK + '></td>')


def quiet_label(minutes):
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return str(minutes) + " min ago"
    hours = minutes // 60
    rest = minutes % 60
    return str(hours) + "h " + str(rest) + "m ago"


def fault_html(problems):
    if not problems:
        return ""
    lines = "".join('<div>' + esc(p) + '</div>' for p in problems)
    return ('<div class="fault"><b>A camera is not working</b>'
            'Attendance from it is not being recorded. Check the server console '
            'for the full error.' + lines + '</div>')


def office_rows_html(rows):
    if not rows:
        return ('<tr><td colspan="6"><div class="empty"><b>Nobody is in yet</b>'
                'People appear here as soon as the camera recognises them.'
                '</div></td></tr>')
    html = ""
    for r in rows:
        html += ('<tr>' + avatar_cell(r["emp_id"], ring=not r["stale"])
                 + '<td class="num">' + esc(r["emp_id"]) + '</td>'
                 + '<td>' + esc(r["name"]) + '</td>'
                 + '<td class="num">' + r["in_ts"].strftime("%H:%M:%S") + '</td>'
                 + '<td class="num">' + r["last_seen"].strftime("%H:%M:%S") + '</td>'
                 + '<td>' + esc(camera_name(r.get("camera", ""))) + '</td></tr>')
    return html


def punch_rows_html(punches):
    if not punches:
        return ('<tr><td colspan="6"><div class="empty">'
                '<b>Nothing recorded yet today</b>'
                'Punches appear here when someone arrives, or walks out through '
                'the exit zone.</div></td></tr>')
    html = ""
    for p in reversed(punches):
        pill = "in" if p["direction"] == "IN" else "out"
        html += ('<tr>' + avatar_cell(p["emp_id"], ring=p["direction"] == "IN")
                 + '<td class="num">' + esc(p["emp_id"]) + '</td>'
                 + '<td>' + esc(p["name"]) + '</td>'
                 + '<td class="num">' + p["ts"].strftime("%d %b") + '</td>'
                 + '<td class="num">' + p["ts"].strftime("%H:%M:%S") + '</td>'
                 + '<td><span class="pill ' + pill + '">' + esc(p["direction"])
                 + '</span></td></tr>')
    return html


TODAY_JS = """
(function(){
  var office = document.getElementById('office');
  var punches = document.getElementById('punches');
  var staleNote = document.getElementById('stale-note');
  var faults = document.getElementById('faults');

  var FALLBACK = "onerror=\\"this.replaceWith(Object.assign(" +
                 "document.createElement('div'),{className:'blank'}))\\"";

  function avatar(id, ring){
    return '<td><img class="avatar' + (ring ? ' ring' : '') +
           '" src="/photo/' + id + '" alt="" ' + FALLBACK + '></td>';
  }

  function paintOffice(rows){
    if (!rows.length){
      office.innerHTML = '<tr><td colspan="6"><div class="empty">' +
        '<b>Nobody is in yet</b>People appear here as soon as the camera ' +
        'recognises them.</div></td></tr>';
      return;
    }
    var html = '';
    for (var i = 0; i < rows.length; i++){
      var r = rows[i];
      html += '<tr>' + avatar(r.emp_id, !r.stale) +
        '<td class="num">' + r.emp_id + '</td><td>' + r.name + '</td>' +
        '<td class="num">' + r.in_at + '</td>' +
        '<td class="num">' + r.last_seen + '</td>' +
        '<td>' + r.camera + '</td></tr>';
    }
    office.innerHTML = html;
  }

  function paintPunches(rows){
    if (!rows.length){
      punches.innerHTML = '<tr><td colspan="6"><div class="empty">' +
        '<b>Nothing recorded yet today</b>Punches appear here when someone ' +
        'arrives, or walks out through the exit zone.</div></td></tr>';
      return;
    }
    var html = '';
    for (var j = rows.length - 1; j >= 0; j--){
      var p = rows[j];
      var cls = p.direction === 'IN' ? 'in' : 'out';
      html += '<tr>' + avatar(p.emp_id, p.direction === 'IN') +
        '<td class="num">' + p.emp_id + '</td><td>' + p.name + '</td>' +
        '<td class="num">' + p.date + '</td>' +
        '<td class="num">' + p.time + '</td>' +
        '<td><span class="pill ' + cls + '">' + p.direction + '</span></td></tr>';
    }
    punches.innerHTML = html;
  }

  function paintFaults(list){
    if (!list || !list.length){ faults.innerHTML = ''; return; }
    var rows = '';
    for (var i = 0; i < list.length; i++) rows += '<div>' + list[i] + '</div>';
    faults.innerHTML = '<div class="fault"><b>A camera is not working</b>' +
      'Attendance from it is not being recorded. Check the server console for ' +
      'the full error.' + rows + '</div>';
  }

  function paint(d){
    document.getElementById('present').textContent = d.present;
    document.getElementById('leaving').textContent = d.leaving;
    document.getElementById('seen').textContent = d.seen;
    document.getElementById('count').textContent = d.punch_count;
    document.getElementById('stamp').textContent = d.stamp;

    staleNote.innerHTML = d.stale
      ? '<p class="note" style="color:var(--amber);margin-bottom:12px;">' +
        d.stale + ' not seen recently — still marked IN. They may have left ' +
        'without passing the exit zone.</p>'
      : '';

    paintFaults(d.faults);
    paintOffice(d.office || []);
    paintPunches(d.punches || []);
  }

  setInterval(function(){
    fetch('/api/today').then(function(r){ return r.json(); })
      .then(paint).catch(function(){});
  }, 10000);
})();
"""


@app.get("/api/today")
def api_today():
    day_str = date.today().strftime("%Y-%m-%d")
    punches = get_punches_for_date(day_str)
    presence = get_presence_for_date(day_str)
    office = in_office_now(punches, presence)

    return {
        "stamp": datetime.now(IST).strftime("%a %d %b %Y · %H:%M:%S"),
        "present": len(office),
        "leaving": sum(1 for r in office if r["leaving"]),
        "stale": sum(1 for r in office if r["stale"]),
        "seen": len(presence),
        "punch_count": len(punches),
        "faults": [esc(p) for p in camera_problems()],
        "office": [{"emp_id": esc(r["emp_id"]), "name": esc(r["name"]),
                    "in_at": r["in_ts"].strftime("%H:%M:%S"),
                    "last_seen": r["last_seen"].strftime("%H:%M:%S"),
                    "quiet": quiet_label(r["quiet_minutes"]),
                    "stale": r["stale"], "leaving": r["leaving"],
                    "camera": esc(camera_name(r.get("camera", "")))}
                   for r in office],
        "punches": [{"emp_id": esc(p["emp_id"]), "name": esc(p["name"]),
                     "date": p["ts"].strftime("%d %b"),
                     "time": p["ts"].strftime("%H:%M:%S"),
                     "direction": p["direction"]} for p in punches],
    }


@app.get("/", response_class=HTMLResponse)
def today_page():
    day_str = date.today().strftime("%Y-%m-%d")
    punches = get_punches_for_date(day_str)
    presence = get_presence_for_date(day_str)
    office = in_office_now(punches, presence)
    stale = sum(1 for r in office if r["stale"])
    leaving = sum(1 for r in office if r["leaving"])

    stale_note = ""
    if stale:
        stale_note = ('<p class="note" style="color:var(--amber);margin-bottom:12px;">'
                      + str(stale) + ' not seen recently — still marked IN. They may '
                      'have left without passing the exit zone.</p>')

    body = (
        '<div class="head"><div>'
        '<div class="eyebrow">Attendance</div><h1>Who is in the building</h1>'
        '</div><div class="stamp" id="stamp">'
        + datetime.now(IST).strftime("%a %d %b %Y · %H:%M:%S") + '</div></div>'

        '<div id="faults">' + fault_html(camera_problems()) + '</div>'

        '<div class="strip">'
        '<div class="lead"><b id="present">' + str(len(office)) + '</b>'
        '<span>In the office</span></div>'
        '<div><b id="leaving">' + str(leaving) + '</b><span>At the exit</span></div>'
        '<div><b id="seen">' + str(len(presence)) + '</b><span>Seen today</span></div>'
        '<div><b id="count">' + str(len(punches)) + '</b><span>Punches today</span></div>'
        '</div>'

        '<h2>In the office now</h2>'
        + '<div id="stale-note">' + stale_note + '</div>'
        '<div class="panel panel-tight"><table>'
        '<thead><tr><th></th><th class="num">ID</th><th>Name</th>'
        '<th class="num">In at</th><th class="num">Last seen</th>'
        '<th>Department</th></tr></thead>'
        '<tbody id="office">' + office_rows_html(office) + '</tbody>'
        '</table></div>'

        '<h2 style="margin-top:26px;">Punch log</h2>'
        '<div class="panel panel-tight"><table>'
        '<thead><tr><th></th><th class="num">ID</th><th>Name</th>'
        '<th class="num">Date</th><th class="num">Time</th><th>Direction</th></tr></thead>'
        '<tbody id="punches">' + punch_rows_html(punches) + '</tbody>'
        '</table></div>'
        '<script>' + TODAY_JS + '</script>'
    )
    return page("Today", body, "/")


MARK_BODY = """
<div class="head"><div>
<div class="eyebrow">Camera</div><h1>Live camera</h1>
</div></div>

<div class="tabs" id="camTabs"></div>

<div class="panel">
  <h2 id="camTitle">Camera</h2>
  <p class="note">The blue box is the exit zone. Set <code>exit_zone</code> in
  CAMERAS so it covers the doorway people actually walk through.</p>
  <div class="row" style="margin-bottom:14px;">
    <button type="button" id="stopBtn" class="btn ghost">Stop this camera</button>
    <button type="button" id="startBtn" class="btn" style="display:none;">Start this camera</button>
  </div>
  <div class="stage"><img id="feed" alt="Live camera view"></div>
  <div class="readout" id="readout">Connecting to the camera…</div>
  <div class="readout" id="perf" style="font-size:12px;color:var(--muted);
       display:__DIAG__;"></div>
  <div class="readout" id="foundUrl" style="font-size:12px;color:var(--muted);
       display:__DIAG__;"></div>
  <div class="err" id="feedErr"></div>
  <div class="log" id="feedLog"></div>
</div>

<div class="panel">
  <h2>Test with this PC's webcam</h2>

  <label for="camPick">Camera</label>
  <select id="camPick"><option value="">Open the camera to list what's available</option></select>

  <div style="position:relative;display:inline-block;margin-top:12px;">
    <video id="cam" autoplay playsinline style="max-width:460px;width:100%;border-radius:11px;background:#0B0C16;display:none;"></video>
    <canvas id="overlay" style="position:absolute;top:0;left:0;pointer-events:none;"></canvas>
  </div>
  <canvas id="shot" style="display:none;"></canvas>

  <div class="row" style="margin-top:12px;">
    <button type="button" id="openBtn" class="btn ghost">Open camera</button>
    <button type="button" id="scanBtn" class="btn gold" style="display:none;">Start scanning</button>
    <button type="button" id="closeBtn" class="btn ghost" style="display:none;">Close camera</button>
  </div>
  <div class="err" id="camErr"></div>
  <div class="readout" id="testOut">Camera is off.</div>
  <div class="log" id="testLog"></div>
</div>

<script>
(function(){
  var tabs = document.getElementById('camTabs');
  var title = document.getElementById('camTitle');
  var feed = document.getElementById('feed');
  var readout = document.getElementById('readout');
  var errBox = document.getElementById('feedErr');
  var logBox = document.getElementById('feedLog');
  var perf = document.getElementById('perf');
  var found = document.getElementById('foundUrl');
  var stopBtn = document.getElementById('stopBtn');
  var startBtn = document.getElementById('startBtn');
  var timer = null, seen = {}, live = false, currentCam = '', cams = [];

  function log(text, colour){
    var line = document.createElement('div');
    line.style.borderLeftColor = colour;
    line.textContent = new Date().toLocaleTimeString() + '  ' + text;
    logBox.prepend(line);
    while (logBox.children.length > 40) logBox.removeChild(logBox.lastChild);
  }

  function attach(){
    if (!live) return;
    feed.removeAttribute('src');
    feed.src = '/api/ip_camera/mjpeg?cam=' + encodeURIComponent(currentCam) +
               '&t=' + Date.now();
  }
  feed.onerror = function(){ if (live) setTimeout(attach, 2000); };

  function paintTabs(){
    tabs.innerHTML = '';
    cams.forEach(function(c){
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'tab-btn' + (c.id === currentCam ? ' on' : '') +
                    (c.enabled === false ? ' disabled-cam' : '');
      b.textContent = c.name + (c.enabled === false ? '  (disabled — click Start to run it)'
                                : (c.recognition ? '' : '  (view only)'));
      b.onclick = function(){ select(c.id); };
      tabs.appendChild(b);
    });
  }

  function select(id){
    if (id === currentCam && live) return;
    currentCam = id;
    logBox.innerHTML = '';
    seen = {};
    perf.textContent = '';
    var c = cams.filter(function(x){ return x.id === id; })[0];
    title.textContent = c ? c.name : 'Camera';
    paintTabs();
    if (c && c.enabled === false){
      live = false;
      if (timer) clearInterval(timer);
      feed.removeAttribute('src');
      errBox.textContent = '';
      readout.textContent = 'This camera is disabled in local_settings. ' +
        'It will NOT start on its own — click "Start this camera" below ' +
        'if you really want to run it now.';
      stopBtn.style.display = 'none';
      startBtn.style.display = '';
      return;
    }
    readout.textContent = 'Switching camera…';
    begin();
  }

  function poll(){
    fetch('/api/ip_camera/status?cam=' + encodeURIComponent(currentCam))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d.error){
        readout.textContent = d.error;
        perf.textContent = '';
        return;
      }
      if (d.phase === 'finding'){
        readout.textContent = 'Working out this camera\\u2019s address\\u2026 trying ' +
          'the paths used by each maker. This takes up to a minute the first time.';
        perf.textContent = '';
        return;
      }
      if (d.phase === 'connecting'){
        readout.textContent = 'Connecting to the camera\\u2026 no video yet.';
        perf.textContent = '';
        return;
      }
      if (d.phase === 'idle'){
        readout.textContent = 'This camera is stopped. Nothing is being recorded from it.';
        perf.textContent = '';
        return;
      }
      if (!d.recognition){
        readout.textContent = 'Live. This camera is view-only — no faces are ' +
                              'looked for and no attendance is recorded from it.';
        perf.textContent = '';
        return;
      }

      var t = d.timing || {};
      var total = (t.exit_ran || 0) + (t.exit_skipped || 0);
      var skipped = total ? Math.round(100 * (t.exit_skipped || 0) / total) : 0;
      perf.textContent =
        'room pass ' + (t.room_ms || 0) + ' ms every ' + __ROOM__ + ' s' +
        '   ·   exit pass ' + (t.exit_ms || 0) + ' ms every ' + __EXIT__ + ' s' +
        '   ·   exit skipped ' + skipped + '% (no motion)' +
        '   ·   faces in room ' + (t.faces || 0) +
        '   ·   pending exits ' + (d.armed || 0);

      var faces = d.faces || [];
      readout.textContent = faces.length
        ? faces.map(function(f){ return f.label; }).join('   ')
        : 'Live. No faces in view.';

      if (d.found_brand && found){
        found.textContent = 'Address found automatically (' + d.found_brand +
          ' style): ' + d.found_url;
      } else if (found){
        found.textContent = '';
      }

      faces.forEach(function(f){
        var e = f.event;
        if (!e) return;
        var now = Date.now();
        if (e.status === 'recorded'){
          if (!seen[f.label] || now - seen[f.label] > 30000){
            log(f.label + ' marked ' + e.direction + ' at ' + e.time, '#1B7A57');
            seen[f.label] = now;
          }
        } else if (e.status === 'leaving'){
          var k = 'leave-' + e.emp_id;
          if (!seen[k] || now - seen[k] > 30000){
            log(e.name + ' is at the exit — OUT will be written if they do ' +
                'not come back', '#8A6410');
            seen[k] = now;
          }
        }
      });
    }).catch(function(){});
  }

  function begin(){
    errBox.textContent = '';
    readout.textContent = 'Connecting to the camera…';
    live = true;
    startBtn.style.display = 'none';
    stopBtn.style.display = '';
    var fd = new FormData();
    fd.append('cam', currentCam);
    fetch('/api/ip_camera/connect', { method:'POST', body:fd })
      .then(function(){ attach(); })
      .catch(function(err){ errBox.textContent = 'Could not connect: ' + err.message; });
    if (timer) clearInterval(timer);
    timer = setInterval(poll, 1500);
  }

  stopBtn.onclick = function(){
    live = false;
    var fd = new FormData();
    fd.append('cam', currentCam);
    fetch('/api/ip_camera/stop', { method:'POST', body:fd });
    feed.removeAttribute('src');
    if (timer) clearInterval(timer);
    readout.textContent = 'This camera is stopped. Nothing is being recorded from it.';
    stopBtn.style.display = 'none';
    startBtn.style.display = '';
  };
  startBtn.onclick = begin;

  fetch('/api/cameras').then(function(r){ return r.json(); })
  .then(function(d){
    cams = d.cameras || [];
    if (!cams.length){ readout.textContent = 'No cameras are configured.'; return; }
    select(d.default && cams.some(function(c){ return c.id === d.default; })
           ? d.default : cams[0].id);
  })
  .catch(function(err){ errBox.textContent = 'Could not list cameras: ' + err.message; });
})();
</script>

<script>
(function(){
  var video = document.getElementById('cam');
  var overlay = document.getElementById('overlay');
  var shot = document.getElementById('shot');
  var pick = document.getElementById('camPick');
  var openBtn = document.getElementById('openBtn');
  var scanBtn = document.getElementById('scanBtn');
  var closeBtn = document.getElementById('closeBtn');
  var errBox = document.getElementById('camErr');
  var out = document.getElementById('testOut');
  var logBox = document.getElementById('testLog');
  var stream = null, scanning = false, timer = null, busy = false, seen = {};

  function log(text, colour){
    var line = document.createElement('div');
    line.style.borderLeftColor = colour;
    line.textContent = new Date().toLocaleTimeString() + '  ' + text;
    logBox.prepend(line);
  }

  function start(deviceId){
    if (stream) stream.getTracks().forEach(function(t){ t.stop(); });
    var want = deviceId ? { video:{ deviceId:{ exact:deviceId } } } : { video:true };
    return navigator.mediaDevices.getUserMedia(want).then(function(s){
      stream = s;
      video.srcObject = s;
      video.style.display = 'block';
      scanBtn.style.display = '';
      closeBtn.style.display = '';
      openBtn.style.display = 'none';
    });
  }

  function listCameras(){
    return navigator.mediaDevices.enumerateDevices().then(function(devs){
      var cams = devs.filter(function(d){ return d.kind === 'videoinput'; });
      pick.innerHTML = '';
      if (!cams.length){ pick.innerHTML = '<option value="">No cameras found</option>'; return; }
      cams.forEach(function(c, i){
        var o = document.createElement('option');
        o.value = c.deviceId;
        o.textContent = c.label || ('Camera ' + (i + 1));
        pick.appendChild(o);
      });
    });
  }

  openBtn.onclick = function(){
    errBox.textContent = '';
    start(pick.value || null).then(listCameras).then(function(){
      out.textContent = 'Camera ready. Start scanning when you are.';
    }).catch(function(err){
      errBox.textContent = 'Could not open the camera: ' + err.message +
        '. Browsers only allow camera access over localhost or HTTPS, so use ' +
        'this page on the server PC itself.';
    });
  };

  pick.onchange = function(){
    if (!stream) return;
    start(pick.value).catch(function(err){
      errBox.textContent = 'Could not switch camera: ' + err.message;
    });
  };

  function stopScan(){
    scanning = false;
    if (timer) { clearInterval(timer); timer = null; }
    scanBtn.textContent = 'Start scanning';
    scanBtn.className = 'btn gold';
  }

  closeBtn.onclick = function(){
    stopScan();
    if (stream) stream.getTracks().forEach(function(t){ t.stop(); });
    stream = null;
    video.style.display = 'none';
    overlay.getContext('2d').clearRect(0, 0, overlay.width, overlay.height);
    scanBtn.style.display = 'none';
    closeBtn.style.display = 'none';
    openBtn.style.display = '';
    out.textContent = 'Camera is off.';
  };

  function draw(faces, w, h){
    overlay.width = video.clientWidth;
    overlay.height = video.clientHeight;
    var ctx = overlay.getContext('2d');
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    var sx = video.clientWidth / w, sy = video.clientHeight / h;
    faces.forEach(function(f){
      var x = f.box[0]*sx, y = f.box[1]*sy;
      var bw = (f.box[2]-f.box[0])*sx, bh = (f.box[3]-f.box[1])*sy;
      var colour = f.recognized ? '#1B7A57' : '#B23A2B';
      ctx.lineWidth = 2; ctx.strokeStyle = colour;
      ctx.strokeRect(x, y, bw, bh);
      var text = f.recognized ? (f.name + ' (' + f.emp_id + ')') : 'Not recognised';
      ctx.font = '600 13px IBM Plex Mono, monospace';
      ctx.fillStyle = colour;
      ctx.fillRect(x, y - 20, ctx.measureText(text).width + 12, 20);
      ctx.fillStyle = '#fff';
      ctx.fillText(text, x + 6, y - 6);
    });
  }

  function scanOnce(){
    if (busy || !video.videoWidth) return;
    busy = true;
    shot.width = video.videoWidth;
    shot.height = video.videoHeight;
    shot.getContext('2d').drawImage(video, 0, 0);
    shot.toBlob(function(blob){
      if (!blob) { busy = false; return; }
      var fd = new FormData();
      fd.append('image', blob, 'frame.jpg');
      fetch('/api/recognize', { method:'POST', body:fd })
      .then(function(r){ return r.json(); })
      .then(function(d){
        var faces = d.faces || [];
        draw(faces, shot.width, shot.height);
        var known = faces.filter(function(f){ return f.recognized; });
        if (known.length){
          out.textContent = 'Recognised ' + known.map(function(f){ return f.name; }).join(', ');
          known.forEach(function(f){
            if (f.event && f.event.status === 'recorded'){
              var now = Date.now();
              if (!seen[f.emp_id] || now - seen[f.emp_id] > 30000){
                log(f.name + ' (' + f.emp_id + ') marked ' + f.event.direction +
                    ' at ' + f.event.time, '#1B7A57');
                seen[f.emp_id] = now;
              }
            }
          });
        } else if (faces.length){
          out.textContent = faces.length + ' face(s) in view, none enrolled.';
        } else {
          out.textContent = 'No face in view. Look at the camera.';
        }
      })
      .catch(function(err){ out.textContent = 'Scan failed: ' + err.message; })
      .then(function(){ busy = false; });
    }, 'image/jpeg', 0.85);
  }

  scanBtn.onclick = function(){
    if (scanning){ stopScan(); out.textContent = 'Scanning stopped.'; return; }
    scanning = true;
    scanBtn.textContent = 'Stop scanning';
    scanBtn.className = 'btn danger';
    out.textContent = 'Scanning…';
    timer = setInterval(scanOnce, 1500);
  };
})();
</script>
"""


@app.get("/mark", response_class=HTMLResponse)
def mark_attendance_page():
    body = (MARK_BODY.replace("__ROOM__", str(ROOM_INTERVAL))
                     .replace("__EXIT__", str(EXIT_INTERVAL))
                     .replace("__DIAG__", "block" if ZONE_DEBUG else "none"))
    return page("Live camera", body, "/mark")


@app.post("/api/recognize")
async def api_recognize(image: UploadFile = File(...)):
    """The webcam test path."""
    contents = await image.read()
    img = cv2.imdecode(np.frombuffer(contents, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return {"status": "error", "message": "That frame could not be read."}

    orig_h, orig_w = img.shape[:2]
    small = resize_for_detection(img, max_dim=INTERACTIVE_MAX_DIM)
    scale_x = orig_w / small.shape[1]
    scale_y = orig_h / small.shape[0]

    with _interactive_model_lock:
        faces = interactive_app.get(small)
    if not faces:
        return {"status": "no_face", "faces": []}

    ids, names, matrix, id_array = load_templates()
    if matrix is None:
        return {"status": "error", "message": "No one is enrolled yet.", "faces": []}

    results = []
    for face in faces:
        box = face.bbox.astype(float)
        x1 = int(box[0] * scale_x); y1 = int(box[1] * scale_y)
        x2 = int(box[2] * scale_x); y2 = int(box[3] * scale_y)

        scores = matrix @ face.normed_embedding
        best = int(scores.argmax())
        best_score = float(scores[best])

        rival_mask = id_array != ids[best]
        rival = float(scores[rival_mask].max()) if rival_mask.any() else 0.0

        if best_score >= MATCH_THRESHOLD and (best_score - rival) >= MATCH_MARGIN:
            emp_id, name = ids[best], names[best]
            event = record_attendance_event(emp_id, name, best_score, camera="webcam")
            results.append({"box": [x1, y1, x2, y2], "recognized": True,
                            "emp_id": emp_id, "name": name,
                            "score": round(best_score, 2), "event": event})
        else:
            results.append({"box": [x1, y1, x2, y2], "recognized": False,
                            "score": round(best_score, 2)})

    return {"status": "ok", "faces": results}


ENROL_BODY = """
<div class="head"><div>
<div class="eyebrow">People</div><h1>Enrol a person</h1>
</div></div>

__BANNER__

<div class="panel" style="max-width:640px;">

  <form id="enrolForm">
    <label for="emp_id">Employee ID</label>
    <input id="emp_id" name="emp_id" required placeholder="220108">
    <div class="note" id="empNote" style="margin-top:4px;"></div>

    <label for="name">Full name</label>
    <input id="name" name="name" required placeholder="Nileshkumar Narayan Das">

    <label>Photos from this PC</label>
    <div class="shots" id="fileShots"></div>
    <button type="button" id="addFileBtn" class="add-tile" title="Add photos" aria-label="Add photos">+</button>
    <input type="file" id="fileInput" accept="image/*" multiple style="display:none;">

    <label>Or take photos now</label>
    <div style="background:#F4F4F9;border:1px solid var(--line);border-radius:10px;padding:15px;">
      <label for="camPick" style="margin-top:0;">Camera</label>
      <select id="camPick"><option value="">Open the camera to list what's available</option></select>
      <video id="cam" autoplay playsinline style="max-width:340px;width:100%;border-radius:10px;background:#0B0C16;display:none;margin-top:12px;"></video>
      <canvas id="shot" style="display:none;"></canvas>
      <div class="row" style="margin-top:12px;">
        <button type="button" id="openBtn" class="btn ghost small">Open camera</button>
        <button type="button" id="grabBtn" class="btn gold small" style="display:none;">Take photo</button>
        <button type="button" id="rotateBtn" class="btn ghost small" style="display:none;">Rotate</button>
        <button type="button" id="closeBtn" class="btn ghost small" style="display:none;">Close camera</button>
      </div>
      <div class="note" id="rotateNote" style="display:none;"></div>
      <div class="err" id="camErr"></div>
      <div class="shots" id="shots"></div>
    </div>

    <label style="display:flex;align-items:center;gap:9px;text-transform:none;
                  letter-spacing:0;font-size:13px;color:var(--text);margin-top:18px;">
      <input type="checkbox" id="replaceBox" style="width:auto;margin:0;">
      Replace everything already stored for this person
    </label>

    <div class="err" id="formErr"></div>
    <button class="btn" type="submit" id="saveBtn" style="margin-top:8px;">Save angles</button>
  </form>
</div>

<script>
(function(){
  var video = document.getElementById('cam');
  var shot = document.getElementById('shot');
  var pick = document.getElementById('camPick');
  var openBtn = document.getElementById('openBtn');
  var grabBtn = document.getElementById('grabBtn');
  var rotateBtn = document.getElementById('rotateBtn');
  var rotateNote = document.getElementById('rotateNote');
  var closeBtn = document.getElementById('closeBtn');
  var camErr = document.getElementById('camErr');
  var shots = document.getElementById('shots');
  var fileInput = document.getElementById('fileInput');
  var addFileBtn = document.getElementById('addFileBtn');
  addFileBtn.onclick = function(){ fileInput.click(); };
  var form = document.getElementById('enrolForm');
  var saveBtn = document.getElementById('saveBtn');
  var formErr = document.getElementById('formErr');
  var empIdInput = document.getElementById('emp_id');
  var nameInput = document.getElementById('name');
  var replaceBox = document.getElementById('replaceBox');
  var empNote = document.getElementById('empNote');
  var fileShots = document.getElementById('fileShots');
  var stream = null, taken = [], picked = [], rotation = 0;

  function applyRotationPreview(){
    // Some webcams (often clip-on ones mounted sideways) hand the browser a
    // sideways picture with no way to tell -- unlike a phone, a USB webcam
    // carries no orientation sensor, so there is no way to auto-detect this.
    // Rotating the live preview here just turns the on-screen picture so it
    // is easier to line the face up; the actual saved photo is rotated
    // separately at capture time in grabBtn.onclick, further down.
    video.style.transform = rotation ? 'rotate(' + rotation + 'deg)' : '';
    if (rotation){
      rotateNote.style.display = '';
      rotateNote.textContent = 'Preview rotated ' + rotation + '°. Captured photos are saved '
        + 'the same way round, so if the face looks upright above, it will be saved upright too.';
    } else {
      rotateNote.style.display = 'none';
    }
  }

  var lastLookupId = '';
  var confirmedReplaceFor = '';
  function lookupEmployee(){
    var id = empIdInput.value.trim();
    if (!id || id === lastLookupId) return;
    lastLookupId = id;
    replaceBox.checked = false;
    empNote.textContent = '';
    fetch('/api/employee_lookup?emp_id=' + encodeURIComponent(id))
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.emp_id !== empIdInput.value.trim()) return;
        if (d.found_in_master && !nameInput.value.trim()){
          nameInput.value = d.name;
        }
        if (d.already_enrolled){
          empNote.textContent = (d.name || 'This employee') + ' (ID ' + id +
            ') already has ' + d.template_count + ' photo(s) enrolled.';
          if (confirmedReplaceFor !== id){
            kenConfirm((d.name || 'This employee') + ' (ID ' + id +
              ') is already enrolled. Replace their stored photos with the new ones?',
              {title:'Already enrolled', okText:'Replace photos', danger:false})
              .then(function(ok){
                confirmedReplaceFor = id;
                replaceBox.checked = ok;
              });
          }
        } else if (d.found_in_master){
          empNote.textContent = d.name + ' — new enrolment.';
        }
      })
      .catch(function(){});
  }
  empIdInput.addEventListener('blur', lookupEmployee);
  empIdInput.addEventListener('change', lookupEmployee);

  function start(deviceId){
    if (stream) stream.getTracks().forEach(function(t){ t.stop(); });
    var want = deviceId ? { video:{ deviceId:{ exact:deviceId } } } : { video:true };
    return navigator.mediaDevices.getUserMedia(want).then(function(s){
      stream = s;
      video.srcObject = s;
      video.style.display = 'block';
      grabBtn.style.display = '';
      rotateBtn.style.display = '';
      closeBtn.style.display = '';
      openBtn.style.display = 'none';
      applyRotationPreview();
    });
  }

  rotateBtn.onclick = function(){
    rotation = (rotation + 90) % 360;
    applyRotationPreview();
  };

  function listCameras(){
    return navigator.mediaDevices.enumerateDevices().then(function(devs){
      var cams = devs.filter(function(d){ return d.kind === 'videoinput'; });
      pick.innerHTML = '';
      if (!cams.length){ pick.innerHTML = '<option value="">No cameras found</option>'; return; }
      cams.forEach(function(c, i){
        var o = document.createElement('option');
        o.value = c.deviceId;
        o.textContent = c.label || ('Camera ' + (i + 1));
        pick.appendChild(o);
      });
    });
  }

  function cameraErrorMessage(err){
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
      return 'This browser will not allow camera access here. Browsers only ' +
        'allow it over "localhost" or HTTPS -- open this page on the server PC ' +
        'itself, or over https://, and try again.';
    }
    if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError'){
      return 'Camera permission was denied. Click the camera icon in the address ' +
        'bar (or open the site settings from the browser menu) and set Camera ' +
        'to Allow, then click "Open camera" again.';
    }
    if (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError'){
      return 'No camera was found on this PC.';
    }
    if (err.name === 'NotReadableError' || err.name === 'TrackStartError'){
      return 'The camera could not be started -- another app (or another browser ' +
        'tab) is probably already using it. Close that and try again.';
    }
    return 'Could not open the camera: ' + err.message;
  }

  openBtn.onclick = function(){
    camErr.textContent = '';
    start(pick.value || null).then(listCameras).catch(function(err){
      camErr.textContent = cameraErrorMessage(err);
    });
  };

  pick.onchange = function(){
    if (!stream) return;
    start(pick.value).catch(function(err){
      camErr.textContent = cameraErrorMessage(err);
    });
  };

  closeBtn.onclick = function(){
    if (stream) stream.getTracks().forEach(function(t){ t.stop(); });
    stream = null;
    video.style.display = 'none';
    grabBtn.style.display = 'none';
    rotateBtn.style.display = 'none';
    rotateNote.style.display = 'none';
    closeBtn.style.display = 'none';
    openBtn.style.display = '';
  };

  grabBtn.onclick = function(){
    var w = video.videoWidth, h = video.videoHeight;
    var sideways = (rotation === 90 || rotation === 270);
    shot.width = sideways ? h : w;
    shot.height = sideways ? w : h;
    var ctx = shot.getContext('2d');
    ctx.save();
    ctx.translate(shot.width / 2, shot.height / 2);
    ctx.rotate(rotation * Math.PI / 180);
    ctx.drawImage(video, -w / 2, -h / 2);
    ctx.restore();
    shot.toBlob(function(blob){
      if (!blob) return;
      var index = taken.length;
      taken.push(blob);
      var fig = document.createElement('figure');
      var img = document.createElement('img');
      img.src = URL.createObjectURL(blob);
      var rm = document.createElement('a');
      rm.href = '#';
      rm.textContent = 'Remove';
      rm.onclick = function(e){ e.preventDefault(); taken[index] = null; fig.remove(); };
      fig.appendChild(img);
      fig.appendChild(rm);
      shots.appendChild(fig);
    }, 'image/jpeg', 0.92);
  };

  fileInput.onchange = function(){
    var newFiles = Array.prototype.slice.call(fileInput.files);
    newFiles.forEach(function(file){
      var index = picked.length;
      picked.push(file);
      var fig = document.createElement('figure');
      var img = document.createElement('img');
      img.src = URL.createObjectURL(file);
      var rm = document.createElement('a');
      rm.href = '#';
      rm.textContent = 'Remove';
      rm.onclick = function(e){ e.preventDefault(); picked[index] = null; fig.remove(); };
      fig.appendChild(img);
      fig.appendChild(rm);
      fileShots.appendChild(fig);
    });
    fileInput.value = '';
  };

  form.onsubmit = function(e){
    e.preventDefault();
    formErr.textContent = '';
    var empId = document.getElementById('emp_id').value.trim();
    var name = document.getElementById('name').value.trim();
    var files = picked.filter(function(f){ return f !== null; });
    var live = taken.filter(function(b){ return b !== null; });

    if (!empId || !name){ formErr.textContent = 'Employee ID and name are both needed.'; return; }
    if (!files.length && !live.length){
      formErr.textContent = 'Add at least one photo, from this PC or from the camera.';
      return;
    }

    var fd = new FormData();
    fd.append('emp_id', empId);
    fd.append('name', name);
    fd.append('replace', document.getElementById('replaceBox').checked ? 'yes' : '');
    files.forEach(function(f){ fd.append('photos', f, f.name); });
    live.forEach(function(b, i){ fd.append('photos', b, 'capture_' + (i+1) + '.jpg'); });

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    fetch('/enroll', { method:'POST', body:fd })
      .then(function(r){ window.location.href = r.url; })
      .catch(function(err){
        formErr.textContent = 'Upload failed: ' + err.message;
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save angles';
      });
  };
})();
</script>
"""


@app.get("/enroll", response_class=HTMLResponse)
def enroll_form(msg: str = ""):
    banner = '<div class="banner">' + esc(msg) + '</div>' if msg else ""
    return page("Enrol", ENROL_BODY.replace("__BANNER__", banner), "/enroll")


def store_templates(emp_id, name, vectors, label, replace):
    """Saves one template row per face, instead of averaging them into one."""
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO employees (emp_id, name, enrolled_at) VALUES (%s,%s,NOW()) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), "
                "enrolled_at=COALESCE(enrolled_at, NOW());", (emp_id, name))

    if replace:
        cur.execute("DELETE FROM templates WHERE emp_id=%s;", (emp_id,))
        next_no = 1
    else:
        cur.execute("SELECT COALESCE(MAX(template_no), 0) FROM templates "
                    "WHERE emp_id=%s;", (emp_id,))
        next_no = (cur.fetchone()[0] or 0) + 1

    added = 0
    for vec in vectors:
        if next_no > MAX_TEMPLATES_PER_PERSON:
            break
        vec = np.asarray(vec, dtype=np.float32)
        vec = vec / np.linalg.norm(vec)
        cur.execute("INSERT INTO templates (emp_id, template_no, embedding, label) "
                    "VALUES (%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE embedding=VALUES(embedding), "
                    "label=VALUES(label);",
                    (emp_id, next_no, json.dumps(vec.tolist()), label))
        next_no += 1
        added += 1

    conn.commit()
    cur.close()
    conn.close()
    invalidate_templates()
    return added


def count_templates(emp_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM templates WHERE emp_id=%s;", (emp_id,))
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return int(n or 0)


def employee_master_name(emp_id):
    """Looks up a name in the employee_master table. Returns "" if that
    table doesn't exist yet (not imported), the id isn't in it, or the DB
    is unreachable — any of those just means no auto-fill, never an error
    the operator has to deal with."""
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT full_name FROM employee_master WHERE emp_id=%s;",
                    (emp_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return (row[0] or "") if row else ""
    except Exception as e:
        print("[employee_master] lookup failed: " + str(e))
        return ""


@app.get("/api/employee_lookup")
def employee_lookup(emp_id: str = ""):
    emp_id = emp_id.strip()
    if not emp_id:
        return {"emp_id": "", "found_in_master": False, "name": "",
                "already_enrolled": False, "template_count": 0}
    name = employee_master_name(emp_id)
    templates = count_templates(emp_id)
    return {
        "emp_id": emp_id,
        "found_in_master": bool(name),
        "name": name,
        "already_enrolled": templates > 0,
        "template_count": templates,
    }


@app.post("/api/check_face")
async def check_face(photo: UploadFile = File(...)):
    """Instant per-photo feedback for the Enrol page and the person page's
    "add more photos" box. Enrolling silently drops any photo with no clear
    face -- that used to mean someone could click "Take photo" six times and
    only find out afterwards, from the final save count, that five of them
    were thrown away. This lets the page tell them right after each shot,
    while they're still in front of the camera and can just retake it."""
    contents = await photo.read()
    img = cv2.imdecode(np.frombuffer(contents, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return {"ok": False, "reason": "Not a readable image."}
    raw_h, raw_w = img.shape[:2]
    img = resize_for_detection(img, max_dim=INTERACTIVE_MAX_DIM)
    with _interactive_model_lock:
        faces = interactive_app.get(img)
    if not faces:
        # Temporary debugging aid: hand back exactly the bytes the server
        # decoded and ran detection on, base64-encoded, so a "no face found"
        # on a photo that looks fine on screen can be checked directly
        # instead of guessing whether something got mangled in between.
        debug_url = "data:image/jpeg;base64," + base64.b64encode(contents).decode("ascii")
        return {"ok": False, "reason": "No face found (received " + str(raw_w) + "x"
                + str(raw_h) + ") — retake with the face centred and well lit.",
                "debug_url": debug_url}
    faces.sort(key=lambda f: (f.bbox[3] - f.bbox[1]), reverse=True)
    f = faces[0]
    face_h = float(f.bbox[3] - f.bbox[1])
    img_h = float(img.shape[0]) or 1.0
    if (face_h / img_h) < IDENTIFY_MIN_FACE_PCT:
        return {"ok": False, "reason": "Face is too small/far — move closer or zoom in."}
    return {"ok": True, "reason": "Face found."}


@app.post("/enroll")
async def do_enroll(emp_id: str = Form(...), name: str = Form(...),
                    replace: str = Form(default=""),
                    return_to: str = Form(default=""),
                    photos: list[UploadFile] = File(...)):
    target = return_to if return_to == ("/person/" + str(emp_id)) else "/enroll"

    
    total_submitted = len(photos)
    all_photos = []
    vecs = []
    for photo in photos:
        contents = await photo.read()
        img = cv2.imdecode(np.frombuffer(contents, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        all_photos.append(contents)
        small = resize_for_detection(img, max_dim=INTERACTIVE_MAX_DIM)
        with _interactive_model_lock:
            faces = interactive_app.get(small)
        if faces:
            faces.sort(key=lambda f: (f.bbox[3] - f.bbox[1]), reverse=True)
            vecs.append(faces[0].normed_embedding)

    if not all_photos:
        return RedirectResponse(
            target + "?msg=" + str(total_submitted)
            + " photo(s) were sent but none of them could be read as an image.",
            status_code=303)

    wipe = replace == "yes"
    folder = person_photo_dir(emp_id)
    os.makedirs(folder, exist_ok=True)
    if wipe:
        for old in os.listdir(folder):
            os.remove(os.path.join(folder, old))
    existing = [i for i, _ in list_person_photos(emp_id)]
    start = (max(existing) + 1) if existing else 1
    for i, raw in enumerate(all_photos):
        with open(os.path.join(folder, str(start + i) + ".jpg"), "wb") as fh:
            fh.write(raw)

    # Always run this, even with an empty vecs list — it is also what
    # creates/updates the person's row in the People list. Skipping it when
    # no face was found would save the photos to disk but leave a brand-new
    # person invisible in the People list, since that comes from this row.
    store_templates(emp_id, name, vecs, "photo", wipe)
    total_photos = len(existing) + len(all_photos)

    # Kept deliberately simple: just confirm it worked and give one total —
    # a breakdown of how many had a usable face read as "something went
    # wrong" when nothing had.
    msg = (name + " (ID " + str(emp_id) + "): successfully enrolled — "
           + str(total_photos) + " photo(s) on file.")

    
    return RedirectResponse("/person/" + str(emp_id) + "?msg=" + msg, status_code=303)


@app.get("/photo/{emp_id}")
def get_photo(emp_id: str):
    photos = list_person_photos(emp_id)
    if not photos:
        return Response(status_code=404)
    with open(photos[0][1], "rb") as fh:
        return Response(content=fh.read(), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.get("/person_photo/{emp_id}/{idx}")
def person_photo(emp_id: str, idx: int):
    path = os.path.join(person_photo_dir(emp_id), str(idx) + ".jpg")
    if not os.path.isfile(path):
        return Response(status_code=404)
    with open(path, "rb") as fh:
        return Response(content=fh.read(), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.get("/people", response_class=HTMLResponse)
def people_list():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT e.emp_id, e.name, "
                "(SELECT COUNT(*) FROM templates t WHERE t.emp_id = e.emp_id), "
                "e.enrolled_at "
                "FROM employees e ORDER BY e.emp_id;")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    body_rows = ""
    missing_photos = 0
    thin = 0
    for sr_no, (emp_id, name, enrolled, enrolled_at) in enumerate(rows, start=1):
        shots = list_person_photos(emp_id)
        photo_count = len(shots)
        if not shots:
            missing_photos += 1
            face_cell = '<div class="blank"></div>'
            view = '<span style="color:var(--muted);font-size:12.5px;">No photos</span>'
        else:
            face_cell = ('<img class="avatar" src="/photo/' + esc(emp_id) + '" alt="">')
            view = ('<a class="btn ghost small" href="/person/' + esc(emp_id) + '">'
                    + str(photo_count) + ' photos</a>')
        
        if not photo_count:
            pill = '<span class="pill no">No face</span>'
        elif photo_count == 1:
            thin += 1
            pill = '<span class="pill warn">1 angle</span>'
        else:
            pill = ('<span class="pill yes">' + str(photo_count) + ' angles</span>')
        remove = ('<form method="post" action="/delete/' + esc(emp_id) + '" '
                  'class="kc-confirm-form" style="display:inline;" '
                  'data-kc-title="Remove employee?" '
                  'data-kc-ok-text="Remove" '
                  'data-kc-message="Remove ' + esc(name) + ' and all their '
                  'attendance records? This cannot be undone.">'
                  '<button type="submit" class="btn danger small">Remove</button>'
                  '</form>')
        enrolled_cell = (enrolled_at.strftime("%d %b %Y, %H:%M") if enrolled_at
                          else '<span style="color:var(--muted);">&mdash;</span>')
        body_rows += ('<tr><td class="num">' + str(sr_no) + '</td>'
                      '<td>' + face_cell + '</td>'
                      '<td class="num">' + esc(emp_id) + '</td>'
                      '<td>' + esc(name) + '</td>'
                      '<td>' + pill + '</td>'
                      '<td class="num">' + enrolled_cell + '</td>'
                      '<td><div class="row">' + view + remove + '</div></td></tr>')

    if not rows:
        body_rows = ('<tr><td colspan="7"><div class="empty">'
                     '<b>Nobody is enrolled</b>Add the first person from the Enrol page '
                     'and the camera will start recognising them.</div></td></tr>')

    warning = ""
    if thin:
        warning += ('<p class="note" style="color:var(--amber);margin-bottom:12px;">'
                    + str(thin) + ' with only one angle stored — add more from the '
                    'Enrol page to improve recognition.</p>')
    if missing_photos:
        warning += ('<p class="note" style="color:var(--amber);margin-bottom:12px;">'
                   + str(missing_photos) + ' without a photo on file.</p>')

    body = ('<div class="head"><div><div class="eyebrow">People</div>'
            '<h1>Enrolled people</h1></div>'
            '<a class="btn" href="/enroll">Enrol a person</a></div>'

            '<div class="strip">'
            '<div><b>' + str(len(rows)) + '</b><span>Total enrolled</span></div>'
            '</div>'

            + warning +
            '<div class="panel panel-tight"><table><thead><tr>'
            '<th class="num">Sr No</th><th></th><th class="num">ID</th><th>Name</th>'
            '<th>Angles stored</th><th class="num">Enrolled on</th><th>Actions</th>'
            '</tr></thead><tbody>' + body_rows + '</tbody></table></div>')
    return page("People", body, "/people")


@app.post("/delete/{emp_id}")
def delete_person(emp_id: str):
    folder = person_photo_dir(emp_id)
    try:
        if os.path.isdir(folder):
            for f in os.listdir(folder):
                try:
                    os.remove(os.path.join(folder, f))
                except OSError:
                    pass
            os.rmdir(folder)
    except OSError:
        pass

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM templates WHERE emp_id=%s;", (emp_id,))
    cur.execute("DELETE FROM events WHERE emp_id=%s;", (emp_id,))
    cur.execute("DELETE FROM presence WHERE emp_id=%s;", (emp_id,))
    cur.execute("DELETE FROM employees WHERE emp_id=%s;", (emp_id,))
    conn.commit()
    cur.close()
    conn.close()

    invalidate_templates()
    forget_person_state(emp_id)
    return RedirectResponse("/people", status_code=303)


@app.get("/person/{emp_id}", response_class=HTMLResponse)
def person_detail(emp_id: str, msg: str = ""):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM employees WHERE emp_id=%s;", (emp_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    name = row[0] if row else ""

    banner = '<div class="banner">' + esc(msg) + '</div>' if msg else ""

    shots = list_person_photos(emp_id)
    if shots:
        gallery = ""
        for idx, _ in shots:
            src = "/person_photo/" + esc(emp_id) + "/" + str(idx)
            gallery += ('<figure><img src="' + src + '" alt="">'
                        '<a href="' + src + '" download="' + esc(emp_id)
                        + '_' + str(idx) + '.jpg">Download</a></figure>')
        gallery = '<div class="gallery">' + gallery + '</div>'
    else:
        gallery = ('<div class="empty"><b>No photos on file</b>'
                   'Add some below, or enrol this person again. Recognition is unaffected either way.</div>')

    add_more = ('''
<div class="panel" style="max-width:640px;">
  <h2>Add more photos</h2>
  <form id="addPhotosForm">
    <label>Photos from this PC</label>
    <div class="shots" id="moreShots"></div>
    <button type="button" id="addMoreBtn" class="add-tile" title="Add photos" aria-label="Add photos">+</button>
    <input type="file" id="morePhotos" accept="image/*" multiple style="display:none;">
    <div class="err" id="moreErr"></div>
    <button class="btn" type="submit" id="moreSaveBtn" style="margin-top:8px;">Add these photos</button>
  </form>
</div>
<script>
(function(){
  var input = document.getElementById('morePhotos');
  var addBtn = document.getElementById('addMoreBtn');
  addBtn.onclick = function(){ input.click(); };
  var shotsBox = document.getElementById('moreShots');
  var form = document.getElementById('addPhotosForm');
  var errBox = document.getElementById('moreErr');
  var saveBtn = document.getElementById('moreSaveBtn');
  var picked = [];

  input.onchange = function(){
    Array.prototype.slice.call(input.files).forEach(function(file){
      var index = picked.length;
      picked.push(file);
      var fig = document.createElement('figure');
      var img = document.createElement('img');
      img.src = URL.createObjectURL(file);
      var rm = document.createElement('a');
      rm.href = '#';
      rm.textContent = 'Remove';
      rm.onclick = function(e){ e.preventDefault(); picked[index] = null; fig.remove(); };
      fig.appendChild(img);
      fig.appendChild(rm);
      shotsBox.appendChild(fig);
    });
    input.value = '';
  };

  form.onsubmit = function(e){
    e.preventDefault();
    errBox.textContent = '';
    var files = picked.filter(function(f){ return f !== null; });
    if (!files.length){ errBox.textContent = 'Pick at least one photo first.'; return; }

    var fd = new FormData();
    fd.append('emp_id', ''' + json.dumps(emp_id) + ''');
    fd.append('name', ''' + json.dumps(name or emp_id) + ''');
    fd.append('replace', '');
    fd.append('return_to', ''' + json.dumps("/person/" + emp_id) + ''');
    files.forEach(function(f){ fd.append('photos', f, f.name); });

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    fetch('/enroll', { method:'POST', body:fd })
      .then(function(r){ window.location.href = r.url; })
      .catch(function(err){
        errBox.textContent = 'Upload failed: ' + err.message;
        saveBtn.disabled = false;
        saveBtn.textContent = 'Add these photos';
      });
  };
})();
</script>
''')

    body = ('<div class="head"><div><div class="eyebrow">ID ' + esc(emp_id) + '</div>'
            '<h1>' + esc(name) + '</h1></div>'
            '<a class="btn ghost" href="/people">Back to people</a></div>'
            + banner +
            '<div class="panel">' + gallery + '</div>'
            + add_more)
    return page(esc(name) or "Person", body, "/people")


def hhmm(ts):
    return ts.strftime("%H:%M:%S") if ts else "—"


def camera_name(cam_id):
    """Friendly name for a camera id stored on a punch."""
    if not cam_id:
        return "Camera"
    for cam in CAMERAS:
        if cam["id"] == cam_id:
            return cam["name"]
    return cam_id


def source_label(punch):
    """How this punch came to exist, in words a manager can act on."""
    src = punch.get("source") or "camera"
    if src == "eod":
        return "Day end", True
    if src == "auto":
        return "Auto", True
    if src == "moved":
        return "Moved area", False
    if src == "exit":
        return "Exit zone", False
    if src == "webcam":
        return "Webcam test", False
    return camera_name(punch.get("camera", "")), False


def numbered_punches(punches):
    """Adds each punch's sequence number within that person's own day."""
    seen = {}
    out = []
    for p in punches:
        seen[p["emp_id"]] = seen.get(p["emp_id"], 0) + 1
        row = dict(p)
        row["seq"] = seen[p["emp_id"]]
        out.append(row)
    return out


TABS_JS = """
(function(){
  var btns = document.querySelectorAll('.tab-btn');
  btns.forEach(function(b){
    b.onclick = function(){
      btns.forEach(function(x){ x.classList.remove('on'); });
      b.classList.add('on');
      document.querySelectorAll('.tab-panel').forEach(function(p){
        p.style.display = 'none';
      });
      document.getElementById('tab-' + b.dataset.tab).style.display = '';
    };
  });
})();
"""


@app.get("/reports", response_class=HTMLResponse)
def reports(day: str = ""):
    if not day:
        day = date.today().strftime("%Y-%m-%d")

    punches = get_punches_for_date(day)
    presence = get_presence_for_date(day)
    detail = numbered_punches(punches)
    summary = day_summary(punches)

    log_rows = ""
    for p in detail:
        pill = "in" if p["direction"] == "IN" else "out"
        label, assumed = source_label(p)
        if assumed:
            origin = '<span class="pill no">' + esc(label) + '</span>'
        else:
            origin = ('<span style="color:var(--muted);font-size:12px;">'
                      + esc(label) + '</span>')
        log_rows += ('<tr><td><img class="avatar" src="/photo/' + esc(p["emp_id"])
                     + '" alt="" onerror="this.replaceWith(Object.assign('
                     'document.createElement(\'div\'),{className:\'blank\'}))"></td>'
                     '<td class="num">' + esc(p["emp_id"]) + '</td>'
                     '<td>' + esc(p["name"]) + '</td>'
                     '<td class="num">' + p["ts"].strftime("%d %b %Y") + '</td>'
                     '<td class="num">' + p["ts"].strftime("%H:%M:%S") + '</td>'
                     '<td><span class="pill ' + pill + '">' + esc(p["direction"])
                     + '</span></td>'
                     '<td class="num">#' + str(p["seq"]) + '</td>'
                     '<td>' + origin + '</td></tr>')
    if not detail:
        log_rows = ('<tr><td colspan="8"><div class="empty">'
                    '<b>No punches on this date</b>Nothing was recorded. Check the date, '
                    'or that the camera was running.</div></td></tr>')

    sum_rows = ""
    assumed_count = 0
    for s in summary:
        seen = presence.get(s["emp_id"], {})
        note = ' <span class="pill in">Still in</span>' if s["still_in"] else ""
        if s["inferred"]:
            note += ' <span class="pill no">Assumed OUT</span>'
            assumed_count += 1
        sum_rows += ('<tr><td><img class="avatar" src="/photo/' + esc(s["emp_id"])
                     + '" alt="" onerror="this.replaceWith(Object.assign('
                     'document.createElement(\'div\'),{className:\'blank\'}))"></td>'
                     '<td class="num">' + esc(s["emp_id"]) + '</td>'
                     '<td>' + esc(s["name"]) + note + '</td>'
                     '<td class="num">' + hhmm(s["first_in"]) + '</td>'
                     '<td class="num">' + hhmm(s["last_out"]) + '</td>'
                     '<td class="num">' + hhmm(seen.get("last_seen")) + '</td>'
                     '<td class="num">' + str(seen.get("sightings", 0)) + '</td>'
                     '<td class="num">' + str(s["punches"]) + '</td>'
                     '<td class="num">' + ("%.2f" % s["hours"]) + '</td></tr>')
    if not summary:
        sum_rows = ('<tr><td colspan="9"><div class="empty">'
                    '<b>No attendance on this date</b>Pick another date, or check that '
                    'the camera was running.</div></td></tr>')

    total_hours = sum(s["hours"] for s in summary)

    assumed_note = ""
    if assumed_count:
        assumed_note = ('<p class="note" style="color:var(--amber);">'
                        + str(assumed_count) + ' with an assumed OUT — time taken '
                        'from their last sighting. Check before using these hours.</p>')

    body = (
        '<div class="head"><div><div class="eyebrow">Reports</div>'
        '<h1>Daily attendance</h1></div></div>'

        '<div class="panel"><form method="get" action="/reports" '
        'style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;">'
        '<div><label for="day" style="margin-top:0;">Date</label>'
        '<input type="date" id="day" name="day" value="' + esc(day) + '" '
        'style="width:auto;"></div>'
        '<button class="btn" type="submit">Show</button>'
        '<a class="btn gold" href="/reports/export?day=' + esc(day) + '">Download Excel</a>'
        '<a class="btn ghost" href="/reports/export/pdf?day=' + esc(day)
        + '">Download PDF</a>'
        '</form></div>'

        '<div class="strip">'
        '<div><b>' + str(len(summary)) + '</b><span>People</span></div>'
        '<div><b>' + str(len(punches)) + '</b><span>Punches</span></div>'
        '<div><b>' + ("%.1f" % total_hours) + '</b><span>Total hours</span></div>'
        '</div>'

        + assumed_note +

        '<div class="tabs">'
        '<button type="button" class="tab-btn on" data-tab="detail">'
        'Detail — every punch</button>'
        '<button type="button" class="tab-btn" data-tab="summary">'
        'Summary — first in, last out</button>'
        '</div>'

        '<div class="tab-panel" id="tab-detail">'
        '<div class="panel panel-tight"><table><thead><tr>'
        '<th></th><th class="num">ID</th><th>Name</th><th class="num">Date</th>'
        '<th class="num">Time</th><th>Direction</th><th class="num">Punch</th>'
        '<th>Source</th></tr></thead><tbody>' + log_rows + '</tbody></table></div>'
        '</div>'

        '<div class="tab-panel" id="tab-summary" style="display:none;">'
        '<div class="panel panel-tight"><table><thead><tr>'
        '<th></th><th class="num">ID</th><th>Name</th><th class="num">First in</th>'
        '<th class="num">Last out</th><th class="num">Last seen</th>'
        '<th class="num">Sightings</th><th class="num">Punches</th>'
        '<th class="num">Hours</th>'
        '</tr></thead><tbody>' + sum_rows + '</tbody></table></div>'
        '</div>'

        '<script>' + TABS_JS + '</script>'
    )
    return page("Reports", body, "/reports")


@app.get("/reports/export")
def export_excel(day: str = ""):
    if not day:
        day = date.today().strftime("%Y-%m-%d")
    punches = get_punches_for_date(day)
    presence = get_presence_for_date(day)
    detail = numbered_punches(punches)
    summary = day_summary(punches)

    head_fill = PatternFill("solid", fgColor="14162B")
    head_font = Font(color="FFFFFF", bold=True, size=10)
    mono = Font(name="Consolas", size=10)

    def style_sheet(ws, headers, widths):
        ws.append(headers)
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for cell in ws[1]:
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.freeze_panes = "A2"

    wb = Workbook()

    ws1 = wb.active
    ws1.title = "Detail"
    style_sheet(ws1, ["ID", "Name", "Date", "Time", "Direction", "Punch No", "Source"],
                [12, 30, 14, 12, 12, 10, 14])
    for p in detail:
        label, _ = source_label(p)
        ws1.append([p["emp_id"], p["name"], p["ts"].strftime("%d %b %Y"),
                    p["ts"].strftime("%H:%M:%S"), p["direction"], p["seq"], label])
    for row in ws1.iter_rows(min_row=2):
        for cell in row:
            cell.font = mono

    ws2 = wb.create_sheet("Summary")
    style_sheet(ws2, ["ID", "Name", "First In", "Last Out", "Last Seen",
                      "Sightings", "Punches", "Hours", "Status", "OUT observed"],
                [12, 30, 12, 12, 12, 11, 10, 10, 12, 14])
    for s in summary:
        seen = presence.get(s["emp_id"], {})
        last_seen = seen.get("last_seen")
        ws2.append([s["emp_id"], s["name"],
                    s["first_in"].strftime("%H:%M:%S") if s["first_in"] else "",
                    s["last_out"].strftime("%H:%M:%S") if s["last_out"] else "",
                    last_seen.strftime("%H:%M:%S") if last_seen else "",
                    seen.get("sightings", 0), s["punches"], s["hours"],
                    "Still in" if s["still_in"] else "Left",
                    "No — assumed" if s["inferred"] else "Yes"])
    for row in ws2.iter_rows(min_row=2):
        for cell in row:
            cell.font = mono

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 'attachment; filename=attendance_' + day + '.xlsx'})


@app.get("/reports/export/pdf")
def export_pdf(day: str = ""):
    if not day:
        day = date.today().strftime("%Y-%m-%d")
    punches = get_punches_for_date(day)
    presence = get_presence_for_date(day)
    detail = numbered_punches(punches)
    summary = day_summary(punches)

    ink = colors.HexColor("#14162B")
    muted = colors.HexColor("#6B6E88")
    line = colors.HexColor("#DEDEE7")
    zebra = colors.HexColor("#F4F4F9")
    green = colors.HexColor("#1B7A57")
    madder = colors.HexColor("#B23A2B")

    sheet = getSampleStyleSheet()
    title_style = ParagraphStyle("KenTitle", parent=sheet["Title"],
                                 textColor=ink, fontSize=17, spaceAfter=2,
                                 alignment=0)
    sub_style = ParagraphStyle("KenSub", parent=sheet["Normal"],
                               textColor=muted, fontSize=9)
    h2_style = ParagraphStyle("KenH2", parent=sheet["Heading2"],
                              textColor=ink, fontSize=12.5,
                              spaceBefore=16, spaceAfter=3)
    note_style = ParagraphStyle("KenNote", parent=sheet["Normal"],
                                textColor=muted, fontSize=8.5, spaceAfter=7,
                                leading=11)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            topMargin=15 * mm, bottomMargin=14 * mm,
                            leftMargin=14 * mm, rightMargin=14 * mm,
                            title="KEN Attendance " + day)

    total_hours = sum(s["hours"] for s in summary)

    story = [
        Paragraph("KEN Attendance — Daily Report", title_style),
        Paragraph("Date " + day + "  ·  " + str(len(summary)) + " people  ·  "
                  + str(len(punches)) + " punches  ·  "
                  + ("%.1f" % total_hours) + " hours  ·  generated "
                  + datetime.now(IST).strftime("%d %b %Y %H:%M:%S"), sub_style),
        Spacer(1, 9),
    ]

    base_style = [
        ("BACKGROUND", (0, 0), (-1, 0), ink),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, zebra]),
        ("GRID", (0, 0), (-1, -1), 0.4, line),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
    ]

    story.append(Paragraph("1. Detail — every punch", h2_style))
    story.append(Paragraph(
        "Every IN and every OUT, one row each, oldest first. Nothing is merged: a person "
        "who came and went five times appears in ten rows. Source says how the punch "
        "came to exist — 'Exit zone' means they were seen walking out, 'Day end' means "
        "nobody saw them leave and the time was taken from their last sighting.",
        note_style))

    detail_data = [["ID", "Name", "Date", "Time", "Direction", "Punch No", "Source"]]
    for p in detail:
        label, _ = source_label(p)
        detail_data.append([str(p["emp_id"]), p["name"],
                            p["ts"].strftime("%d %b %Y"),
                            p["ts"].strftime("%H:%M:%S"),
                            p["direction"], "#" + str(p["seq"]), label])
    if len(detail_data) == 1:
        detail_data.append(["—", "No punches on this date", "", "", "", "", ""])

    detail_table = Table(detail_data, repeatRows=1, hAlign="LEFT",
                         colWidths=[52, 195, 85, 70, 70, 62, 80])
    detail_style = list(base_style)
    for i, row in enumerate(detail_data[1:], start=1):
        if row[4] == "IN":
            detail_style.append(("TEXTCOLOR", (4, i), (4, i), green))
        elif row[4] == "OUT":
            detail_style.append(("TEXTCOLOR", (4, i), (4, i), madder))
        if len(row) > 6 and row[6] in ("Day end", "Auto"):
            detail_style.append(("TEXTCOLOR", (6, i), (6, i), madder))
    detail_table.setStyle(TableStyle(detail_style))
    story.append(detail_table)

    story.append(Paragraph("2. Summary — first in, last out", h2_style))
    story.append(Paragraph(
        "One row per person: earliest IN, latest OUT, and the last time the camera saw "
        "them. 'OUT observed = No' means the departure was assumed rather than seen — "
        "those hours should be checked before being used for anything.", note_style))

    sum_data = [["ID", "Name", "First In", "Last Out", "Last Seen",
                 "Sightings", "Punches", "Hours", "Status", "OUT observed"]]
    for s in summary:
        seen = presence.get(s["emp_id"], {})
        sum_data.append([str(s["emp_id"]), s["name"],
                         hhmm(s["first_in"]), hhmm(s["last_out"]),
                         hhmm(seen.get("last_seen")), str(seen.get("sightings", 0)),
                         str(s["punches"]), "%.2f" % s["hours"],
                         "Still in" if s["still_in"] else "Left",
                         "No" if s["inferred"] else "Yes"])
    if len(sum_data) == 1:
        sum_data.append(["—", "No attendance on this date", "", "", "", "", "", "", "", ""])

    sum_table = Table(sum_data, repeatRows=1, hAlign="LEFT",
                      colWidths=[52, 160, 66, 66, 66, 58, 54, 50, 56, 70])
    sum_style = list(base_style)
    for i, row in enumerate(sum_data[1:], start=1):
        if row[8] == "Still in":
            sum_style.append(("TEXTCOLOR", (8, i), (8, i), green))
        if row[9] == "No":
            sum_style.append(("TEXTCOLOR", (9, i), (9, i), madder))
    sum_table.setStyle(TableStyle(sum_style))
    story.append(sum_table)

    doc.build(story)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition":
                 'attachment; filename=attendance_' + day + '.pdf'})