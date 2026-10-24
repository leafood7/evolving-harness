@echo off
setlocal
cd /d "%~dp0"
python -m evolving_harness %*
if errorlevel 1 pause
