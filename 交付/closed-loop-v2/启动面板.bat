@echo off
rem 闭环智能体控制台 — 双击启动，自动打开浏览器，无需终端
cd /d "%~dp0"
".venv\Scripts\python.exe" panel\server.py
pause
