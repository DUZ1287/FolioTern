@echo off
cd /d "%~dp0"
set /p "RADAR_QUESTION=Research question: "
if not defined RADAR_QUESTION exit /b 1
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\research_prompt.py"
pause
