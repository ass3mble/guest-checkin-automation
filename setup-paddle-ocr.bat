@echo off
setlocal
py -3.11 -m pip install --upgrade pip
py -3.11 -m pip install -r requirements-paddle-ocr.txt
echo.
echo PaddleOCR setup complete on Python 3.11.
pause
