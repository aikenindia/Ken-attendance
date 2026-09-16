@echo off
title Deploy KEN Attendance
cd /d "%~dp0"

set SERVER=root@200.234.34.251
set APPDIR=/opt/ken-attendance

echo ============================================
echo   Deploy web_app.py to the server
echo ============================================
echo.
echo Server settings are NOT touched. The database
echo password, thread limits and camera addresses
echo live in local_settings.py on the server, so
echo this only replaces the code.
echo.
echo The logo is carried over automatically from
echo the version already on the server.
echo.

if not exist "web_app.py" (
    echo  PROBLEM: web_app.py is not in this folder.
    pause
    exit /b 1
)

echo Uploading...
scp web_app.py %SERVER%:/tmp/web_app.new
if exist "local_settings.py" (
    scp local_settings.py %SERVER%:/tmp/local_settings.new
)
if exist "enroll_to_db.py" (
    scp enroll_to_db.py %SERVER%:/tmp/enroll_to_db.new
)
if exist "models\osnet_x0_25.onnx" (
    ssh %SERVER% "mkdir -p %APPDIR%/models"
    scp models\osnet_x0_25.onnx %SERVER%:/tmp/osnet_x0_25.new
)
if errorlevel 1 (
    echo.
    echo  Upload failed. Check the network and the password.
    pause
    exit /b 1
)

echo.
echo Installing and restarting...
ssh %SERVER% "bash -s" < deploy_remote.sh

if errorlevel 1 (
    echo.
    echo  Something failed. The previous version was put back
    echo  and is running. Check with:
    echo    ssh %SERVER% "journalctl -u ken-attendance -n 40 --no-pager"
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Done. https://attendance.kenhrms.com
echo ============================================
echo.
pause