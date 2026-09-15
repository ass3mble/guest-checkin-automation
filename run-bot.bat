@echo off
cd /d "%~dp0"
python -m pip install -r requirements-checkin-bot.txt
python -m uvicorn checkin_bot.app:create_app --factory --host 127.0.0.1 --port 8000
pause
