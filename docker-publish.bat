@echo off
setlocal

set IMAGE=christian45410/meshcore-firmware-builder
set TAG=latest

:: Accept version as first argument, or prompt if not provided
if "%~1"=="" (
    set /p VERSION="Enter version tag (e.g. 1.0.0): "
) else (
    set VERSION=%~1
)

echo.
echo =========================================
echo  MeshCore Firmware Builder - Docker Push
echo =========================================
echo.

:: Check Docker is running
docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running. Start Docker Desktop and try again.
    exit /b 1
)

:: Build
echo [1/4] Building image %IMAGE%:%TAG% ...
docker build -t %IMAGE%:%TAG% .
if errorlevel 1 (
    echo [ERROR] Build failed.
    exit /b 1
)
echo [1/4] Build complete.
echo.

:: Tag with version
set VERSION_TAG=%IMAGE%:%VERSION%
echo [2/4] Tagging as %VERSION_TAG% ...
docker tag %IMAGE%:%TAG% %VERSION_TAG%

:: Tag with timestamp as versioned backup  (e.g. 2026-03-05)
for /f "tokens=1-3 delims=-" %%a in ("%DATE:~-10%") do (
    set DATESTAMP=%%c-%%a-%%b
)
set DATED_TAG=%IMAGE%:%DATESTAMP%
echo [2/4] Tagging as %DATED_TAG% ...
docker tag %IMAGE%:%TAG% %DATED_TAG%

:: Push all tags
echo [3/4] Pushing %IMAGE%:%TAG% ...
docker push %IMAGE%:%TAG%
if errorlevel 1 (
    echo [ERROR] Push failed. Are you logged in? Run: docker login
    exit /b 1
)

echo [3/4] Pushing %VERSION_TAG% ...
docker push %VERSION_TAG%
if errorlevel 1 (
    echo [WARNING] Version tag push failed.
)

echo [4/4] Pushing %DATED_TAG% ...
docker push %DATED_TAG%
if errorlevel 1 (
    echo [WARNING] Dated tag push failed, but latest was pushed successfully.
)

echo.
echo =========================================
echo  Done! Image available at:
echo  https://hub.docker.com/r/%IMAGE%
echo =========================================
echo.

endlocal
