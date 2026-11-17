@echo off
cd /d "%~dp0"
echo QualityGate - local data quality workbench
echo Please run this file from Windows File Explorer.
echo The browser will open ONLY after the server passes its health check.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m qualitygate.launcher
) else (
    python -m qualitygate.launcher
)
pause
