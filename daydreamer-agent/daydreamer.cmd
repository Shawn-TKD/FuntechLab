@echo off
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0daydreamer.ps1" %*
exit /b %errorlevel%
