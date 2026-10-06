@echo off
cd /d "%~dp0"
"%~dp0runtime\python.exe" "%~dp0make_release.py"
if errorlevel 1 pause
