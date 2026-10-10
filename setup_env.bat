@echo off
rem ---------------------------------------------------------------------------
rem Ensures .venv exists AND actually works on THIS PC, then installs deps.
rem
rem Why this file exists: a .venv is not portable. Its pyvenv.cfg hardcodes the
rem interpreter path of the machine that created it, so copying the project
rem folder to another PC leaves run.bat launching a launcher that points at a
rem python.exe that does not exist there ("Python venv launcher is sorry to
rem say... did not find executable at ..."). This script detects that case and
rem rebuilds the venv instead of failing.
rem
rem Exit code 0 = ready to run, 1 = setup failed (message already printed).
rem ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"

rem --- 1. Does a usable venv already exist? -------------------------------
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys" >nul 2>&1
    if not errorlevel 1 goto :check_deps
)

rem --- 2. No venv, or a copied-from-another-PC one: rebuild it. -------------
echo.
echo [setup] No working .venv on this PC - building one now.
echo [setup] (A .venv copied from another computer cannot work: it points at
echo [setup]  that computer's Python. Deleting and recreating it is automatic.)
if exist ".venv" rmdir /s /q ".venv"

set "BASE="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "BASE=py -3"
if not defined BASE (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "BASE=python"
)
if not defined BASE goto :nopython

echo [setup] Creating .venv with: %BASE%
%BASE% -m venv .venv
if errorlevel 1 goto :failed
if not exist "%VENV_PY%" goto :failed

rem --- 3. Dependencies (only when missing, so a normal launch stays fast). --
:check_deps
"%VENV_PY%" -c "import instagrapi, tkinter, requests" >nul 2>&1
if not errorlevel 1 (
    echo [setup] Environment ready.
    exit /b 0
)

echo [setup] Installing dependencies (first run on a PC takes a minute)...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :failed
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :pipfailed

"%VENV_PY%" -c "import instagrapi, tkinter, requests" >nul 2>&1
if errorlevel 1 goto :pipfailed

echo [setup] Environment ready.
exit /b 0

rem --- failures: say what to do, in plain words. ---------------------------
:nopython
echo.
echo [setup] ERROR: Python was not found on this PC.
echo.
echo   1. Install Python 3.11 or newer from https://www.python.org/downloads/
echo      (tick "Add python.exe to PATH" during setup).
echo   2. Close this window and double-click run.bat again.
echo.
exit /b 1

:pipfailed
echo.
echo [setup] ERROR: dependencies could not be installed (see the pip output above).
echo         Usually one of two things: no internet connection on this PC, or a
echo         requirement in requirements.txt that cannot be installed.
echo         Fix that, then double-click run.bat again.
echo.
exit /b 1

:failed
echo.
echo [setup] ERROR: could not create the .venv. See the messages above.
echo         Check that Python is installed and that you can write to this folder.
echo.
exit /b 1