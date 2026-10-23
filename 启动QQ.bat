@echo off
setlocal
chcp 65001 >nul
title QQ Manager
cd /d "%~dp0"

if not exist "E:/Python/python.exe" (
  echo [ERROR] python not found
  goto :done
)
"E:/Python/python.exe" qq_manager.py

:done
echo.
pause
