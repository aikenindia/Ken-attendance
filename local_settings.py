"""
Settings for THIS server only.  /opt/ken-attendance/local_settings.py

Anything defined here replaces the matching value in web_app.py, so an app
update never overwrites the database password, the camera addresses, or the
tuning below.

After editing:  systemctl restart ken-attendance
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
# stay marked IN on a room camera (IT) until a real OUT punch, the
# exit-zone/doorway confirms it, or day-end (20:00) closes an abandoned IN
# as a safety net. Set back to True if this is ever wanted again.
AUTO_CHECKOUT_ENABLED = False

# TEMPORARY — 11 Sep. Ravi sir stood in front of the Head Office IN camera
# and got no IN punch, and with SHOW_UNKNOWN_BOXES off there is no way to
# tell, from the Live Camera page alone, whether the camera:
#   (a) never detected a face there at all (too small/dark/angled), or
#   (b) detected a face but the match score never cleared the threshold
#       against his stored templates (enrolment/quality problem), or
#   (c) matched him fine but something in the IN/OUT logic swallowed it.
# With both of these on: SCORE_DEBUG prints every face's real score to
# `journalctl -u ken-attendance -f` (look for lines starting "[score]"
# from cam "ho_in"), and SHOW_UNKNOWN_BOXES draws a grey box on the Live
# Camera page for any face that WAS detected but not matched to anyone —
# so a face with no box at all means detection itself failed (camera/
# resolution/lighting), while a grey "unidentified" box means detection
# is fine and it's the match that's failing.
# Turn both back to False once this is diagnosed — they are noisy in
# production and SHOW_UNKNOWN_BOXES exists specifically to stay off day
# to day (per SHOW_UNKNOWN_BOXES's own default), so this is a diagnostic
# setting, not a permanent one.
SCORE_DEBUG = True
ZONE_DEBUG = False
SHOW_UNKNOWN_BOXES = True

# The live-preview image (Live Camera page) already only does this
# resize-and-recompress work while someone actually has that camera's tab
# open (it drops to roughly one frame every 3 seconds on its own the moment
# nobody is watching) -- so raising these back up only costs anything during
# the minutes someone is actually looking at a feed, not around the clock.
# 5x/sec and a small preview was making the live view look choppy and
# blurry while genuinely being watched, which is what was reported. This is
# separate work from face recognition either way, so it has no effect on
# attendance accuracy.
STREAM_FPS = 15
STREAM_MAX_WIDTH = 1280


CAMERAS = [
    {
        "id": "it",
        "name": "IT Department",
        "settings": {
            "match_threshold": 0.42,
            "match_margin": 0.10,
            "presence_min_score": 0.52,
            "exit_min_face_pct": 0.10,
            "reread_below_pct": 0.0,
            "stale_out_minutes": 12,
            "instant_toggle": False,
        },
        # "httpPreview" never answers on this DVR (confirmed in the logs, not
        # a timing fluke) -- going straight to "picture" saves a wasted wait
        # on every reconnect. Real fix is still a camera/DVR setting, not
        # this list; leaving the dead address out until that's sorted.
        "url": "http://admin:%40Ken%40123@203.109.35.75:8081"
               "/ISAPI/Streaming/channels/101/picture",
        "recognition": True,
        "exit_zone": (0.65, 0.60, 1.0, 1.0),
        "watch_zone": (0.50, 0.45, 1.0, 1.0),
    },
    {
        "id": "unit1",
        "name": "KEN Global Unit 1",
        # Stopped for the HO camera work -- flip back to True (or delete
        # this line) once Unit 1 is reconnected and ready to resume.
        "enabled": False,
        # Both RTSP addresses below get "no answer" -- confirmed it's port
        # 554 (RTSP) not being reachable at all, not a wrong path/password.
        # Going straight to the snapshot address until whoever manages the
        # router opens/forwards port 554 to this DVR. Once that's confirmed
        # working, put the two RTSP lines back in FRONT of the snap.jpg line
        # (as a list) so it prefers the higher-quality video feed again:
        #   "rtsp://admin:%40Ken%40321@203.109.35.73:554"
        #   "/cam/realmonitor?channel=1&subtype=0",
        #   "rtsp://admin:%40Ken%40321@203.109.35.73:554"
        #   "/Streaming/Channels/101",
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
        "url": ["http://admin:@203.109.35.73:8444"
                "/webcapture.jpg?command=snap&channel=1",
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
        # Same story as Unit 1 -- RTSP (port 554) gets "no answer". Once
        # port 554 is opened for this DVR too, restore the list form with
        # RTSP first:
        #   "rtsp://admin:@203.109.35.76:554"
        #   "/user=admin&password=&channel=1&stream=0.sdp",
        "url": "http://admin:@203.109.35.76/webcapture.jpg?command=snap&channel=1",
        "recognition": True,
        "exit_zone": (0.28, 0.0, 0.82, 0.34),
        "watch_zone": (0.15, 0.0, 1.0, 1.0),
    },
    {
        "id": "ho",
        "name": "Head Office (OUT)",
        # Originally found by auto-discovery as a Dahua/CP Plus-style MJPEG
        # stream at the bare path below, and pinned to skip re-searching on
        # every restart. That address then started coming back "connected,
        # but 0 bytes ever arrived" -- the camera's own video engine
        # jammed, which normally needs someone to power-cycle the camera.
        # Given as a list rather than one fixed address: the app tries each
        # in order and uses whichever one actually delivers a picture. This
        # does not fix a genuinely frozen camera, but it means a channel or
        # path that is still working is used automatically instead of the
        # whole camera sitting dead until someone remembers there was only
        # ever one address configured.
        "url": [
            # subtype=1 (the camera's own low-resolution "sub stream") is
            # tried FIRST now. The high-resolution main stream (subtype=0 /
            # the bare path) was connecting, but the link out to this camera
            # can't reliably carry it -- that showed up as a laggy picture
            # and repeated "Lost the camera stream. Reconnecting." with
            # torn/corrupted frames. The sub stream is a much smaller amount
            # of data per second, which the same link should carry cleanly.
            # It's a lower-resolution picture, but still plenty for face
            # recognition and for a person watching the live view. If the
            # network link out to this camera is later confirmed solid
            # (e.g. after the hardware/network check below), the main
            # stream lines can be moved back above this one.
            "http://admin:%40Ken%40123@122.179.158.179:8081/cgi-bin/mjpg/video.cgi?channel=1&subtype=1",
            "http://admin:%40Ken%40123@122.179.158.179:8081/cgi-bin/mjpg/video.cgi?channel=1&subtype=0",
            "http://admin:%40Ken%40123@122.179.158.179:8081/cgi-bin/mjpg/video.cgi",
            "http://admin:%40Ken%40123@122.179.158.179:8081/videostream.cgi",
            "http://admin:%40Ken%40123@122.179.158.179:8081/video.cgi",
            "rtsp://admin:%40Ken%40123@122.179.158.179:554/cam/realmonitor?channel=1&subtype=1",
            "rtsp://admin:%40Ken%40123@122.179.158.179:554/cam/realmonitor?channel=1&subtype=0",
            # You've since said this camera's real password actually starts
            # with a lowercase k ("@ken@123"), opposite of what's above --
            # everything above already works, so those lines stay first and
            # untouched. These are the same two most-used addresses with
            # lowercase-k added purely as a safety net.
            "http://admin:%40ken%40123@122.179.158.179:8081/cgi-bin/mjpg/video.cgi?channel=1&subtype=1",
            "rtsp://admin:%40ken%40123@122.179.158.179:554/cam/realmonitor?channel=1&subtype=0",
        ],
        # Every address above is a continuous stream, never a single photo,
        # so this stays correct no matter which one the list picks.
        "mjpeg": True,
        "recognition": True,
        # Doorway / OUT-only, same pattern as Unit 2 -- named "out camera"
        # by request, so it can close an existing IN but never opens a new
        # one on its own.
        "mode": "doorway",
        "approach_means": "out",
        # Corrected after you confirmed the exit/walking path is on the
        # RIGHT side of this frame, not the left -- the earlier guess had
        # it mirrored the wrong way. Box now covers the right half of the
        # frame, lower two-thirds, matching the same "near corridor, not
        # the whole room" reasoning as before, just on the correct side.
        # Watch the blue box on the Live Camera page against a few real
        # people actually leaving and nudge these numbers -- widen
        # leftward if it's missing people still close to the desks, or
        # narrow further if it's catching people who are just walking past
        # without leaving.
        "exit_zone": (0.45, 0.35, 1.0, 1.0),
        "settings": {"marks_in": False},
    },
    {
        "id": "ho_in",
        "name": "Head Office (IN)",
        # The old camera at this position was a CP Plus CP-UNC-DA21L3C-LQ,
        # which never gave a working classic HTTP/RTSP feed no matter what
        # was tried -- it was replaced with a Hikvision unit instead. Same
        # network position (122.179.158.179:8080), new hardware, so the old
        # camera's long list of guessed CP Plus/Dahua-style addresses has
        # been removed entirely and replaced with Hikvision's own, standard
        # address patterns. Password confirmed as "@Ken@123" (capital K).
        "url": [
            # Sub stream (102, lower resolution/bandwidth) tried FIRST --
            # the main stream (101) connected, but the picture came back
            # badly torn/corrupted (the same "link can't carry this much
            # data" symptom OUT's camera had, fixed the same way there).
            # 102 needs far less bandwidth per second and is still plenty
            # for face recognition and normal viewing.
            "http://admin:%40Ken%40123@122.179.158.179:8080/ISAPI/Streaming/channels/102/httppreview",
            "rtsp://admin:%40Ken%40123@122.179.158.179:554/Streaming/Channels/102",
            "rtsp://admin:%40Ken%40123@122.179.158.179:8554/Streaming/Channels/102",
            # Main stream (101), kept as a fallback only -- move these back
            # above if the network link to this camera is ever confirmed
            # solid enough to carry it cleanly.
            "http://admin:%40Ken%40123@122.179.158.179:8080/ISAPI/Streaming/channels/101/httppreview",
            "rtsp://admin:%40Ken%40123@122.179.158.179:554/Streaming/Channels/101",
            "rtsp://admin:%40Ken%40123@122.179.158.179:8554/Streaming/Channels/101",
        ],
        # If none of these are it, the on-screen address stays whatever the
        # first candidate is and the error shown will still carry the
        # bytes/frames detail to work from -- open the new camera's own
        # admin page (Configuration/Setting -> Network -> Advanced ->
        # Integration Protocol, or similar) and check its actual RTSP port
        # and stream numbers, and that can be added to this list directly.
        "mjpeg": True,
        "recognition": True,
        # Doorway / IN-only, same pattern as Unit 1 -- named "in camera" by
        # request, so it can open a fresh IN but never writes an OUT on its
        # own.
        "mode": "doorway",
        "approach_means": "in",
        # Narrowed to just the actual doorway (the bright opening on the
        # right of the frame, leading outside) instead of the whole room.
        # Widened slightly on the left edge after a closer look at someone
        # actually standing at the door, so the zone catches the doorway's
        # wooden frame too, not just the bright outdoor patch past it.
        # This box is only about WHEN to confirm someone has reached the
        # door -- face recognition itself still runs on the whole picture,
        # so a person is already being identified while still walking
        # across the room (like the person mid-floor in your screenshot);
        # this box just decides the moment their IN gets recorded. Watch
        # the blue box on the Live Camera page against a few real people
        # walking through and nudge these four numbers if it's cutting off
        # the door or catching desks that aren't part of the doorway.
        "exit_zone": (0.68, 0.05, 1.0, 1.0),
        # A person walking in is first spotted while still small and far
        # across the room, on the frame the camera was already shrunk down
        # to for detection -- a lot of the detail that would help tell one
        # face from another gets lost in that shrink well before size is
        # even the issue. "reread_below_pct" tells the app: any face
        # smaller than this fraction of the frame's height gets a second
        # look -- cropped straight out of the camera's original, full
        # resolution frame (not the shrunk one), zoomed in, and re-checked.
        # This was OFF everywhere (the app-wide default is 0), so a distant
        # face here only ever got the one low-detail attempt. 0.18 matches
        # this app's own "comfortable" size for a confident match, so
        # anyone smaller/further than that now gets the zoomed-in second
        # look automatically.
        # reread_below_pct was raised to 0.18 here (from the app-wide 0.0)
        # on the assumption that cropping from "the camera's original full
        # resolution frame" gives a real second look at a distant, small
        # face. That assumption stopped being true once this camera's URL
        # list (above) was changed to try the 102 SUB-stream first, to fix
        # the torn/corrupted picture on the main stream — the "original
        # full resolution frame" the app captures for ho_in is now the
        # lower-resolution sub-stream itself, so reread_face() is cropping
        # an already-small face and cubic-upscaling it to 320px, which
        # invents detail rather than revealing it (this is the exact
        # failure mode tuning_block.py already documented for the IT
        # camera: "poor landmarks give a badly aligned crop, and ArcFace
        # is very sensitive to alignment error").
        #
        # Set back to 0.0 (matching every other camera) for Ravi's case:
        # this can only ever make a real match score LOWER by feeding it a
        # blurrier crop, never turn a wrong face into an accepted match, so
        # it is safe to try without new evidence. Re-raise it (and confirm
        # with camera_check.py what the ho_in sub-stream's native
        # resolution actually is, in pixels, not percent) if faces caught
        # early/far from the door still don't get an IN once the SCORE_DEBUG
        # log above shows real scores.
        "settings": {"marks_out": False, "reread_below_pct": 0.0},
    },
]