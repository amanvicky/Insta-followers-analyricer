@echo off
rem Builds a standalone Windows .exe (dist\InstagramProfileChecker.exe) with PyInstaller.
rem Uses setup_env.bat first, so building works on a fresh PC or after the
rem project folder was copied from another machine.
setlocal
cd /d "%~dp0"

call "%~dp0setup_env.bat"
if errorlevel 1 (
    echo.
    echo Build aborted: the environment is not ready (see above).
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 (
    echo.
    echo Build aborted: could not install the build tools (see above).
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed ^
  --name InstagramProfileChecker ^
  insta_checker\app.py
if errorlevel 1 (
    echo.
    echo Build failed (see the PyInstaller output above).
    pause
    exit /b 1
)

echo.
echo Done. Your exe is at dist\InstagramProfileChecker.exe
pause
exit /b 0