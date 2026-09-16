#!/bin/bash
# Runs ON THE SERVER, piped in by deploy.bat. Any non-zero exit rolls back.
set -e

APPDIR=/opt/ken-attendance
NEW=/tmp/web_app.new
LIVE="$APPDIR/web_app.py"
PREV="$APPDIR/web_app.py.prev"

# ---- 1. syntax check before anything is replaced -------------------------
python3 -m py_compile "$NEW"
echo "  syntax OK"

# ---- 2. carry the logo across --------------------------------------------
# The published file ships with LOGO_B64 empty, because the real value is a
# 19,000-character base64 string that belongs to this install rather than to
# the code. Copying it forward here means the sidebar and the PDF header do
# not silently go blank on every deploy, and nobody has to paste it by hand.
python3 - "$LIVE" "$NEW" <<'PY'
import sys
live, new = sys.argv[1], sys.argv[2]
try:
    current = open(live, encoding='utf-8').read()
except FileNotFoundError:
    print("  no existing file, skipping logo carry-over"); raise SystemExit(0)

incoming = open(new, encoding='utf-8').read()
logo = [l for l in current.split('\n') if l.startswith('LOGO_B64')]

if not logo or logo[0].strip() in ('LOGO_B64 = ""', "LOGO_B64 = ''"):
    print("  no logo on the server to carry over"); raise SystemExit(0)
if 'LOGO_B64 = ""' not in incoming:
    print("  incoming file already has a logo, leaving it alone"); raise SystemExit(0)

open(new, 'w', encoding='utf-8').write(
    incoming.replace('LOGO_B64 = ""', logo[0], 1))
print("  logo carried over,", len(logo[0]), "characters")
PY

# ---- 3. swap, keeping the previous version for rollback -------------------
cp -f "$LIVE" "$PREV" 2>/dev/null || true
mv "$NEW" "$LIVE"
chown ken:ken "$LIVE"

if [ -f /tmp/local_settings.new ]; then
    python3 -m py_compile /tmp/local_settings.new
    mv /tmp/local_settings.new "$APPDIR/local_settings.py"
    chown ken:ken "$APPDIR/local_settings.py"
    echo "  local_settings.py updated"
fi

if [ -f /tmp/enroll_to_db.new ]; then
    python3 -m py_compile /tmp/enroll_to_db.new
    mv /tmp/enroll_to_db.new "$APPDIR/enroll_to_db.py"
    chown ken:ken "$APPDIR/enroll_to_db.py"
    echo "  enroll_to_db.py updated"
fi

if [ -f /tmp/osnet_x0_25.new ]; then
    mkdir -p "$APPDIR/models"
    mv /tmp/osnet_x0_25.new "$APPDIR/models/osnet_x0_25.onnx"
    chown ken:ken "$APPDIR/models/osnet_x0_25.onnx"
    echo "  OSNet Re-ID model updated"
fi

# ---- 4. restart and confirm it STAYS up -----------------------------------
# A syntax check only proves the file parses. It says nothing about a missing
# import, a bad camera entry or a database it cannot reach — all of which let
# the service start and then die seconds later. systemd would restart it in a
# loop and the deploy would report success while attendance recorded nothing.
# Waiting and re-checking is what catches that.
systemctl restart ken-attendance
sleep 8

if ! systemctl is-active --quiet ken-attendance; then
    echo "  service did not stay up — rolling back"
    cp -f "$PREV" "$LIVE"
    chown ken:ken "$LIVE"
    systemctl restart ken-attendance
    sleep 4
    echo "  previous version restored:"
    systemctl is-active ken-attendance
    exit 1
fi

RESTARTS=$(systemctl show ken-attendance -p NRestarts --value)
sleep 6
RESTARTS2=$(systemctl show ken-attendance -p NRestarts --value)
if [ "$RESTARTS2" -gt "$RESTARTS" ]; then
    echo "  service is restart-looping — rolling back"
    cp -f "$PREV" "$LIVE"
    chown ken:ken "$LIVE"
    systemctl restart ken-attendance
    exit 1
fi

echo "  running:  $(grep '^BUILD' "$LIVE" | cut -d'"' -f2)"
journalctl -u ken-attendance -n 40 --no-pager | grep -E "\[yolo\]|\[camera\]" || true
echo "  deploy complete"
