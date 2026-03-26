@echo off
cd /d "%~dp0"

echo [start] openim sdk bridge
start /b cmd /c "npm run openim-bridge"

timeout /t 3 /nobreak >nul

echo [start] python main
start /b cmd /c "python main.py"
