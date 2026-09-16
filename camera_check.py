"""
KEN Face Attendance — per-camera reality check.

For each configured camera this fetches ONE frame and reports:

  * the actual resolution arriving (not the camera's advertised one)
  * every face found, in PIXELS as well as percent
  * the real score against the stored templates

Percent hides the thing that matters. A "5% face" is 54 pixels tall on a
1080-line stream and 24 pixels on a 480-line one, and the recogniser needs
112. So a camera on its sub-stream can look identical in the logs to one on
its main stream while being completely unusable.

Run with the service STOPPED, so two copies of the face models are not in
memory at once on a 2-core box:

    systemctl stop ken-attendance
    cd /opt/ken-attendance
    source venv/bin/activate
    python camera_check.py
    systemctl start ken-attendance
"""

import os
import ssl
import json
import time
import urllib.request
from urllib.parse import urlparse, urlunparse, unquote

os.environ.setdefault("OMP_NUM_THREADS", "2")

import numpy as np
import cv2
import mysql.connector
import insightface

import local_settings as L

CAMERAS = getattr(L, "CAMERAS", [])
DB = getattr(L, "DB", dict(host="localhost", port=3306,
                           database="face_attendance", user="root",
                           password=""))

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def fetcher(url):
    """An opener that handles digest auth, plus the URL minus credentials."""
    parsed = urlparse(url)
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host = parsed.hostname or ""
    if parsed.port:
        host += ":" + str(parsed.port)
    clean = urlunparse((parsed.scheme, host, parsed.path, parsed.params,
                        parsed.query, parsed.fragment))
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, clean, user, password)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ctx),
        urllib.request.HTTPDigestAuthHandler(mgr),
        urllib.request.HTTPBasicAuthHandler(mgr))
    return opener, clean


def grab_http(url, timeout=10):
    """One frame from a snapshot URL, or from the first frame of an MJPEG stream."""
    opener, clean = fetcher(url)
    reply = opener.open(clean, timeout=timeout)
    ctype = (reply.headers.get("Content-Type") or "").lower()

    if "multipart" in ctype:
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = reply.read(16384)
            if not chunk:
                break
            buf += chunk
            start = buf.find(b"\xff\xd8")
            end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
            if start >= 0 and end > 0:
                reply.close()
                return cv2.imdecode(np.frombuffer(buf[start:end + 2], np.uint8),
                                    cv2.IMREAD_COLOR)
        reply.close()
        return None

    data = reply.read()
    reply.close()
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def grab_rtsp(url, timeout=10):
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;8000000"
    cap = cv2.VideoCapture(url)
    frame = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, got = cap.read()
        if ok and got is not None:
            frame = got
            break
    cap.release()
    return frame


def grab(url):
    if url.lower().startswith("rtsp://"):
        return grab_rtsp(url)
    return grab_http(url)


def load_templates():
    conn = mysql.connector.connect(**{k: v for k, v in DB.items()
                                      if k != "autocommit"})
    cur = conn.cursor()
    cur.execute("SELECT t.emp_id, e.name, t.embedding FROM templates t "
                "JOIN employees e ON e.emp_id = t.emp_id;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        return [], None
    names = [r[1] or str(r[0]) for r in rows]
    matrix = np.array([json.loads(r[2]) for r in rows], dtype=np.float32)
    return names, matrix


def urls_for(cam):
    """Every address worth trying for this camera, in order."""
    configured = cam.get("url")
    if isinstance(configured, str) and configured:
        return [configured]
    if isinstance(configured, (list, tuple)):
        return list(configured)
    host = cam.get("host", "")
    return ["(auto-discovered at runtime — no fixed url in local_settings) "
            + host]


def main():
    print("Loading the face model…")
    app = insightface.app.FaceAnalysis(
        name="buffalo_l", allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=-1, det_size=(1280, 1280))

    names, matrix = load_templates()
    print(str(len(names)) + " templates loaded\n")

    for cam in CAMERAS:
        print("=" * 72)
        print(cam["name"] + "   (" + cam["id"] + ")")
        print("=" * 72)

        frame = None
        used = None
        for url in urls_for(cam):
            if url.startswith("("):
                print("  " + url)
                print("  Cannot test — put the discovered url into "
                      "local_settings.py first.")
                print("  It is printed in the log at startup:")
                print("    journalctl -u ken-attendance | grep 'found a working'")
                break
            try:
                frame = grab(url)
            except Exception as e:
                print("  failed: " + str(e)[:90])
                frame = None
            if frame is not None:
                used = url
                break
            print("  no picture from that address, trying the next")

        if frame is None:
            print()
            continue

        h, w = frame.shape[:2]
        print("  address    " + (urlparse(used).path or used))
        print("  RESOLUTION " + str(w) + " x " + str(h))
        if h < 720:
            print("             ^^ SUB-STREAM. This is the whole problem for")
            print("                this camera — see the note at the end.")
        print()

        faces = app.get(frame)
        if not faces:
            print("  No face detected in this frame.\n")
            continue

        faces.sort(key=lambda f: (f.bbox[3] - f.bbox[1]), reverse=True)
        print("  %-8s %-7s %-9s %s" % ("PIXELS", "PCT", "USABLE", "BEST MATCH"))
        for face in faces:
            b = face.bbox.astype(int)
            px = int(b[3] - b[1])
            pct = px / float(h)

            if px >= 90:
                usable = "yes"
            elif px >= 60:
                usable = "marginal"
            else:
                usable = "NO"

            if matrix is None:
                best = "no templates"
            else:
                scores = matrix @ face.normed_embedding
                i = int(scores.argmax())
                best = "%s  %.3f" % (names[i][:26], float(scores[i]))

            print("  %-8s %-7s %-9s %s"
                  % (str(px) + "px", str(int(pct * 100)) + "%", usable, best))
        print()

    print("=" * 72)
    print("HOW TO READ THIS")
    print("=" * 72)
    print("PIXELS is the only number that matters. The recogniser resizes")
    print("every face to 112x112 before measuring it.")
    print()
    print("   90px and above   a real measurement, scores 0.45 to 0.75")
    print("   60 to 90px       works, but scores sit lower")
    print("   under 60px       mostly invented pixels, scores near random")
    print()
    print("If a camera reports 640x480 or similar, it is serving its")
    print("SUB-stream. Every face on it is roughly half the pixels it")
    print("should be. Switching that camera to its main stream doubles")
    print("every face height at a stroke, and costs nothing but bandwidth.")


if __name__ == "__main__":
    main()
