@echo off
rem ============================================================
rem key-tool 启动入口（双击即可）
rem ============================================================
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
pause
