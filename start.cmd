@echo off
setlocal
cd /d "%~dp0"
title Image Ranker - local companion
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Project Python is missing. Follow the setup steps in README.md.
  pause
  exit /b 1
)
if not exist "%~dp0config.json" (
  echo Local configuration is missing. Run the setup steps in README.md.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -B "%~dp0server.py" --config "%~dp0config.json"
if errorlevel 1 (
  echo.
  echo The companion could not start. Check config.json and whether another instance is running.
  pause
)
endlocal
