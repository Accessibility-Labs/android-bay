@echo off
cd /d "%~dp0"
if not exist "runtime\pythonw.exe" (
  echo The portable runtime is missing. Extract the complete AndroidBay folder first.
  pause
  exit /b 1
)
start "" "%~dp0runtime\pythonw.exe" "%~dp0server.py" %*
