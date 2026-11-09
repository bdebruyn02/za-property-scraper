@echo off
rem For Windows Task Scheduler: scrape and keep only listings not seen before.
cd /d "%~dp0.."
.venv\Scripts\python property_scraper.py --new-only >> output\daily.log 2>&1
