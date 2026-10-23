@echo off
setlocal
chcp 65001 >nul
title QQ Boot
cd /d "%~dp0"

if not exist "E:/Python/python.exe" (
  echo [ERROR] python not found
  goto :done
)
"E:/Python/python.exe" boot_all.py

:done
echo.
pause
