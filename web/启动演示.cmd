@echo off
cd /d "%~dp0"
start "" http://localhost:4173
node server.cjs
pause
