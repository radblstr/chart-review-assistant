@echo off
cd /d "%~dp0.."

:: Demo mode -- the bundled demo in demo_data (no Mosaiq access or patient data), no
:: console. CRA_DEMO_MODE=1 drives the demo path in app.py.
set CRA_DEMO_MODE=1
set CRA_MODE=desktop
start "" ".venv\Scripts\pythonw.exe" -m chart_review_assistant.app
