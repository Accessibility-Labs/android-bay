@echo off
cd /d "%~dp0"
start "" "%~dp0runtime\pythonw.exe" "%~dp0server.py" --demo
