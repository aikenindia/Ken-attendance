# =====================================================================
#  KEN Face Attendance — tuning block, 18 Aug
#
#  APPEND this to the END of /opt/ken-attendance/local_settings.py.
#  Do NOT replace that file — it holds the database password, and
#  overwriting it is what caused the Internal Server Error before.
#
#  Python takes the LAST assignment, so anything here overrides both
#  web_app.py and anything set earlier in local_settings.py. If you
#  appended ROOM_DET_SIZE or MAX_DETECTION_DIM by hand earlier, those
#  older lines are now harmless — these win.
# =====================================================================


# --- 1. Stop the re-read from destroying small faces -----------------
#
# Every face under 14% went through reread_face(), which crops it,
# upscales it to 320px with cubic interpolation, and re-runs detection.
# On this system that is EVERY face. An upscaled 30-pixel face gives
# poor landmarks, poor landmarks give a badly aligned 112x112 crop, and
# ArcFace is very sensitive to alignment error.
#
# 0.0 disables it. This is the single change most likely to move scores,
# and it is free to test. If scores drop instead, put 0.14 back.
REREAD_BELOW_PCT = 0.0


# --- 2. Detection input size -----------------------------------------
#
# InsightFace letterboxes the frame to fit det_size. A 1920x1080 frame
# into a (1920, 1920) box means no shrinking, which is what you want for
# small faces — but it is slow, and your room pass is already taking
# 6-7 seconds against a 6 second interval.
#
# 1280 keeps a 3% face at roughly 32px instead of 45px. That face was
# never going to be recognised anyway, so the speed is worth more.
ROOM_DET_SIZE = (1280, 1280)
MAX_DETECTION_DIM = 1600


# --- 3. Give the CPU room ---------------------------------------------
#
# Three cameras, each running YOLO plus two InsightFace models, on a
# shared VPS core. The room pass is running every 12-13 seconds despite
# ROOM_INTERVAL being 6 — it simply cannot keep up.
#
# YOLO every 1.5s is still fine for following a walking person.
YOLO_INTERVAL = 1.5
ROOM_INTERVAL = 8.0
INFERENCE_THREADS = 2


# --- 4. Thresholds — LEAVE THESE ALONE FOR NOW ------------------------
#
# Listed so you can see them in one place, at their current values.
# Lowering MATCH_THRESHOLD is tempting and it is how the warehouse
# worker got recorded as Ashish Kamble. A face scoring 0.16 against
# three different people in near-random order is not a weak match, it
# is no match — accepting it records the wrong person's attendance.
#
# Change these only AFTER check_templates.py says the templates are
# healthy and re-enrolment through the cameras has been done.
MATCH_THRESHOLD = 0.42
MATCH_MARGIN = 0.10
IDENTIFY_MIN_FACE_PCT = 0.05
IDENTIFY_COMFORTABLE_PCT = 0.18
IDENTIFY_SMALL_PENALTY = 0.10
PRESENCE_MIN_SCORE = 0.52

# Both stay off. Soft matching is what produced the false attendance,
# and auto-learning then stored that stranger's face under an employee
# name — 31 templates had to be deleted.
SOFT_MATCH_ENABLED = False
AUTO_LEARN = False


# --- 5. Keep the score log on while diagnosing ------------------------
SCORE_DEBUG = True
ZONE_DEBUG = True


# --- 6. Optional: turn the IT camera down to presence only ------------
#
# The IT camera looks across a room at people facing their monitors.
# Faces there read 2-4%, which is 20-45 pixels — below what any model
# can identify. It will not produce attendance no matter what is tuned.
#
# Uncommenting this stops it looking for faces at all, freeing most of
# a core for the two doorway cameras where faces are actually large
# enough. Video still streams; only recognition stops.
#
# for _cam in CAMERAS:
#     if _cam["id"] == "it":
#         _cam["recognition"] = False
