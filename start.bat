@echo off
echo ==================================================
echo   Meet Intelligence - Live Bot ^& Command Center
echo ==================================================
echo Backend API:     http://localhost:8000
echo Live Dashboard:  http://localhost:8000/dashboard
echo Extension:       %~dp0extension
echo ==================================================
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
pause
