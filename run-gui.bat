@echo off
REM auvide GUI launcher. Uses uv to provide Python + tkinter (no system Python needed).
setlocal
where uv >nul 2>&1
if errorlevel 1 (
  echo [error] uv was not found on PATH. Install uv, or run: python "%~dp0gui.py"
  pause
  exit /b 1
)
uv run --python 3.12 --with pillow "%~dp0gui.py" %*
if errorlevel 1 pause
