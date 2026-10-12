@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-ccitecheck.ps1"
if errorlevel 1 (
  echo.
  echo CCitecheck installation did not complete.
  pause
)
endlocal
