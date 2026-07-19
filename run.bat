@echo off
REM auvide launcher - forwards all args to upscale_hdr.py
REM   double-clickable: drag a video file onto this .bat to upscale with defaults
setlocal
where python >nul 2>&1
if errorlevel 1 (
  echo [error] Python was not found on PATH. Install Python 3.8+ and retry.
  pause
  exit /b 1
)
python "%~dp0upscale_hdr.py" %*
if errorlevel 1 pause
