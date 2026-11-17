@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0radar.py" configure
pause
