@echo off
title KEN Face Attendance System

REM Move to the folder this file is sitting in, whatever that happens to be.
REM Without this, double-clicking from Explorer can start somewhere else
REM entirely and nothing below would be found.
cd /d "%~dp0"

echo ============================================
echo   KEN Face Attendance
echo ============================================
echo.
echo Starting up. The first run of the day takes
echo about 15 seconds while the face models load.
echo.
echo LEAVE THIS WINDOW OPEN. It IS the server -
echo closing it stops attendance being recorded.
echo.

REM Check the virtual environment is actually here before trying to use it.
REM A venv cannot be moved: every .exe inside it has its original path baked
REM in, so moving or renaming the project folder breaks it and produces a
REM confusing "cannot find the file specified" error later on.
if not exist "venv\Scripts\activate.bat" (
    echo.
    echo  PROBLEM: no virtual environment found in this folder.
    echo.
    echo  If the project folder was recently moved or renamed, the old venv
    echo  is broken and has to be rebuilt. In a terminal here, run:
    echo.
    echo      python -m venv venv
    echo      venv\Scripts\activate
    echo      pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

call "venv\Scripts\activate.bat"

REM Open the dashboard in the default browser. This runs BEFORE the server is
REM ready, so the first page load may fail - refresh once and it will be there.
start "" http://localhost:8080

REM "python -m uvicorn" rather than plain "uvicorn".
REM
REM Windows resolves a bare "uvicorn" against PATH, which can easily find a
REM copy installed system-wide instead of the one in this venv. The server then
REM starts with the wrong Python and reports missing packages that ARE
REM installed - just somewhere else. Going through "python -m" guarantees the
REM active environment is the one used.
python -m uvicorn web_app:app --host 0.0.0.0 --port 8080

REM Only reached if the server stops or fails to start. Without pause the
REM window would vanish instantly, taking the error message with it.
echo.
echo ============================================
echo  The server has stopped.
echo  Any error is shown above.
echo ============================================
pause