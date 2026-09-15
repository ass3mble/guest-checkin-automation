@echo off
cd /d "%~dp0"
tools\cloudflared.exe tunnel --url http://127.0.0.1:8000
pause
