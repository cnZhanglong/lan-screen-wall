@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo   局域网屏幕墙 - 主控端(屏幕墙)
echo ========================================
echo.
echo 首次运行请先安装依赖: pip install -r requirements.txt
echo.
set /p TOKEN=请输入连接令牌(直接回车默认 change-me): 
if "%TOKEN%"=="" set TOKEN=change-me
set /p PORT=请输入监听端口(直接回车默认 5000): 
if "%PORT%"=="" set PORT=5000
echo.
echo 启动中... 各终端请运行:
echo   python agent.py --host 本机IP --port %PORT% --token %TOKEN%
echo.
python hub.py --port %PORT% --token %TOKEN%
pause
