@echo off
cd /d "%~dp0.."

:: Desktop app -- native WebView2 window, no console
set CRA_MODE=desktop
start "" ".venv\Scripts\pythonw.exe" -m chart_review_assistant.app
