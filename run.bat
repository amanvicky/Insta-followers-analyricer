@echo off
rem Launches the Instagram Profile Checker.
rem The environment is prepared by setup_env.bat, which repairs a .venv that
rem was copied from another PC (its pyvenv.cfg points at a Python that is not
rem there). Never launch a venv blindly - that is what produced
rem "Python venv launcher is sorry to say ... did not find executable at ...".
setlocal
cd /d "%~dp0"

rem Absolute path: do not rely on CWD or PATH to find the helper.
call "%~dp0setup_env.bat"
if errorlevel 1 goto :setup_failed

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" insta_checker\app.py
    exit /b 0
)

".venv\Scripts\python.exe" insta_checker\app.py
exit /b 0

:setup_failed
echo.
echo The app was not started because setup did not finish.
pause
exit /b 1