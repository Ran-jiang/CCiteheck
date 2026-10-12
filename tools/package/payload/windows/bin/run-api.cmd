@echo off
rem CCiteheck API service (HTTPS :3000), started by scheduled task via run-hidden.vbs
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src;%ROOT%;%ROOT%\runtime\site-packages"
"%ROOT%\runtime\python\pythonw.exe" -m apps.api.server ^
  --cert "%USERPROFILE%\.office-addin-dev-certs\localhost.crt" ^
  --key "%USERPROFILE%\.office-addin-dev-certs\localhost.key" ^
  >> "%ROOT%\logs\api.log" 2>&1
