@echo off
cd /d "%~dp0"
"%~dp0runtime\python.exe" "%~dp0launcher.py" --stop
if errorlevel 1 pause
