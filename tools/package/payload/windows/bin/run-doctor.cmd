@echo off
rem Environment self-check
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src;%ROOT%;%ROOT%\runtime\site-packages"
"%ROOT%\runtime\python\python.exe" -m apps.cli.main doctor --law-db "%ROOT%\data\laws.sqlite"
