@echo off
cd /d "%~dp0"
python -m evolving_harness native %*
if errorlevel 1 pause
