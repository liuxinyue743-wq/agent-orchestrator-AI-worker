@echo off
rem CL_AO 控制台 — 双击启动
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 尚未安装 CL_AO。请先双击 安装.bat。
    pause
    exit /b 1
)
".venv\Scripts\python.exe" panel\server.py
if errorlevel 1 (
    echo [错误] 面板异常退出。请检查 AO 是否启动、端口是否占用以及环境变量配置。
)
pause
