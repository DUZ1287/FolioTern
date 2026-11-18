@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Open-FolioTern.ps1"
if errorlevel 1 pause
