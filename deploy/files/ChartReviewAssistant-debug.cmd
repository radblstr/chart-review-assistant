@echo off
rem Debug launcher: runs the app with a visible console so errors are readable.
set "ROOT=%~dp0"
set "CRA_DATA_DIR=%ROOT%data"
set "CRA_MODE=desktop"
"%ROOT%python\python.exe" -m chart_review_assistant.app
pause
