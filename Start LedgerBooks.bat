@echo off
title LedgerBooks
cd /d "%~dp0"
echo Starting LedgerBooks...
echo.
echo Once it says "running at http://localhost:5057", open that address in your browser.
echo Keep this window open while you're using LedgerBooks. Close it to stop the server.
echo.
"venv\Scripts\python.exe" serve.py
pause
