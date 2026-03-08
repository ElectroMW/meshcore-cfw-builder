@echo off
echo MeshCore Firmware Builder
echo ========================

REM Use PlatformIO's bundled Python
set PIO_PYTHON=%USERPROFILE%\.platformio\penv\Scripts\python.exe

if not exist "%PIO_PYTHON%" (
    echo ERROR: PlatformIO Python not found at %PIO_PYTHON%
    echo Make sure PlatformIO is installed.
    pause & exit /b 1
)

REM Check for git
where git >nul 2>&1
if errorlevel 1 (
    echo ERROR: git not found. Install from https://git-scm.com
    pause & exit /b 1
)

REM Install Flask if missing
"%PIO_PYTHON%" -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo Installing Flask...
    "%PIO_PYTHON%" -m pip install flask
)

REM Start server
echo.
echo Starting server at http://127.0.0.1:5000
echo Press Ctrl+C to stop.
echo.
start "" http://127.0.0.1:5000
cd /d "%~dp0"
"%PIO_PYTHON%" app.py
pause
