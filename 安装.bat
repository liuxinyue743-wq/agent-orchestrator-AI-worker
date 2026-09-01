@echo off
rem ============================================================
rem  闭环多智能体系统 v0.2 - 一键安装
rem  在本目录(closed-loop-v2)内运行：建虚拟环境、装依赖、离线自检
rem  AO 可用 scripts\Install-AO.ps1 从官方 Release 下载并校验
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] 创建 Python 虚拟环境 .venv ...
python -m venv .venv
if errorlevel 1 (
    echo [错误] 未找到 python。请先安装 Python 3.10+ 并加入 PATH，再重新运行本脚本。
    pause
    exit /b 1
)

echo [2/3] 安装依赖 ...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重新运行本脚本。
    pause
    exit /b 1
)

echo [3/3] 运行离线自检（不访问网络）...
set PYTHONPATH=src
set "PATH=%CD%\.venv\Scripts;%PATH%"
.venv\Scripts\python.exe -m pytest tests -q
if errorlevel 1 (
    echo [错误] 自检未通过，请联系开发方。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  安装完成！
echo  下一步：
echo   1. 确认 AO 已安装并注册 demo 项目（见 docs\首次初始化与安装AO.md）
echo   2. 启动 AO 桌面应用（守护进程监听 127.0.0.1:3001）
echo   3. 双击 启动面板.bat
echo ============================================================
pause
