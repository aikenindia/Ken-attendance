"""
Settings for THIS server only.  /opt/ken-attendance/local_settings.py

Anything defined here replaces the matching value in web_app.py, so an app
update never overwrites the database password, the camera addresses, or the
tuning below.

After editing: systemctl restart ken-attendance
"""


DB = dict(host="localhost", port=3306, database="face_attendance",
          user="kenapp", password="KENCamera@2026", autocommit=True)


SECRET_KEY = "464ae0c4cb2f79647d00c7a73e0796daac8a808aa7fd1a338af2a1b17cbd3d7d"

SESSION_HOURS = 48
SIGN_OUT_ON_BROWSER_CLOSE = False


INFERENCE_THREADS = 2
ROOM_INTERVAL = 5.0
ROOM_DET_SIZE = (1280, 1280)
MAX_DETECTION_DIM = 1600

YOLO_ENABLED = False
YOLO_INTERVAL = 0.2
REREAD_BELOW_PCT = 0.0
SIDE_MATCH_THRESHOLD = 0.28

OSNET_REID_ENABLED = True
OSNET_MODEL_PATH = "models/osnet_x0_25.onnx"
REID_MATCH_THRESHOLD = 0.70


MATCH_THRESHOLD = 0.42
MATCH_MARGIN = 0.10
IDENTIFY_MIN_FACE_PCT = 0.05
IDENTIFY_COMFORTABLE_PCT = 0.18
IDENTIFY_SMALL_PENALTY = 0.10
PRESENCE_MIN_SCORE = 0.52
FACE_IN_PERSON_MIN = 0.25

EXIT_MIN_FACE_PCT = 0.10

TRACK_MOVES = True
MOVE_QUIET_SECONDS = 30
SOFT_MATCH_ENABLED = False

AUTO_LEARN = False
AUTO_LEARN_MIN_SCORE = 0.40
AUTO_LEARN_MAX_SCORE = 0.85

PRESENCE_WRITE_SECONDS = 20

EXIT_ARM_MIN_HOLD_SECONDS = 3.0

# The "AUTO" OUT punches (a camera deciding on its own that someone left,
# without a real exit-zone/doorway confirmation) were firing seconds after
# the person's own IN -- turned off per management's request. People now
# stay marked IN on a room camera until a real OUT punch, the exit-zone/doorway
# confirms it, or day-end (20:00) closes an abandoned IN as a safety net.
# Set back to True if this is ever wanted again.
AUTO_CHECKOUT_ENABLED = False

# TEMPORARY — 11 Sep. Ravi sir stood in front of the Head Office IN camera
# and got no IN punch, and with SHOW_UNKNOWN_BOXES off there is no way to tell,
# from the Live Camera page alone, whether the camera:
#   (a) never detected a face there at all (too small/dark/angled), or
#   (b) detected a face but the match score never cleared the threshold
#       against his stored templates (enrolment/quality problem), or
#   (c) matched him fine but something in the IN/OUT logic swallowed it.
# With both of these on: SCORE_DEBUG prints every face's real score to
# `journalctl -u ken-attendance -f` (look for lines starting "[score]"
# from cam "ho_in"), and SHOW_UNKNOWN_BOXES draws a grey box on the Live Camera
# page for any face that WAS detected but not matched to anyone — so a face
# with no box at all means detection itself failed (camera/resolution/lighting),
# while a grey "unidentified" box means detection is fine and it's the match
# that's failing.
# Turn both back to False once this is diagnosed — they are noisy in
# production and SHOW_UNKNOWN_BOXES exists specifically to stay off day to day
# (per SHOW_UNKNOWN_BOXES's own default), so this is a diagnostic setting,
# not a permanent one.
SCORE_DEBUG = True
ZONE_DEBUG = False
SHOW_UNKNOWN_BOXES = True

# The live-preview image (Live Camera page) already only does this
# resize-and-recompress work while someone actually has that camera's tab
# open (it drops to roughly one frame every 3 seconds on its own the moment
# nobody is watching) -- so raising these back up only costs anything during
# the minutes someone is actually looking at a feed, not around the clock.
# 5x/sec and a small preview was making the live view look choppy and blurry
# while genuinely being watched, which is what was reported. This is separate
# work from face recognition either way, so it has no effect on attendance
# accuracy.
STREAM_FPS = 15
STREAM_MAX_WIDTH = 1280


