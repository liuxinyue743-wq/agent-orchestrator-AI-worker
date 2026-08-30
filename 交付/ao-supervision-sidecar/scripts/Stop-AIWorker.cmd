@echo off
REM Double-click entry point: stops the supervision sidecar + AO daemon.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-AIWorker.ps1" %*
pause
