Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Meet Intelligence - Live Bot & Command Center   " -ForegroundColor Yellow
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "Backend API:     http://localhost:8000" -ForegroundColor Green
Write-Host "Live Dashboard:  http://localhost:8000/dashboard" -ForegroundColor Green
Write-Host "Extension:       $PSScriptRoot\extension" -ForegroundColor Gray
Write-Host "==================================================" -ForegroundColor Cyan
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
