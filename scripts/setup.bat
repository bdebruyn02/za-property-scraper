@echo off
rem One-time setup on Windows: creates .venv, installs packages and the headless browser.
cd /d "%~dp0.."
if not exist .venv (python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & exit /b 1))
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install -r requirements.txt || exit /b 1
.venv\Scripts\python -m playwright install chromium
echo.
echo Setup done. Start the web UI with scripts\start_ui.bat
