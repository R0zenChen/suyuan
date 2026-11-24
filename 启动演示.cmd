@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动溯源 Demo...
echo 浏览器打开 http://127.0.0.1:8000
echo 请保持此窗口开启。按 Ctrl+C 停止服务。
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -u app.py
) else (
  python -u app.py
)
echo.
echo 服务已停止。如果上方有错误，请检查 Python 是否已安装，或端口 8000 是否被占用。
pause
