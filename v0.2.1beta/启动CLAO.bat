@echo off
rem CL_AO — 双击启动，自动准备 Python 环境并打开浏览器
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto bootstrap
".venv\Scripts\python.exe" -c "import platform,sys,yaml,jsonschema; raise SystemExit(0 if platform.python_implementation() == 'CPython' and sys.version_info[:2] == (3, 12) else 1)"
if errorlevel 1 goto bootstrap
goto start_panel

:bootstrap
echo Preparing the local Python environment...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"
if errorlevel 1 (
  echo Bootstrap failed. Review the error above.
  pause
  exit /b 1
)

:start_panel
".venv\Scripts\python.exe" panel\server.py
set "panel_exit=%errorlevel%"
pause
exit /b %panel_exit%
