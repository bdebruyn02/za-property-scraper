@echo off
rem Starts the web UI on http://127.0.0.1:8765 and opens it in your browser.
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (echo Run scripts\setup.bat first. & pause & exit /b 1)
.venv\Scripts\python web_ui.py %*
