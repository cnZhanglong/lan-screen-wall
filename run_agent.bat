@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo   局域网屏幕墙 - 采集端(被控端)
echo ========================================
echo.
echo 首次运行请先安装依赖: pip install -r requirements.txt
echo.
set /p HOST=请输入主控端IP(直接回车默认 127.0.0.1): 
if "%HOST%"=="" set HOST=127.0.0.1
set /p PORT=请输入端口(直接回车默认 5000): 
if "%PORT%"=="" set PORT=5000
set /p TOKEN=请输入连接令牌(直接回车默认 change-me): 
if "%TOKEN%"=="" set TOKEN=change-me
echo.
echo 开始采集本机屏幕并推送到 %HOST%:%PORT%
python agent.py --host %HOST% --port %PORT% --token %TOKEN%
pause
