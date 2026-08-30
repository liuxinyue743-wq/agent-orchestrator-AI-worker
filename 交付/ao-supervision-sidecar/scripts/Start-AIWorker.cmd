@echo off
REM Double-click entry point: starts AO + supervision sidecar in a console.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-AIWorker.ps1" %*
pause
