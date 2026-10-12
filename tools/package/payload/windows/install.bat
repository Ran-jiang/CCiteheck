@echo off
rem CCiteheck installer entry: self-elevate then run install.ps1
rem (ASCII only in this file: cmd.exe parses batch files with the OEM codepage)
cd /d "%~dp0"
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator permission...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