CAMERAS = [
    {
        "id": "unit1",
        "name": "KEN Global Unit 1",
        # Stopped for the HO camera work -- flip back to True (or delete
        # this line) once Unit 1 is reconnected and ready to resume.
        "enabled": False,
        "url": "https://admin:%40Ken%40321@203.109.35.73:8443/snap.jpg",
        "recognition": True,
        "exit_zone": (0.0, 0.0, 1.0, 1.0),
        "mode": "doorway",
        "approach_means": "in",
        "settings": {"marks_out": False},
    },
    {
        "id": "unit2",
        "name": "KEN Global Unit 2",
        # Stopped for the HO camera work -- flip back to True (or delete
        # this line) once Unit 2 is reconnected and ready to resume.
        "enabled": False,
        "url": ["http://admin:@203.109.35.73:8444/webcapture.jpg?command=snap&channel=1",
                "http://admin:@203.109.35.73:8444/snap.jpg"],
        "recognition": True,
        "mode": "doorway",
        "approach_means": "out",
        "exit_zone": (0.0, 0.0, 1.0, 1.0),
        "settings": {"marks_in": False},
    },
    {
        "id": "entry",
        "name": "ANITA GODOWN",
        # Stopped for the HO camera work -- flip back to True (or delete
        # this line) once Anita Godown is reconnected and ready to resume.
        "enabled": False,
        "url": "http://admin:@203.109.35.76/webcapture.jpg?command=snap&channel=1",
        "recognition": True,
        "exit_zone": (0.28, 0.0, 0.82, 0.34),
        "watch_zone": (0.15, 0.0, 1.0, 1.0),
    },
    {
        "id": "ho",
        "name": "Head Office (OUT)",
        "url": [
            "http://admin:%40Ken%40123@122.179.158.179:8081/cgi-bin/mjpg/video.cgi?channel=1&subtype=1",
            "http://admin:%40Ken%40123@122.179.158.179:8081/cgi-bin/mjpg/video.cgi?channel=1&subtype=0",
            "http://admin:%40Ken%40123@122.179.158.179:8081/cgi-bin/mjpg/video.cgi",
            "http://admin:%40Ken%40123@122.179.158.179:8081/videostream.cgi",
            "http://admin:%40Ken%40123@122.179.158.179:8081/video.cgi",
            "rtsp://admin:%40Ken%40123@122.179.158.179:554/cam/realmonitor?channel=1&subtype=1",
            "rtsp://admin:%40Ken%40123@122.179.158.179:554/cam/realmonitor?channel=1&subtype=0",
            "http://admin:%40ken%40123@122.179.158.179:8081/cgi-bin/mjpg/video.cgi?channel=1&subtype=1",
            "rtsp://admin:%40ken%40123@122.179.158.179:554/cam/realmonitor?channel=1&subtype=0",
        ],
        "mjpeg": True,
        "recognition": True,
        "mode": "doorway",
        "approach_means": "out",
        "exit_zone": (0.45, 0.35, 1.0, 1.0),
        "settings": {"marks_in": False},
    },
    {
        "id": "ho_in",
        "name": "Head Office (IN)",
        "url": [
            "http://admin:%40Ken%40123@122.179.158.179:8080/ISAPI/Streaming/channels/102/httppreview",
            "rtsp://admin:%40Ken%40123@122.179.158.179:554/Streaming/Channels/102",
            "rtsp://admin:%40Ken%40123@122.179.158.179:8554/Streaming/Channels/102",
            "http://admin:%40Ken%40123@122.179.158.179:8080/ISAPI/Streaming/channels/101/httppreview",
            "rtsp://admin:%40Ken%40123@122.179.158.179:554/Streaming/Channels/101",
            "rtsp://admin:%40Ken%40123@122.179.158.179:8554/Streaming/Channels/101",
        ],
        "mjpeg": True,
        "recognition": True,
        "mode": "doorway",
        "approach_means": "in",
        "exit_zone": (0.68, 0.05, 1.0, 1.0),
        "settings": {"marks_out": False, "reread_below_pct": 0.0},
    },
]
