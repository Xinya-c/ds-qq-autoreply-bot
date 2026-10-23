@echo off
setlocal
chcp 65001 >nul
title QQ Auto Reply
cd /d "%~dp0"

echo ============================================================
echo  QQ Auto Reply daemon
echo  Backend: CodeBuddy CLI
echo  Press Ctrl+C or close this window to stop
echo ============================================================
echo.
if not exist "E:/Python/python.exe" (
  echo [ERROR] python not found
  goto :done
)
"E:/Python/python.exe" qq_autoreply.py
echo.
echo [daemon exited, code %ERRORLEVEL%]

:done
echo.
pause
