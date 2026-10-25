@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
if errorlevel 1 goto failed
where python >nul 2>&1
if errorlevel 1 goto missing_python
set "HARNESS_TASK_WORKSPACE=%~dp0..\log-inspector"
if not exist "%HARNESS_TASK_WORKSPACE%\AGENTS.md" goto missing_project
echo Starting Pi with Harness for log-inspector...
echo Viewing cards and feedback is local. New model tasks may incur charges.
python -m evolving_harness --db "%HARNESS_TASK_WORKSPACE%\.harness\state.sqlite3" --user local --project log-inspector --provider deepseek --model deepseek-v4-pro native --workspace "%HARNESS_TASK_WORKSPACE%" --allow-write --thinking off
if errorlevel 1 goto failed
exit /b 0
:missing_python
echo Python was not found. Install Python or add it to PATH, then retry.
goto failed
:missing_project
echo The sibling log-inspector project was not found. Check the workspace path in this file.
goto failed
:failed
echo Startup or execution failed. Keep the error above for troubleshooting.
pause
exit /b 1
